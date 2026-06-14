"""
🚀 Mouss Tec Enterprise — Views & MAS Orchestrator Layer
=========================================================
المعمارية: كل وكيل (Agent) عبارة عن دالة نقية (Pure Function) تقبل بيانات وترجع بيانات.
الـ Views هي فقط HTTP adapters تستدعي الوكلاء — لا منطق داخل الـ view نفسه.
الـ Orchestrator يُدار بـ async-safe thread pool مع DB connection management صحيح.
"""

from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Sum, F, Q
from django.utils import timezone
from django.db import connection, transaction, close_old_connections
from django.core.cache import cache
from django.conf import settings
from django_tenants.utils import schema_context
from decimal import Decimal, InvalidOperation

import json
import urllib.parse
import base64
import uuid
import re
import logging
import concurrent.futures

from ..ai_services import predict_parts_from_dtc, scan_invoice_image_ai, call_gemini_layer
from clients.models import GlobalB2BMarketplace, Client, BlindBiddingRequest
from clients.services.entitlements import require_feature

try:
    import qrcode
    from io import BytesIO
except ImportError:
    qrcode = None

from ..models import (
    Product, Inventory, SaleInvoice, SaleInvoiceItem, Branch,
    Customer, Vehicle, ScrapDismantlingJob, ScrapDismantlingYield,
    FinancialTransaction, EmployeeShift, MaintenanceContract, Treasury,
    ChartOfAccount, AccountingEntry, InventoryMovement, StockAlert,
    ImportSession, AuditLog, PurchaseInvoice, Vendor,
)


# Shared utilities live in their own submodule and are re-exported here
# so existing view definitions (defined below) and external imports still see them.
from .utils import *  # noqa: F401, F403
from .utils import _json_response_safe, _get_branch_for_user, _require_tenant  # noqa: F401


# Barcode, mobile cycle count, offline POS sync, diagnostic intake, forecasting, movement log.



