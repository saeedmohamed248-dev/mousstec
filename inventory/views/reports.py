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


# Async reports, statements (customer/vendor), ledger, bank reconciliation, commission payout, import flow, P&L / trial-balance / balance-sheet / product-profitability.



# =====================================================================
# 📊 6. التقارير غير المتزامنة
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def request_async_report_api(request):
    """
    طلب تقرير غير متزامن — حالياً يدعم التوليد المباشر للتقارير الصغيرة.
    الأنواع المدعومة: inventory_valuation, sales_summary, purchase_summary
    """
    report_type = request.GET.get('type', 'inventory_valuation')
    branch = _get_branch_for_user(request.user)

    try:
        if report_type == 'inventory_valuation':
            from inventory.models import Inventory as InventoryModel
            inv_qs = InventoryModel.objects.select_related('product', 'branch').all()
            if branch:
                inv_qs = inv_qs.filter(branch=branch)
            items = []
            total_value = Decimal('0')
            for inv in inv_qs:
                value = inv.quantity * (inv.product.average_cost or Decimal('0'))
                items.append({
                    "product": inv.product.name,
                    "part_number": inv.product.part_number,
                    "branch": inv.branch.name,
                    "quantity": inv.quantity,
                    "avg_cost": float(inv.product.average_cost or 0),
                    "value": float(value),
                })
                total_value += value
            return _json_response_safe({
                "status": "ready",
                "report_type": report_type,
                "data": items,
                "total_value": float(total_value),
            })

        elif report_type == 'sales_summary':
            from_date = request.GET.get('from', '')
            to_date = request.GET.get('to', '')
            try:
                from_d = timezone.datetime.strptime(from_date, '%Y-%m-%d').date() if from_date else timezone.now().date().replace(day=1)
                to_d = timezone.datetime.strptime(to_date, '%Y-%m-%d').date() if to_date else timezone.now().date()
            except ValueError:
                return _json_response_safe({"error": "تنسيق تاريخ خاطئ"}, 400)

            qs = SaleInvoice.objects.filter(status='posted', date_created__date__gte=from_d, date_created__date__lte=to_d)
            if branch:
                qs = qs.filter(branch=branch)
            agg = qs.aggregate(
                total_revenue=Sum('total_amount'),
                total_cost=Sum('total_cost'),
                total_profit=Sum('net_profit'),
            )
            return _json_response_safe({
                "status": "ready",
                "report_type": report_type,
                "period": {"from": str(from_d), "to": str(to_d)},
                "data": {
                    "invoice_count": qs.count(),
                    "total_revenue": float(agg['total_revenue'] or 0),
                    "total_cost": float(agg['total_cost'] or 0),
                    "total_profit": float(agg['total_profit'] or 0),
                },
            })

        else:
            return _json_response_safe({
                "error": f"نوع التقرير '{report_type}' غير مدعوم. الأنواع المتاحة: inventory_valuation, sales_summary"
            }, 400)

    except Exception as e:
        logger.error(f"[REPORT] Error generating {report_type}: {e}")
        return _json_response_safe({"error": "حدث خطأ أثناء توليد التقرير"}, 500)


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def download_async_report_api(request, task_id):
    return _json_response_safe({
        "status": "not_implemented",
        "message": "تحميل التقارير غير المتزامنة سيتم تفعيله قريباً. استخدم التقارير المباشرة حالياً.",
    }, 501)


# =====================================================================
# 📊 11. تقارير الأرباح والخسائر (Profit & Loss Reports)
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def profit_loss_report_api(request):
    """
    تقرير الأرباح والخسائر — يقارن الإيرادات بالمصروفات لفترة محددة.
    يدعم ?from=YYYY-MM-DD&to=YYYY-MM-DD
    🔒 محصور: admin + manager فقط
    """

    from_date = request.GET.get('from', '')
    to_date = request.GET.get('to', '')

    try:
        if from_date:
            from_date = timezone.datetime.strptime(from_date, '%Y-%m-%d').date()
        else:
            from_date = timezone.now().date().replace(day=1)
        if to_date:
            to_date = timezone.datetime.strptime(to_date, '%Y-%m-%d').date()
        else:
            to_date = timezone.now().date()
    except ValueError:
        return _json_response_safe({"error": "تنسيق تاريخ خاطئ. استخدم YYYY-MM-DD"}, 400)

    branch = _get_branch_for_user(request.user)

    # الإيرادات من فواتير البيع المعتمدة
    sales_qs = SaleInvoice.objects.filter(status='posted', date_created__date__gte=from_date, date_created__date__lte=to_date)
    purchases_qs = PurchaseInvoice.objects.filter(status='posted', date_created__date__gte=from_date, date_created__date__lte=to_date)

    if branch:
        sales_qs = sales_qs.filter(branch=branch)
        purchases_qs = purchases_qs.filter(branch=branch)

    agg = sales_qs.aggregate(
        rev=Sum('total_amount'),
        cost=Sum('total_cost'),
        profit=Sum('net_profit'),
    )
    total_revenue = agg['rev'] or Decimal('0')
    total_cost = agg['cost'] or Decimal('0')
    # gross_profit = sum of net_profit (already = revenue_excl_tax - cogs, tax excluded)
    gross_profit = agg['profit'] or Decimal('0')

    # المصروفات العمومية
    expenses_qs = FinancialTransaction.objects.filter(
        transaction_type='out', date__date__gte=from_date, date__date__lte=to_date,
        sale_invoice__isnull=True, purchase_invoice__isnull=True  # مصروفات تشغيلية فقط
    )
    if branch:
        expenses_qs = expenses_qs.filter(treasury__branch=branch)

    total_expenses = expenses_qs.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    net_profit = gross_profit - total_expenses

    # التفصيل بحسب فئات المصروفات
    expense_breakdown = list(
        expenses_qs.values('category__name')
        .annotate(total=Sum('amount'))
        .order_by('-total')
    )

    # التفصيل بحسب نوع الفاتورة
    revenue_by_type = list(
        sales_qs.values('invoice_type')
        .annotate(total=Sum('total_amount'), profit=Sum('net_profit'))
        .order_by('-total')
    )

    return _json_response_safe({
        "status": "success",
        "period": {"from": str(from_date), "to": str(to_date)},
        "summary": {
            "total_revenue": float(total_revenue),
            "total_cost_of_goods": float(total_cost),
            "gross_profit": float(gross_profit),
            "total_operating_expenses": float(total_expenses),
            "net_profit": float(net_profit),
            "profit_margin_percent": round(float(net_profit / max(gross_profit, Decimal('0.01')) * 100), 2) if gross_profit > 0 else 0,
        },
        "revenue_by_type": [
            {"type": r['invoice_type'], "revenue": float(r['total']), "profit": float(r['profit'] or 0)}
            for r in revenue_by_type
        ],
        "expense_breakdown": [
            {"category": e['category__name'] or 'غير مصنف', "total": float(e['total'])}
            for e in expense_breakdown
        ],
        "invoices_count": sales_qs.count(),
        "purchases_count": purchases_qs.count(),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def trial_balance_api(request):
    """
    ميزان المراجعة — يعرض أرصدة جميع الحسابات (مدين/دائن).
    🔒 محصور: admin + manager فقط
    """
    from inventory.models import ChartOfAccount, AccountingEntry

    as_of = request.GET.get('as_of', '')
    try:
        if as_of:
            as_of_date = timezone.datetime.strptime(as_of, '%Y-%m-%d').date()
        else:
            as_of_date = timezone.now().date()
    except ValueError:
        return _json_response_safe({"error": "تنسيق تاريخ خاطئ. استخدم YYYY-MM-DD"}, 400)

    accounts = ChartOfAccount.objects.filter(is_active=True).order_by('code')
    rows = []
    total_debit = Decimal('0')
    total_credit = Decimal('0')

    for account in accounts:
        entries_qs = AccountingEntry.objects.filter(
            account=account, entry_date__date__lte=as_of_date
        )
        agg = entries_qs.aggregate(
            sum_debit=Sum('debit'),
            sum_credit=Sum('credit')
        )
        d = agg['sum_debit'] or Decimal('0')
        c = agg['sum_credit'] or Decimal('0')

        # Normal balance: assets/expenses are debit-normal, liabilities/equity/revenue are credit-normal
        if account.account_type in ('asset', 'expense'):
            balance = d - c
            row_debit = balance if balance > 0 else Decimal('0')
            row_credit = abs(balance) if balance < 0 else Decimal('0')
        else:
            balance = c - d
            row_credit = balance if balance > 0 else Decimal('0')
            row_debit = abs(balance) if balance < 0 else Decimal('0')

        if row_debit > 0 or row_credit > 0:
            rows.append({
                "code": account.code,
                "name": account.name,
                "type": account.account_type,
                "debit": float(row_debit),
                "credit": float(row_credit),
            })
            total_debit += row_debit
            total_credit += row_credit

    return _json_response_safe({
        "status": "success",
        "as_of": str(as_of_date),
        "accounts": rows,
        "totals": {
            "total_debit": float(total_debit),
            "total_credit": float(total_credit),
            "is_balanced": abs(total_debit - total_credit) < Decimal('0.01'),
        },
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def balance_sheet_api(request):
    """
    الميزانية العمومية — أصول = خصوم + حقوق ملكية.
    🔒 محصور: admin + manager فقط
    """
    from inventory.models import ChartOfAccount, AccountingEntry

    as_of = request.GET.get('as_of', '')
    try:
        if as_of:
            as_of_date = timezone.datetime.strptime(as_of, '%Y-%m-%d').date()
        else:
            as_of_date = timezone.now().date()
    except ValueError:
        return _json_response_safe({"error": "تنسيق تاريخ خاطئ. استخدم YYYY-MM-DD"}, 400)

    def _section_data(account_type):
        accounts = ChartOfAccount.objects.filter(
            account_type=account_type, is_active=True
        ).order_by('code')
        items = []
        section_total = Decimal('0')
        for account in accounts:
            agg = AccountingEntry.objects.filter(
                account=account, entry_date__date__lte=as_of_date
            ).aggregate(sum_debit=Sum('debit'), sum_credit=Sum('credit'))
            d = agg['sum_debit'] or Decimal('0')
            c = agg['sum_credit'] or Decimal('0')
            if account_type in ('asset', 'expense'):
                balance = d - c
            else:
                balance = c - d
            if balance != 0:
                items.append({"code": account.code, "name": account.name, "balance": float(balance)})
                section_total += balance
        return items, section_total

    assets, total_assets = _section_data('asset')
    liabilities, total_liabilities = _section_data('liability')
    equity_items, total_equity = _section_data('equity')

    # Add net income (revenue - expenses) to equity as retained earnings
    revenue_items, total_revenue = _section_data('revenue')
    expense_items, total_expenses = _section_data('expense')
    net_income = total_revenue - total_expenses
    total_equity_with_income = total_equity + net_income

    return _json_response_safe({
        "status": "success",
        "as_of": str(as_of_date),
        "assets": {"items": assets, "total": float(total_assets)},
        "liabilities": {"items": liabilities, "total": float(total_liabilities)},
        "equity": {
            "items": equity_items,
            "retained_earnings": float(net_income),
            "total": float(total_equity_with_income),
        },
        "balance_check": {
            "total_assets": float(total_assets),
            "total_liabilities_equity": float(total_liabilities + total_equity_with_income),
            "is_balanced": abs(total_assets - (total_liabilities + total_equity_with_income)) < Decimal('0.01'),
        },
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def product_profitability_api(request):
    """أربحية كل منتج — أعلى 20 منتج ربحية 🔒 admin + manager"""

    branch = _get_branch_for_user(request.user)
    items_qs = SaleInvoiceItem.objects.filter(invoice__status='posted')
    if branch:
        items_qs = items_qs.filter(invoice__branch=branch)

    from django.db.models import Sum, F, ExpressionWrapper, DecimalField
    product_stats = (
        items_qs.values('product__name', 'product__part_number')
        .annotate(
            total_qty=Sum('quantity'),
            total_revenue=Sum(F('quantity') * F('unit_price'), output_field=DecimalField()),
            total_cost=Sum(F('quantity') * F('product__average_cost'), output_field=DecimalField()),
        )
        .annotate(
            profit=F('total_revenue') - F('total_cost'),
        )
        .order_by('-profit')[:20]
    )

    return _json_response_safe({
        "status": "success",
        "top_products": [
            {
                "name": p['product__name'],
                "part_number": p['product__part_number'],
                "qty_sold": p['total_qty'],
                "revenue": float(p['total_revenue'] or 0),
                "cost": float(p['total_cost'] or 0),
                "profit": float(p['profit'] or 0),
            }
            for p in product_stats
        ]
    })


# =====================================================================
# 📥 12. نظام الاستيراد الآمن (Safe Import System)
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def import_upload_api(request):
    """
    رفع ملف استيراد (CSV/Excel) وإنشاء جلسة استيراد جديدة.
    يبدأ الفحص والمعاينة تلقائياً.
    """
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)

    entity_type = request.POST.get('entity_type', '')
    uploaded_file = request.FILES.get('file')

    if not uploaded_file:
        return _json_response_safe({"error": "الملف مطلوب"}, 400)
    if entity_type not in ('customer', 'product', 'invoice', 'vendor'):
        return _json_response_safe({"error": "نوع البيانات غير مدعوم. الخيارات: customer, product, invoice, vendor"}, 400)

    import tablib
    try:
        # قراءة الملف
        file_content = uploaded_file.read()
        if uploaded_file.name.endswith('.csv'):
            dataset = tablib.Dataset().load(file_content.decode('utf-8-sig'), format='csv')
        elif uploaded_file.name.endswith(('.xlsx', '.xls')):
            dataset = tablib.Dataset().load(file_content, format='xlsx')
        else:
            return _json_response_safe({"error": "صيغة الملف غير مدعومة. استخدم CSV أو Excel."}, 400)

        # إنشاء جلسة الاستيراد + التحقق — atomic لضمان عدم وجود session يتيمة
        with transaction.atomic():
            session = ImportSession.objects.create(
                entity_type=entity_type,
                status='validating',
                uploaded_file=uploaded_file,
                original_filename=uploaded_file.name,
                total_rows=len(dataset),
                created_by=request.user,
            )

            # الفحص والتحقق
            validation_errors = []
            conflicts = []
            valid_count = 0

            for i, row in enumerate(dataset.dict, start=1):
                row_errors = []

                if entity_type == 'product':
                    if not row.get('name') and not row.get('اسم المنتج'):
                        row_errors.append("اسم المنتج مطلوب")
                    pn = row.get('part_number') or row.get('رقم القطعة', '')
                    if pn and Product.objects.filter(part_number=pn).exists():
                        conflicts.append({"row": i, "field": "part_number", "value": pn, "reason": "رقم القطعة موجود مسبقاً"})

                elif entity_type == 'customer':
                    if not row.get('name') and not row.get('اسم العميل'):
                        row_errors.append("اسم العميل مطلوب")
                    phone = row.get('phone') or row.get('الهاتف', '')
                    if phone and Customer.objects.filter(phone=phone).exists():
                        conflicts.append({"row": i, "field": "phone", "value": phone, "reason": "رقم الهاتف مسجل مسبقاً"})

                elif entity_type == 'vendor':
                    if not row.get('name') and not row.get('اسم المورد'):
                        row_errors.append("اسم المورد مطلوب")

                if row_errors:
                    validation_errors.append({"row": i, "errors": row_errors})
                else:
                    valid_count += 1

            session.valid_rows = valid_count
            session.error_rows = len(validation_errors)
            session.conflict_rows = len(conflicts)
            session.validation_report = {"errors": validation_errors}
            session.conflict_report = {"conflicts": conflicts}
            session.status = 'preview'
            session.save()

        return _json_response_safe({
            "status": "success",
            "session_id": str(session.session_id),
            "total_rows": session.total_rows,
            "valid_rows": valid_count,
            "error_rows": len(validation_errors),
            "conflict_rows": len(conflicts),
            "preview_url": f"/system/api/v1/import/{session.session_id}/preview/",
            "message": "تم فحص الملف. راجع المعاينة قبل التأكيد."
        })

    except Exception as e:
        logger.error(f"[IMPORT UPLOAD] {e}")
        return _json_response_safe({"error": f"فشل قراءة الملف: {str(e)}"}, 500)


@login_required(login_url='/login/')
@tenant_required
def import_preview_api(request, session_id):
    """معاينة جلسة الاستيراد — عرض التقارير والتعارضات"""
    session = get_object_or_404(ImportSession, session_id=session_id, created_by=request.user)
    return _json_response_safe({
        "status": "success",
        "session_id": str(session.session_id),
        "entity_type": session.entity_type,
        "current_status": session.status,
        "total_rows": session.total_rows,
        "valid_rows": session.valid_rows,
        "error_rows": session.error_rows,
        "conflict_rows": session.conflict_rows,
        "validation_report": session.validation_report,
        "conflict_report": session.conflict_report,
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def import_confirm_api(request, session_id):
    """
    تأكيد الاستيراد — يبدأ الاستيراد الفعلي بعد المعاينة.
    يأخذ نسخة احتياطية قبل أي تعديل.
    🚀 محسّن: يستخدم bulk_create / bulk_update بدلاً من حفظ عنصر تلو الآخر.
    """
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)

    session = get_object_or_404(ImportSession, session_id=session_id, created_by=request.user)
    if session.status != 'preview':
        return _json_response_safe({"error": f"الجلسة في حالة '{session.get_status_display()}' ولا يمكن التأكيد."}, 400)

    import tablib
    BULK_BATCH_SIZE = 500  # حجم الدفعة لتجنب استنزاف الذاكرة مع الملفات العملاقة

    try:
        session.status = 'importing'
        session.save(update_fields=['status'])

        # قراءة الملف مجدداً
        session.uploaded_file.seek(0)
        content = session.uploaded_file.read()
        if session.original_filename.endswith('.csv'):
            dataset = tablib.Dataset().load(content.decode('utf-8-sig'), format='csv')
        else:
            dataset = tablib.Dataset().load(content, format='xlsx')

        imported_ids = []
        backup_data = []

        with transaction.atomic():
            if session.entity_type == 'product':
                # ── المرحلة 1: تصنيف الصفوف (جديد vs تحديث) بضربة DB واحدة ──
                rows_parsed = []
                for row in dataset.dict:
                    pn = row.get('part_number') or row.get('رقم القطعة', '')
                    name = row.get('name') or row.get('اسم المنتج', '')
                    if not name:
                        continue
                    rows_parsed.append({'pn': pn, 'name': name, 'row': row})

                # استعلام واحد لجلب كل المنتجات الموجودة بدل N استعلام
                all_pns = [r['pn'] for r in rows_parsed if r['pn']]
                existing_map = {}
                if all_pns:
                    existing_map = {
                        p.part_number: p
                        for p in Product.objects.filter(part_number__in=all_pns)
                    }

                # ── المرحلة 2: فرز إلى قائمتين (تحديث + إنشاء) ──
                to_update = []  # منتجات موجودة تحتاج تحديث
                to_create = []  # منتجات جديدة

                for r in rows_parsed:
                    existing = existing_map.get(r['pn']) if r['pn'] else None
                    if existing:
                        # نسخة احتياطية
                        backup_data.append({
                            'model': 'Product', 'pk': existing.pk,
                            'snapshot': {
                                'name': existing.name,
                                'part_number': existing.part_number,
                                'retail_price': str(existing.retail_price),
                            }
                        })
                        existing.name = r['name']
                        if r['row'].get('retail_price') or r['row'].get('سعر البيع'):
                            existing.retail_price = Decimal(
                                str(r['row'].get('retail_price') or r['row'].get('سعر البيع', '0'))
                            )
                        to_update.append(existing)
                    else:
                        to_create.append(Product(
                            name=r['name'],
                            part_number=r['pn'] or f"IMP-{uuid.uuid4().hex[:8]}",
                            retail_price=Decimal(str(
                                r['row'].get('retail_price') or r['row'].get('سعر البيع', '0') or '0'
                            )),
                            purchase_price=Decimal(str(
                                r['row'].get('purchase_price') or r['row'].get('سعر الشراء', '0') or '0'
                            )),
                        ))

                # ── المرحلة 3: تنفيذ bulk بدفعات ──
                if to_update:
                    Product.objects.bulk_update(
                        to_update, ['name', 'retail_price'], batch_size=BULK_BATCH_SIZE
                    )
                    imported_ids.extend([p.pk for p in to_update])

                if to_create:
                    created_objs = Product.objects.bulk_create(
                        to_create, batch_size=BULK_BATCH_SIZE
                    )
                    imported_ids.extend([p.pk for p in created_objs])

            elif session.entity_type == 'customer':
                # ── نفس النمط: استعلام واحد + bulk ──
                rows_parsed = []
                seen_phones = set()
                for row in dataset.dict:
                    name = row.get('name') or row.get('اسم العميل', '')
                    phone = row.get('phone') or row.get('الهاتف', '')
                    if not name:
                        continue
                    # Normalize phone like Customer.save() does
                    if phone:
                        phone = re.sub(r'[\s\-\(\)]+', '', phone)
                        if phone.startswith('00'):
                            phone = '+' + phone[2:]
                        elif phone.startswith('0') and not phone.startswith('+'):
                            phone = '+2' + phone
                    # Skip duplicate phones within same file
                    if phone and phone in seen_phones:
                        continue
                    if phone:
                        seen_phones.add(phone)
                    rows_parsed.append({'name': name, 'phone': phone, 'row': row})

                all_phones = [r['phone'] for r in rows_parsed if r['phone']]
                existing_map = {}
                if all_phones:
                    existing_map = {
                        c.phone: c
                        for c in Customer.objects.filter(phone__in=all_phones)
                    }

                to_update = []
                to_create = []

                for r in rows_parsed:
                    existing = existing_map.get(r['phone']) if r['phone'] else None
                    if existing:
                        backup_data.append({
                            'model': 'Customer', 'pk': existing.pk,
                            'snapshot': {'name': existing.name, 'phone': existing.phone}
                        })
                        existing.name = r['name']
                        to_update.append(existing)
                    else:
                        # Assign unique phone if empty to avoid unique constraint violation
                        cust_phone = r['phone'] or f'+20000{uuid.uuid4().hex[:6]}'
                        to_create.append(Customer(name=r['name'], phone=cust_phone))

                if to_update:
                    Customer.objects.bulk_update(
                        to_update, ['name'], batch_size=BULK_BATCH_SIZE
                    )
                    imported_ids.extend([c.pk for c in to_update])

                if to_create:
                    created_objs = Customer.objects.bulk_create(
                        to_create, batch_size=BULK_BATCH_SIZE
                    )
                    imported_ids.extend([c.pk for c in created_objs])

            elif session.entity_type == 'vendor':
                # ── Vendor: استعلام واحد + bulk_create للجدد ──
                rows_parsed = []
                for row in dataset.dict:
                    name = row.get('name') or row.get('اسم المورد', '')
                    if not name:
                        continue
                    rows_parsed.append({
                        'name': name,
                        'phone': row.get('phone') or row.get('الهاتف', ''),
                    })

                all_names = [r['name'] for r in rows_parsed]
                existing_map = {
                    v.name: v
                    for v in Vendor.objects.filter(name__in=all_names)
                }

                to_create = []
                for r in rows_parsed:
                    if r['name'] in existing_map:
                        imported_ids.append(existing_map[r['name']].pk)
                    else:
                        to_create.append(Vendor(name=r['name'], phone=r['phone']))
                        # منع التكرار داخل نفس الملف
                        existing_map[r['name']] = None

                if to_create:
                    created_objs = Vendor.objects.bulk_create(
                        to_create, batch_size=BULK_BATCH_SIZE
                    )
                    imported_ids.extend([v.pk for v in created_objs])

        session.imported_ids = imported_ids
        session.backup_snapshot = {"backup": backup_data}
        session.status = 'completed'
        session.completed_at = timezone.now()
        session.save()

        return _json_response_safe({
            "status": "success",
            "message": f"تم استيراد {len(imported_ids)} سجل بنجاح.",
            "imported_count": len(imported_ids),
            "session_id": str(session.session_id),
        })

    except Exception as e:
        session.status = 'failed'
        session.save(update_fields=['status'])
        logger.error(f"[IMPORT CONFIRM] {e}")
        return _json_response_safe({"error": f"فشل الاستيراد: {str(e)}"}, 500)


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def import_rollback_api(request, session_id):
    """
    التراجع عن استيراد — يحذف السجلات المُستوردة ويستعيد النسخ الأصلية.
    """
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)

    session = get_object_or_404(ImportSession, session_id=session_id, created_by=request.user)
    if session.status != 'completed':
        return _json_response_safe({"error": "لا يمكن التراجع إلا عن استيراد مكتمل."}, 400)

    try:
        with transaction.atomic():
            # استعادة النسخ الاحتياطية
            backup_data = session.backup_snapshot.get('backup', [])
            for item in backup_data:
                model_name = item['model']
                if model_name == 'Product':
                    Product.objects.filter(pk=item['pk']).update(**{
                        k: v for k, v in item['snapshot'].items()
                        if k in ('name', 'part_number')
                    })
                elif model_name == 'Customer':
                    Customer.objects.filter(pk=item['pk']).update(**{
                        k: v for k, v in item['snapshot'].items()
                        if k in ('name', 'phone')
                    })

            # حذف السجلات الجديدة (التي لم تكن تحديث)
            backup_pks = {item['pk'] for item in backup_data}
            new_ids = [pk for pk in session.imported_ids if pk not in backup_pks]

            if session.entity_type == 'product':
                Product.objects.filter(pk__in=new_ids).delete()
            elif session.entity_type == 'customer':
                Customer.objects.filter(pk__in=new_ids).delete()
            elif session.entity_type == 'vendor':
                Vendor.objects.filter(pk__in=new_ids).delete()

        session.status = 'rolled_back'
        session.save(update_fields=['status'])

        return _json_response_safe({
            "status": "success",
            "message": f"تم التراجع عن الاستيراد وحذف {len(new_ids)} سجل جديد واستعادة {len(backup_data)} سجل أصلي.",
        })

    except Exception as e:
        logger.error(f"[IMPORT ROLLBACK] {e}")
        return _json_response_safe({"error": f"فشل التراجع: {str(e)}"}, 500)


# 📄 13. كشوف الحساب (Statement of Account)
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def customer_statement_api(request, customer_id):
    """
    كشف حساب عميل — كل المعاملات المالية مع الرصيد التراكمي.
    يدعم ?from=YYYY-MM-DD&to=YYYY-MM-DD
    """

    customer = get_object_or_404(Customer, pk=customer_id)
    from_date = request.GET.get('from', '')
    to_date = request.GET.get('to', '')

    try:
        from_date = timezone.datetime.strptime(from_date, '%Y-%m-%d').date() if from_date else None
        to_date = timezone.datetime.strptime(to_date, '%Y-%m-%d').date() if to_date else None
    except ValueError:
        return _json_response_safe({"error": "تنسيق تاريخ خاطئ"}, 400)

    # فواتير البيع
    invoices_qs = SaleInvoice.objects.filter(customer=customer, status='posted').order_by('date_created')
    if from_date:
        invoices_qs = invoices_qs.filter(date_created__date__gte=from_date)
    if to_date:
        invoices_qs = invoices_qs.filter(date_created__date__lte=to_date)

    # المدفوعات — استبعاد المدفوعات المرتبطة بفواتير (لأنها محسوبة في سطر الفاتورة)
    payments_qs = FinancialTransaction.objects.filter(
        customer=customer, transaction_type='in',
        sale_invoice__isnull=True,  # دفعات مستقلة فقط (ليست جزء من فاتورة)
    ).order_by('date')
    if from_date:
        payments_qs = payments_qs.filter(date__date__gte=from_date)
    if to_date:
        payments_qs = payments_qs.filter(date__date__lte=to_date)

    raw_entries = []
    for inv in invoices_qs:
        raw_entries.append({
            "date": str(inv.date_created.date()),
            "sort_key": inv.date_created,
            "type": "invoice",
            "reference": f"فاتورة #{inv.pk}",
            "description": f"فاتورة {inv.get_invoice_type_display()}",
            "debit": float(inv.total_amount),
            "credit": float(inv.paid_amount),
            "delta": inv.due_amount,
        })

    for pay in payments_qs:
        raw_entries.append({
            "date": str(pay.date.date()),
            "sort_key": pay.date,
            "type": "payment",
            "reference": f"سند قبض #{pay.pk}",
            "description": pay.description or 'دفعة نقدية',
            "debit": 0,
            "credit": float(pay.amount),
            "delta": -pay.amount,
        })

    raw_entries.sort(key=lambda x: x['sort_key'])

    entries = []
    running_balance = Decimal('0')
    for e in raw_entries:
        running_balance += e.pop('delta')
        e.pop('sort_key')
        e['balance'] = float(running_balance)
        entries.append(e)

    # 🛡️ total_paid must include BOTH standalone payments AND payments embedded
    # in invoices (invoice.paid_amount). The old version aggregated only
    # standalone payments → it reported 0 paid even when the customer paid
    # in full at the time of the invoice, which is the common case.
    invoice_paid_sum = invoices_qs.aggregate(Sum('paid_amount'))['paid_amount__sum'] or 0
    standalone_paid_sum = payments_qs.aggregate(Sum('amount'))['amount__sum'] or 0
    return _json_response_safe({
        "status": "success",
        "customer": {"id": customer.pk, "name": customer.name, "phone": customer.phone, "current_balance": float(customer.balance)},
        "period": {"from": str(from_date or 'بداية'), "to": str(to_date or 'اليوم')},
        "entries": entries,
        "totals": {
            "total_invoiced": float(invoices_qs.aggregate(Sum('total_amount'))['total_amount__sum'] or 0),
            "total_paid": float(Decimal(str(invoice_paid_sum)) + Decimal(str(standalone_paid_sum))),
            "outstanding_balance": float(customer.balance),
        },
    })


@login_required(login_url='/login/')
@tenant_required
def vendor_statement_api(request, vendor_id):
    """كشف حساب مورد"""

    vendor = get_object_or_404(Vendor, pk=vendor_id)
    from_date = request.GET.get('from', '')
    to_date = request.GET.get('to', '')

    try:
        from_date = timezone.datetime.strptime(from_date, '%Y-%m-%d').date() if from_date else None
        to_date = timezone.datetime.strptime(to_date, '%Y-%m-%d').date() if to_date else None
    except ValueError:
        return _json_response_safe({"error": "تنسيق تاريخ خاطئ"}, 400)

    invoices_qs = PurchaseInvoice.objects.filter(vendor=vendor, status='posted').order_by('date_created')
    if from_date:
        invoices_qs = invoices_qs.filter(date_created__date__gte=from_date)
    if to_date:
        invoices_qs = invoices_qs.filter(date_created__date__lte=to_date)

    # استبعاد المدفوعات المرتبطة بفواتير شراء (لأنها محسوبة في سطر الفاتورة)
    payments_qs = FinancialTransaction.objects.filter(
        vendor=vendor, transaction_type='out',
        purchase_invoice__isnull=True,  # دفعات مستقلة فقط
    ).order_by('date')
    if from_date:
        payments_qs = payments_qs.filter(date__date__gte=from_date)
    if to_date:
        payments_qs = payments_qs.filter(date__date__lte=to_date)

    raw_entries = []
    for inv in invoices_qs:
        due = Decimal(str(inv.total_amount)) - Decimal(str(inv.paid_amount))
        raw_entries.append({
            "date": str(inv.date_created.date()),
            "sort_key": inv.date_created,
            "type": "invoice",
            "reference": f"فاتورة شراء #{inv.pk}",
            "description": f"فاتورة شراء من {vendor.name}",
            "debit": float(inv.total_amount),
            "credit": float(inv.paid_amount),
            "delta": due,
        })

    for pay in payments_qs:
        raw_entries.append({
            "date": str(pay.date.date()),
            "sort_key": pay.date,
            "type": "payment",
            "reference": f"سند صرف #{pay.pk}",
            "description": pay.description or 'تسوية مورد',
            "debit": 0,
            "credit": float(pay.amount),
            "delta": -pay.amount,
        })

    raw_entries.sort(key=lambda x: x['sort_key'])

    entries = []
    running_balance = Decimal('0')
    for e in raw_entries:
        running_balance += e.pop('delta')
        e.pop('sort_key')
        e['balance'] = float(running_balance)
        entries.append(e)

    # 🛡️ total_paid must include payments embedded in invoices too — same
    # fix as the customer statement: standalone payments alone undercount.
    invoice_paid_sum = invoices_qs.aggregate(Sum('paid_amount'))['paid_amount__sum'] or 0
    standalone_paid_sum = payments_qs.aggregate(Sum('amount'))['amount__sum'] or 0
    return _json_response_safe({
        "status": "success",
        "vendor": {"id": vendor.pk, "name": vendor.name, "phone": vendor.phone, "current_balance": float(vendor.balance)},
        "entries": entries,
        "totals": {
            "total_purchases": float(invoices_qs.aggregate(Sum('total_amount'))['total_amount__sum'] or 0),
            "total_paid": float(Decimal(str(invoice_paid_sum)) + Decimal(str(standalone_paid_sum))),
            "outstanding_balance": float(vendor.balance),
        },
    })


@login_required(login_url='/login/')
@tenant_required
def customer_statement_print(request, customer_id):
    """طباعة كشف حساب العميل"""
    customer = get_object_or_404(Customer, pk=customer_id)
    invoices = SaleInvoice.objects.filter(customer=customer, status='posted').order_by('date_created')
    payments = FinancialTransaction.objects.filter(
        customer=customer, transaction_type='in',
        sale_invoice__isnull=True,  # دفعات مستقلة فقط (ليست جزء من فاتورة)
    ).order_by('date')

    return render(request, 'inventory/statement_print.html', {
        'entity': customer,
        'entity_type': 'customer',
        'invoices': invoices,
        'payments': payments,
        'print_date': timezone.now(),
    })


@login_required(login_url='/login/')
@tenant_required
def vendor_statement_print(request, vendor_id):
    """طباعة كشف حساب المورد"""
    vendor = get_object_or_404(Vendor, pk=vendor_id)
    invoices = PurchaseInvoice.objects.filter(vendor=vendor, status='posted').order_by('date_created')
    payments = FinancialTransaction.objects.filter(
        vendor=vendor, transaction_type='out',
        purchase_invoice__isnull=True,  # دفعات مستقلة فقط
    ).order_by('date')

    return render(request, 'inventory/statement_print.html', {
        'entity': vendor,
        'entity_type': 'vendor',
        'invoices': invoices,
        'payments': payments,
        'print_date': timezone.now(),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def account_ledger_api(request, account_id):
    """دفتر أستاذ حساب محاسبي محدد"""
    account = get_object_or_404(ChartOfAccount, pk=account_id)
    entries = AccountingEntry.objects.filter(account=account).order_by('-entry_date')[:100]

    return _json_response_safe({
        "status": "success",
        "account": {"code": account.code, "name": account.name, "type": account.account_type, "balance": float(account.balance)},
        "entries": [
            {
                "date": str(e.entry_date),
                "reference": e.reference,
                "description": e.description,
                "debit": float(e.debit),
                "credit": float(e.credit),
            }
            for e in entries
        ]
    })

# =====================================================================
# 🏦 Bank Reconciliation Views
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def bank_reconciliation_dashboard(request):
    """لوحة المطابقة البنكية — قائمة الكشوف وحالتها."""
    from inventory.models import BankStatement
    statements = BankStatement.objects.select_related('treasury').order_by('-statement_date')[:50]
    stats = {
        'total': BankStatement.objects.count(),
        'reconciled': BankStatement.objects.filter(is_reconciled=True).count(),
        'pending': BankStatement.objects.filter(is_reconciled=False).count(),
    }
    return render(request, 'inventory/bank_reconciliation.html', {
        'statements': statements,
        'stats': stats,
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def bank_reconciliation_detail(request, statement_id):
    """تفاصيل كشف بنكي + سطوره + المطابقة."""
    from inventory.models import BankStatement
    statement = get_object_or_404(BankStatement.objects.select_related('treasury'), pk=statement_id)
    lines = statement.lines.select_related('matched_transaction').order_by('transaction_date')

    return render(request, 'inventory/bank_reconciliation_detail.html', {
        'statement': statement,
        'lines': lines,
        'matched_count': lines.filter(is_matched=True).count(),
        'unmatched_count': lines.filter(is_matched=False).count(),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
@csrf_exempt
def bank_reconciliation_auto_match(request, statement_id):
    """🤖 محاولة مطابقة كل السطور تلقائياً."""
    if request.method != 'POST':
        return _json_response_safe({"error": "POST only"}, 405)

    from inventory.models import BankStatement
    statement = get_object_or_404(BankStatement, pk=statement_id)

    matched = 0
    total_lines = 0
    for line in statement.lines.filter(is_matched=False):
        total_lines += 1
        if line.auto_match() > 0:
            matched += 1

    # If all lines matched, mark statement as reconciled
    if statement.lines.filter(is_matched=False).count() == 0:
        statement.is_reconciled = True
        statement.reconciled_at = timezone.now()
        statement.reconciled_by = request.user
        statement.save(update_fields=['is_reconciled', 'reconciled_at', 'reconciled_by'])

    return _json_response_safe({
        "status": "success",
        "matched": matched,
        "total": total_lines,
        "fully_reconciled": statement.is_reconciled,
        "message": f"تمت مطابقة {matched} من {total_lines} سطر",
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
@csrf_exempt
def bank_statement_upload(request):
    """رفع كشف بنكي (CSV) — يستخرج الأسطر تلقائياً."""
    from inventory.models import BankStatement, BankStatementLine, Treasury
    if request.method != 'POST':
        return _json_response_safe({"error": "POST only"}, 405)

    try:
        treasury_id = int(request.POST.get('treasury_id', 0))
        period_start = request.POST.get('period_start', '')
        period_end = request.POST.get('period_end', '')
        opening_balance = Decimal(request.POST.get('opening_balance', '0'))
        closing_balance = Decimal(request.POST.get('closing_balance', '0'))
    except (ValueError, Exception) as e:
        return _json_response_safe({"error": f"بيانات غير صالحة: {e}"}, 400)

    try:
        treasury = Treasury.objects.get(pk=treasury_id)
    except Treasury.DoesNotExist:
        return _json_response_safe({"error": "الخزينة غير موجودة"}, 404)

    csv_file = request.FILES.get('csv_file')
    if not csv_file:
        return _json_response_safe({"error": "يجب رفع ملف CSV"}, 400)

    # Parse CSV first before creating anything
    import csv, io
    csv_file.seek(0)
    try:
        decoded = csv_file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(decoded))
        parsed_rows = list(reader)
    except Exception as e:
        return _json_response_safe({"error": f"فشل قراءة ملف CSV: {e}"}, 400)

    # Reset file pointer so Django can save the full file
    csv_file.seek(0)

    # Atomic: create statement + all lines together
    try:
        with transaction.atomic():
            statement = BankStatement.objects.create(
                treasury=treasury,
                statement_date=timezone.now().date(),
                period_start=period_start,
                period_end=period_end,
                opening_balance=opening_balance,
                closing_balance=closing_balance,
                uploaded_file=csv_file,
            )

            line_count = 0
            for row in parsed_rows:
                try:
                    amount = Decimal(str(row.get('amount', '0')).replace(',', ''))
                    direction = 'credit' if amount > 0 else 'debit'
                    BankStatementLine.objects.create(
                        statement=statement,
                        transaction_date=row.get('date', timezone.now().date()),
                        description=row.get('description', '')[:300],
                        reference=row.get('reference', '')[:100],
                        amount=abs(amount),
                        direction=direction,
                    )
                    line_count += 1
                except Exception as e:
                    logger.warning(f"[BANK CSV] Skipped row: {e}")
    except Exception as e:
        return _json_response_safe({"error": f"فشل إنشاء الكشف: {e}"}, 500)

    return _json_response_safe({
        "status": "success",
        "statement_id": statement.pk,
        "lines_imported": line_count,
        "redirect": f"/system/bank-reconciliation/{statement.pk}/",
    })


# =====================================================================
# 💸 Commission Payout — outstanding-balance dashboard + pay action
# =====================================================================
# DMS Backlog #5. Replaces the Django-admin bulk action with a proper
# tenant-facing UI: list every employee with commission_balance > 0,
# branch-scoped treasury picker, per-employee checkboxes, single POST
# to settle. admin/manager only.
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def commission_dashboard(request):
    """GET /system/commissions/ — list outstanding commission balances.
    POST /system/commissions/ — settle selected employees from chosen treasury.
    """
    from ..models import EmployeeProfile, Treasury
    from ..services.treasury_service import TreasuryService

    branch = _get_branch_for_user(request.user)

    treasuries = Treasury.objects.filter(is_active=True).select_related('branch')
    if branch and not request.user.is_superuser:
        treasuries = treasuries.filter(branch=branch)

    if request.method == 'POST':
        from django.contrib import messages
        from django.core.exceptions import ValidationError
        from django.shortcuts import redirect

        treasury_id = request.POST.get('treasury_id', '').strip()
        employee_ids = request.POST.getlist('employee_ids')

        if not treasury_id or not employee_ids:
            messages.error(request, '❌ اختر الخزنة والموظفين المراد صرف عمولاتهم.')
            return redirect('inventory:commission_dashboard')

        treasury = treasuries.filter(pk=treasury_id).first()
        if not treasury:
            messages.error(request, '❌ الخزنة المحددة غير صالحة أو خارج فرعك.')
            return redirect('inventory:commission_dashboard')

        # Scope profiles to branch (and to non-zero balance — service double-checks)
        profiles = EmployeeProfile.objects.filter(pk__in=employee_ids)
        if branch and not request.user.is_superuser:
            profiles = profiles.filter(branch=branch)

        try:
            result = TreasuryService.pay_commissions(
                profiles, treasury=treasury, paid_by_user=request.user,
            )
            messages.success(
                request,
                f"✅ صُرفت عمولات {result['paid_count']} موظف بإجمالي "
                f"{result['total_paid']:,.2f} ج.م من خزنة «{result['treasury_name']}»."
            )
        except ValidationError as e:
            messages.error(request, f"❌ {e.messages[0]}")
        return redirect('inventory:commission_dashboard')

    # GET — list outstanding balances
    profiles_qs = (
        EmployeeProfile.objects
        .filter(commission_balance__gt=0)
        .select_related('user', 'branch')
        .order_by('-commission_balance')
    )
    if branch and not request.user.is_superuser:
        profiles_qs = profiles_qs.filter(branch=branch)

    rows = []
    total_outstanding = 0
    for p in profiles_qs:
        rows.append({
            'id': p.pk,
            'name': (p.user.get_full_name() or p.user.username) if p.user else f'#{p.pk}',
            'role': p.get_role_display(),
            'role_code': p.role,
            'branch': p.branch.name if p.branch_id else '—',
            'balance': p.commission_balance,
        })
        total_outstanding += p.commission_balance

    return render(request, 'inventory/commission_dashboard.html', {
        'rows': rows,
        'total_outstanding': total_outstanding,
        'treasuries': treasuries,
        'current_role': getattr(getattr(request.user, 'employee_profile', None), 'role', ''),
        'is_super_user': request.user.is_superuser,
    })