# =====================================================================
# 🏎️ 5. الجرد، الباركود، والمزامنة اللحظية
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def barcode_lookup_api(request):
    code = request.GET.get('code', '').strip()
    if not code:
        return _json_response_safe({"error": "الباركود مفقود"}, 400)

    branch = _get_branch_for_user(request.user)
    product = (
        Product.objects.filter(barcode=code).first()
        or Product.objects.filter(part_number=code).first()
    )
    if not product:
        return _json_response_safe({"error": "القطعة غير مسجلة"}, 404)

    inv = Inventory.objects.filter(product=product, branch=branch).first() if branch else None
    return _json_response_safe({
        "id": product.id,
        "name": product.name,
        "part_number": product.part_number,
        "price": float(product.retail_price),
        "available_qty": inv.quantity if inv else 0,
        "elasticity_indicator": float(product.ai_price_elasticity),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'stock')
def mobile_cycle_count_api(request):
    """HTTP adapter for mobile inventory cycle count — delegates to InventoryService."""
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    try:
        data = json.loads(request.body)
        code = data.get('barcode', '').strip()
        actual_qty = int(data.get('actual_qty', 0))
        branch = _get_branch_for_user(request.user)

        # المشرف العام (superuser) يجب أن يُحدد الفرع صراحةً
        if branch is None:
            branch_id = data.get('branch_id')
            if branch_id:
                branch = Branch.objects.filter(pk=branch_id).first()
            if branch is None:
                branch = Branch.objects.first()
            if branch is None:
                return _json_response_safe({"error": "لا يوجد فرع مسجل بالنظام."}, 400)

        product = (
            Product.objects.filter(barcode=code).first()
            or Product.objects.filter(part_number=code).first()
        )
        if not product:
            return _json_response_safe({"error": "المنتج غير مسجل"}, 404)

        from inventory.services.inventory_service import InventoryService
        diff, new_qty = InventoryService.execute_cycle_count(product, branch, actual_qty)

        return _json_response_safe({
            "status": "success",
            "message": f"تم جرد {product.name}. الرصيد: {new_qty}",
            "variance": diff,
        })
    except Exception as e:
        logger.error("[CYCLE COUNT] %s", e)
        return _json_response_safe({"error": str(e)}, 500)


@login_required(login_url='/login/')
@tenant_required
def offline_pos_sync_api(request):
    """
    [FIXED BY QA]: منطق حفظ الفواتير القادمة من الـ IndexedDB عند انقطاع الإنترنت.
    تم استبدال الـ (عدّ) السطحي بحفظ فعلي في قاعدة البيانات بطريقة آمنة (Atomic Transaction).
    """
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    try:
        data = json.loads(request.body)
        invoices_data = data.get('invoices', [])

        if not invoices_data:
            return _json_response_safe({"status": "success", "message": "لا توجد فواتير للمزامنة."})

        branch = _get_branch_for_user(request.user)

        # المشرف العام: fallback لأول فرع
        if branch is None:
            branch = Branch.objects.first()
        if branch is None:
            return _json_response_safe({"error": "لا يوجد فرع مسجل بالنظام."}, 400)

        synced_count = 0
        skipped_count = 0

        with transaction.atomic():
            for inv_data in invoices_data:
                # فحص التكرار بناءً على local_id (idempotency)
                local_id = inv_data.get('local_id')
                if local_id:
                    already_synced = SaleInvoice.objects.filter(
                        notes__contains=f"[OFFLINE:{local_id}]"
                    ).exists()
                    if already_synced:
                        skipped_count += 1
                        continue

                customer_id = inv_data.get('customer_id')
                customer = Customer.objects.filter(id=customer_id).first() if customer_id else None

                # العميل إلزامي في SaleInvoice — إنشاء عميل "زائر" إذا لم يُحدد
                if customer is None:
                    customer, _ = Customer.objects.get_or_create(
                        phone='+20000000000',
                        defaults={'name': 'عميل زائر (POS)'},
                    )

                offline_tag = f"[OFFLINE:{local_id}]" if local_id else "[OFFLINE]"

                new_invoice = SaleInvoice.objects.create(
                    customer=customer,
                    branch=branch,
                    invoice_type='sale',  # FIX: كان 'cash' وهو غير صالح
                    status='posted',
                    total_amount=Decimal(str(inv_data.get('total_amount', 0))),
                    paid_amount=Decimal(str(inv_data.get('total_amount', 0))),
                    notes=f"مزامنة أوفلاين {offline_tag}",
                    date_created=timezone.now()
                )

                items = inv_data.get('items', [])
                total_cost = Decimal('0.00')
                for item in items:
                    product = Product.objects.filter(id=item.get('product_id')).first()
                    if product:
                        qty = int(item.get('quantity', 1))
                        unit_price = Decimal(str(item.get('unit_price', 0)))
                        cost_at_sale = product.average_cost or product.purchase_price or Decimal('0.00')

                        # Validate stock availability
                        inv_record = Inventory.objects.select_for_update().filter(
                            product=product, branch=branch
                        ).first()
                        if inv_record and inv_record.quantity >= qty:
                            inv_record.quantity = F('quantity') - qty
                            inv_record.save()
                        elif inv_record:
                            logger.warning(
                                "[OFFLINE SYNC] Insufficient stock for %s: have %s, need %s",
                                product.part_number, inv_record.quantity, qty
                            )
                            continue  # Skip item if no stock

                        sale_item = SaleInvoiceItem(
                            invoice=new_invoice,
                            product=product,
                            quantity=qty,
                            unit_price=unit_price,
                            cost_at_sale=cost_at_sale,
                        )
                        sale_item.full_clean()  # Run model validation
                        sale_item.save()
                        total_cost += cost_at_sale * qty

                # Update invoice totals
                new_invoice.total_cost = total_cost
                new_invoice.net_profit = new_invoice.total_amount - total_cost
                new_invoice.save(update_fields=['total_cost', 'net_profit'])

                synced_count += 1

        msg = f"تمت مزامنة {synced_count} فاتورة بنجاح وتحديث أرصدة المخازن."
        if skipped_count:
            msg += f" (تم تخطي {skipped_count} فاتورة مكررة)"

        return _json_response_safe({
            "status": "success",
            "message": msg,
        })
    except Exception as e:
        logger.error(f"[OFFLINE SYNC] {e}")
        return _json_response_safe({"error": "فشل المزامنة وإدخال البيانات"}, 500)


@login_required(login_url='/login/')
@tenant_required
def receive_diagnostic_report(request):
    if request.method != 'POST':
        return HttpResponseForbidden()
    try:
        data = json.loads(request.body)
        vin = data.get('vin', '')
        if not Vehicle.objects.filter(chassis_number=vin).exists():
            return _json_response_safe({"error": "مركبة غير مسجلة"}, 404)
        return _json_response_safe({"status": "success", "message": "تقرير OBD2 مستلم."})
    except Exception as e:
        return _json_response_safe({"error": str(e)}, 500)


@login_required(login_url='/login/')
@tenant_required
def parts_cross_reference_api(request):
    part_number = request.GET.get('part_number', '').strip()
    alts = list(
        Product.objects
            .filter(Q(name__icontains=part_number) | Q(part_number__icontains=part_number))
            .values('id', 'name', 'part_number', 'retail_price')[:5]
    )
    return _json_response_safe({"status": "success", "alternatives": alts})


# =====================================================================
# 📊 14. واجهات التحليلات المتقدمة (Analytics APIs)
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def inventory_movement_log_api(request):
    """سجل حركات المخزون لمنتج محدد — يدعم ?product_id=X"""
    product_id = request.GET.get('product_id')
    if not product_id:
        return _json_response_safe({"error": "product_id مطلوب"}, 400)

    movements = InventoryMovement.objects.filter(product_id=product_id).order_by('-created_at')[:50]
    return _json_response_safe({
        "status": "success",
        "movements": [
            {
                "date": str(m.created_at),
                "reason": m.get_reason_display(),
                "branch": str(m.branch),
                "change": m.quantity_change,
                "before": m.quantity_before,
                "after": m.quantity_after,
                "note": m.note,
            }
            for m in movements
        ]
    })


# =====================================================================
# 📈 Inventory Forecasting (AI-driven demand prediction)
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'stock')
def inventory_forecast_api(request):
    """
    📊 توقع الطلب على المنتجات بناءً على بيانات البيع التاريخية.

    خوارزمية:
    - يحسب متوسط البيع اليومي خلال آخر 90 يوم
    - يحدد المنتجات تحت الـ reorder point
    - يقترح كمية إعادة الطلب لتغطية 30 يوم
    """
    from datetime import timedelta as _td
    from django.db.models import Sum as _Sum
    from inventory.models import Product as _Product, SaleInvoiceItem as _SII, Inventory as _Inv

    days_history = int(request.GET.get('days', 90))
    target_coverage_days = int(request.GET.get('coverage', 30))
    branch = _get_branch_for_user(request.user)

    cutoff = timezone.now() - _td(days=days_history)

    # Total qty sold per product
    sales_qs = _SII.objects.filter(invoice__status='posted', invoice__date_created__gte=cutoff)
    if branch:
        sales_qs = sales_qs.filter(invoice__branch=branch)

    sales_by_product = sales_qs.values('product_id').annotate(total_sold=_Sum('quantity'))

    forecasts = []
    for entry in sales_by_product:
        product_id = entry['product_id']
        total_sold = entry['total_sold'] or 0
        avg_daily = total_sold / days_history if days_history > 0 else 0

        try:
            product = _Product.objects.get(pk=product_id)
        except _Product.DoesNotExist:
            continue

        # Current stock across all branches (or specific branch)
        inv_qs = _Inv.objects.filter(product=product)
        if branch:
            inv_qs = inv_qs.filter(branch=branch)
        current_stock = inv_qs.aggregate(total=_Sum('quantity'))['total'] or 0

        # Days until stockout at current consumption rate
        days_remaining = (current_stock / avg_daily) if avg_daily > 0 else 9999

        # Suggested reorder quantity to cover target_coverage_days
        target_stock = avg_daily * target_coverage_days
        reorder_qty = max(0, int(target_stock - current_stock))

        # Urgency score (lower days_remaining = more urgent)
        if days_remaining < 7:
            urgency = 'critical'
        elif days_remaining < 14:
            urgency = 'high'
        elif days_remaining < 30:
            urgency = 'medium'
        else:
            urgency = 'low'

        forecasts.append({
            'product_id': product_id,
            'product_name': product.name,
            'part_number': product.part_number,
            'current_stock': current_stock,
            'avg_daily_sales': round(avg_daily, 2),
            'days_until_stockout': round(days_remaining, 1) if days_remaining < 9999 else None,
            'suggested_reorder_qty': reorder_qty,
            'urgency': urgency,
        })

    # Sort by urgency: critical → high → medium → low
    urgency_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
    forecasts.sort(key=lambda f: (urgency_order[f['urgency']], -f['avg_daily_sales']))

    return _json_response_safe({
        'status': 'success',
        'period_days': days_history,
        'target_coverage_days': target_coverage_days,
        'forecast_count': len(forecasts),
        'forecasts': forecasts[:50],  # Top 50 most urgent
        'summary': {
            'critical': sum(1 for f in forecasts if f['urgency'] == 'critical'),
            'high': sum(1 for f in forecasts if f['urgency'] == 'high'),
            'medium': sum(1 for f in forecasts if f['urgency'] == 'medium'),
        },
    })
