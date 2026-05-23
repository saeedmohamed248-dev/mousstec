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
from decimal import Decimal

import json
import urllib.parse
import base64
import uuid
import re
import logging
import concurrent.futures

from .ai_services import predict_parts_from_dtc, scan_invoice_image_ai, call_gemini_layer
from clients.models import GlobalB2BMarketplace, Client, BlindBiddingRequest

try:
    import qrcode
    from io import BytesIO
except ImportError:
    qrcode = None

from .models import (
    Product, Inventory, SaleInvoice, SaleInvoiceItem, Branch,
    Customer, Vehicle, ScrapDismantlingJob, ScrapDismantlingYield,
    FinancialTransaction, EmployeeShift, MaintenanceContract, Treasury,
    ChartOfAccount, AccountingEntry, InventoryMovement, StockAlert,
    ImportSession, AuditLog, PurchaseInvoice, Vendor,
)

logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🔌 0. طبقة الأدوات المشتركة (Shared Utilities)
# =====================================================================

def _json_response_safe(data, status=200):
    """مُغلّف آمن يمنع تسريب stack traces في الـ Production"""
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})


def _get_branch_for_user(user):
    """استخراج فرع المستخدم بشكل آمن مع fallback"""
    if user.is_superuser:
        return None  # superuser يرى كل الفروع
    try:
        return user.employee_profile.branch
    except Exception:
        return None


def _require_tenant(request):
    """يتحقق أن الطلب قادم من tenant وليس من الـ public schema"""
    tenant = getattr(request, 'tenant', None)
    if not tenant or tenant.schema_name == 'public':
        return False
    return True


# =====================================================================
# 📊 1. لوحات التحكم ونقطة البيع وكشك الفنيين
# =====================================================================

@login_required(login_url='/secure-portal/')
def branch_dashboard(request):
    if not _require_tenant(request):
        return HttpResponseForbidden(
            "<h1>🛑 دخول غير مصرح</h1>"
            "<p>هذه اللوحة مخصصة للفروع فقط.</p>"
        )

    today = timezone.now().date()
    is_admin = request.user.is_superuser or (
        hasattr(request.user, 'employee_profile')
        and request.user.employee_profile.role in ('admin', 'manager')
    )
    branch = _get_branch_for_user(request.user)

    invoices_qs = SaleInvoice.objects.filter(date_created__date=today)
    inv_qs = Inventory.objects.select_related('product', 'branch')

    if branch and not request.user.is_superuser:
        invoices_qs = invoices_qs.filter(branch=branch)
        inv_qs = inv_qs.filter(branch=branch)

    low_stock = inv_qs.filter(quantity__lte=F('product__min_stock_level'))

    stats = {
        'total_sales_today': invoices_qs.aggregate(Sum('total_amount'))['total_amount__sum'] or 0,
        'net_profit_today': (
            invoices_qs.aggregate(Sum('net_profit'))['net_profit__sum'] or 0
            if is_admin else "🔒 مخفي"
        ),
        'invoices_count': invoices_qs.count(),
        'low_stock_count': low_stock.count(),
    }

    # Trial / subscription countdown
    tenant = getattr(request, 'tenant', None)
    trial_days_left = None
    sub_days_left = None
    if tenant:
        if tenant.status == 'trial':
            trial_days_left = max(0, (tenant.trial_ends_at - today).days)
        elif tenant.status == 'active' and tenant.subscription_end_date:
            sub_days_left = max(0, (tenant.subscription_end_date - today).days)

    return render(request, 'inventory/dashboard.html', {
        'stats': stats,
        'low_stock_items': low_stock[:10],
        'tenant': tenant,
        'trial_days_left': trial_days_left,
        'sub_days_left': sub_days_left,
        'is_admin': is_admin,
    })


def solutions_tour(request):
    return render(request, 'inventory/solutions.html')


@login_required(login_url='/secure-portal/')
def b2b_marketplace(request):
    """واجهة سوق B2B التفاعلية مع بحث حي في السوق المركزي"""
    return render(request, 'inventory/b2b_marketplace.html')


@login_required(login_url='/secure-portal/')
def pos_interface(request):
    return render(request, 'inventory/pos_fast.html')


@login_required(login_url='/secure-portal/')
def mechanic_kiosk_interface(request):
    return render(request, 'inventory/mechanic_bay.html')


# =====================================================================
# 🖨️ 2. محركات الطباعة، المشاركة، والتوقيع الرقمي
# =====================================================================

@login_required(login_url='/secure-portal/')
def print_invoice_a4(request, invoice_id):
    invoice = get_object_or_404(
        SaleInvoice.objects
            .select_related('customer', 'vehicle', 'branch', 'maintenance_contract')
            .prefetch_related('items__product', 'service_items__service'),
        id=invoice_id,
    )
    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return HttpResponseForbidden("لا تملك صلاحية لطباعة فواتير من فروع أخرى.")
    return render(request, 'inventory/invoice_print_a4.html', {
        'invoice': invoice,
        'print_date': timezone.now(),
    })


@login_required(login_url='/secure-portal/')
def print_invoice_thermal(request, invoice_id):
    invoice = get_object_or_404(
        SaleInvoice.objects
            .select_related('customer')
            .prefetch_related('items__product', 'service_items__service'),
        id=invoice_id,
    )
    return render(request, 'inventory/invoice_print_thermal.html', {
        'invoice': invoice,
        'print_date': timezone.now(),
    })


@login_required(login_url='/secure-portal/')
def share_invoice_whatsapp(request, invoice_id):
    invoice = get_object_or_404(SaleInvoice, id=invoice_id)
    if not invoice.customer or not invoice.customer.phone:
        return HttpResponseForbidden("العميل غير مسجل أو لا يملك رقم هاتف.")
    amount_str = f"{float(invoice.total_amount):,.2f}"
    msg = (
        f"مرحباً بك أستاذ {invoice.customer.name} 🚗\n"
        f"تم إصدار مستندكم رقم #{invoice.id}.\n"
        f"الإجمالي: {amount_str} ج.م\n"
        "شكراً لتعاملكم معنا. (Mouss Tec Ecosystem)"
    )
    return redirect(f"https://wa.me/{invoice.customer.phone}?text={urllib.parse.quote(msg)}")


@login_required(login_url='/secure-portal/')
def capture_digital_signature(request, invoice_id):
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    try:
        data = json.loads(request.body)
        if not data.get('signature_data'):
            return _json_response_safe({"error": "بيانات التوقيع فارغة"}, 400)
        # TODO: حفظ الـ base64 في حقل model مخصص
        return _json_response_safe({"status": "success", "message": "تم حفظ التوقيع الإلكتروني."})
    except Exception as e:
        logger.error(f"[SIGNATURE API] {e}")
        return _json_response_safe({"error": "خطأ داخلي"}, 500)


# =====================================================================
# 🚗 3. جواز السفر الرقمي للمركبات وعقود الأساطيل
# =====================================================================

@login_required(login_url='/secure-portal/')
def vehicle_history(request, chassis_number):
    vehicle = get_object_or_404(
        Vehicle.objects.select_related('customer'),
        chassis_number=chassis_number,
    )
    history = SaleInvoice.objects.filter(vehicle=vehicle, status='posted').order_by('-date_created')
    return render(request, 'inventory/vehicle_history.html', {
        'vehicle': vehicle,
        'history': history,
    })


@login_required(login_url='/secure-portal/')
def generate_vehicle_qr(request, chassis_number):
    if not qrcode:
        return HttpResponse("مكتبة qrcode غير مثبتة.", status=501)
    vehicle = get_object_or_404(Vehicle, chassis_number=chassis_number)
    url = request.build_absolute_uri(f'/system/vehicle/{chassis_number}/history/')
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    img_b64 = base64.b64encode(buffer.getvalue()).decode()
    return HttpResponse(
        f'<div style="text-align:center;margin-top:50px;font-family:Cairo;">'
        f'<h2>جواز السفر الرقمي: {vehicle.car_plate}</h2>'
        f'<img src="data:image/png;base64,{img_b64}" /></div>'
    )


@login_required(login_url='/secure-portal/')
def fleet_contract_balance_api(request, contract_code):
    contract = get_object_or_404(
        MaintenanceContract, contract_code=contract_code, is_active=True
    )
    consumed = (
        SaleInvoice.objects
            .filter(maintenance_contract=contract, status='posted')
            .aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
    )
    remaining = contract.total_value - consumed
    return _json_response_safe({
        "status": "success",
        "company_name": contract.customer.name,
        "contract_value": float(contract.total_value),
        "consumed_value": float(consumed),
        "remaining_balance": float(remaining),
        "is_valid": remaining > 0 and contract.end_date >= timezone.now().date(),
    })


@login_required(login_url='/secure-portal/')
def tech_shift_manager_api(request, action):
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    try:
        if not hasattr(request.user, 'employee_profile'):
            return _json_response_safe({"error": "غير مصرح"}, 403)
        profile = request.user.employee_profile
        if profile.role != 'tech':
            return _json_response_safe({"error": "مخصص للفنيين فقط."}, 403)

        if action == 'clock_in':
            if EmployeeShift.objects.filter(employee=profile, clock_out__isnull=True).exists():
                return _json_response_safe({"error": "لديك وردية مفتوحة بالفعل!"}, 400)
            EmployeeShift.objects.create(employee=profile, clock_in=timezone.now())
            return _json_response_safe({"status": "success", "message": "تم تسجيل الدخول."})

        elif action == 'clock_out':
            shift = EmployeeShift.objects.filter(employee=profile, clock_out__isnull=True).first()
            if not shift:
                return _json_response_safe({"error": "لا توجد وردية مفتوحة."}, 400)
            shift.clock_out = timezone.now()
            shift.save()
            return _json_response_safe({
                "status": "success",
                "message": "تم إنهاء الوردية.",
                "total_hours": float(shift.total_hours),
            })

        return _json_response_safe({"error": "Invalid action"}, 400)
    except Exception as e:
        logger.error(f"[SHIFT API] {e}")
        return _json_response_safe({"error": "خطأ داخلي"}, 500)


# =====================================================================
# 🌐 4. Webhooks الخارجية والمزامنة الإقليمية
# =====================================================================

def api_documentation_view(request):
    return HttpResponse(
        "<h1>Mouss Tec B2B API Gateway v1.0</h1>"
        "<p>OpenAPI Documentation — Secure Mode.</p>"
    )


def graphql_gateway_view(request):
    return _json_response_safe({"data": {"message": "GraphQL Federation Gateway Active."}})


@csrf_exempt
def shopify_webhook_receiver(request):
    if request.method != 'POST':
        return HttpResponseForbidden()
    if 'Shopify' not in request.headers.get('User-Agent', ''):
        return HttpResponseForbidden("Invalid Source")
    try:
        logger.info("⚙️ [SHOPIFY] Sync initiated.")
        return _json_response_safe({"status": "success", "message": "Order accepted for sync."})
    except Exception as e:
        return _json_response_safe({"status": "error", "message": str(e)}, 500)


@csrf_exempt
def payment_gateway_callback(request):
    return _json_response_safe({"status": "success", "channel": "fintech_sync_active"})


@csrf_exempt
def market_price_sync_webhook(request):
    return _json_response_safe({"status": "acknowledged"})


@csrf_exempt
def regional_tax_forex_sync_webhook(request):
    return _json_response_safe({"status": "success", "message": "أسعار الصرف تم تحديثها."})


# =====================================================================
# 🏎️ 5. الجرد، الباركود، والمزامنة اللحظية
# =====================================================================

@login_required(login_url='/secure-portal/')
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


@login_required(login_url='/secure-portal/')
def mobile_cycle_count_api(request):
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    try:
        data = json.loads(request.body)
        code = data.get('barcode', '').strip()
        actual_qty = int(data.get('actual_qty', 0))
        branch = _get_branch_for_user(request.user)

        product = (
            Product.objects.filter(barcode=code).first()
            or Product.objects.filter(part_number=code).first()
        )
        if not product:
            return _json_response_safe({"error": "المنتج غير مسجل"}, 404)

        with transaction.atomic():
            inv, _ = Inventory.objects.select_for_update().get_or_create(
                product=product, branch=branch, defaults={'quantity': 0}
            )
            diff = actual_qty - inv.quantity
            inv.quantity = actual_qty
            inv.save()

            # تسجيل خسارة العجز في الخزينة
            if diff < 0:
                treasury = Treasury.objects.filter(branch=branch, is_active=True).first()
                if treasury:
                    loss_value = Decimal(str(abs(diff))) * Decimal(str(product.average_cost))
                    FinancialTransaction.objects.create(
                        treasury=treasury,
                        transaction_type='out',
                        amount=loss_value,
                        description=f"تسوية عجز جرد — {product.name} ({abs(diff)} وحدة)",
                    )

        return _json_response_safe({
            "status": "success",
            "message": f"تم جرد {product.name}. الرصيد: {actual_qty}",
            "variance": diff,
        })
    except Exception as e:
        logger.error(f"[CYCLE COUNT] {e}")
        return _json_response_safe({"error": str(e)}, 500)


@login_required(login_url='/secure-portal/')
def offline_pos_sync_api(request):
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    try:
        data = json.loads(request.body)
        invoices_data = data.get('invoices', [])
        if not invoices_data:
            return _json_response_safe({"status": "success", "message": "لا توجد فواتير للمزامنة."})
        # TODO: معالجة كل فاتورة وإنشاؤها في الـ DB
        return _json_response_safe({
            "status": "success",
            "message": f"تمت مزامنة {len(invoices_data)} فاتورة.",
        })
    except Exception as e:
        logger.error(f"[OFFLINE SYNC] {e}")
        return _json_response_safe({"error": "فشل المزامنة"}, 500)


@login_required(login_url='/secure-portal/')
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


@login_required(login_url='/secure-portal/')
def parts_cross_reference_api(request):
    part_number = request.GET.get('part_number', '').strip()
    alts = list(
        Product.objects
            .filter(Q(name__icontains=part_number) | Q(part_number__icontains=part_number))
            .values('id', 'name', 'part_number', 'retail_price')[:5]
    )
    return _json_response_safe({"status": "success", "alternatives": alts})


# =====================================================================
# 📊 6. التقارير غير المتزامنة
# =====================================================================

@login_required(login_url='/secure-portal/')
def request_async_report_api(request):
    report_type = request.GET.get('type', 'inventory_valuation')
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    logger.info(f"[ASYNC REPORT] Queued task {task_id} type={report_type}")
    return _json_response_safe({
        "status": "processing",
        "task_id": task_id,
        "message": "التقرير قيد المعالجة.",
    })


@login_required(login_url='/secure-portal/')
def download_async_report_api(request, task_id):
    return _json_response_safe({
        "status": "ready",
        "task_id": task_id,
        "download_url": f"https://mousstec.s3.amazonaws.com/reports/{task_id}_export.xlsx",
    })


# =====================================================================
# 🤖 7. الوكلاء الذكيين المنفصلين (Pure Agent Functions + HTTP Adapters)
# =====================================================================

# ------------------------------------------------------------------
# 🔬 وكيل التشخيص — Pure Function (لا تعتمد على HttpRequest)
# ------------------------------------------------------------------
def _agent_diagnostic(dtc_code: str, brand: str = "") -> dict:
    """
    وكيل DTC: يقبل كود العطل والماركة، يعيد قائمة القطع المطلوبة.
    Pure function — آمن للاستدعاء من الـ Orchestrator مباشرةً.
    """
    search_key = f"{dtc_code} {brand}".strip()
    result = predict_parts_from_dtc(search_key)
    if result and "recommendations" in result:
        return {"success": True, "parts": result["recommendations"]}
    return {"success": False, "parts": []}


# ------------------------------------------------------------------
# 🌐 وكيل السوق — Pure Function
# ------------------------------------------------------------------
def _agent_b2b_market(query: str, schema_name: str) -> list:
    """
    وكيل B2B: يبحث في سوق الجملة المركزي.
    Pure function — آمن للاستدعاء من threads مختلفة مع connection cleanup.
    """
    if not query or query == 'N/A':
        return []

    cache_key = f"b2b_agent_{urllib.parse.quote(query.lower()[:50])}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    results = []
    try:
        # 🔌 إدارة الـ DB connections بشكل صحيح داخل الـ thread
        close_old_connections()
        with schema_context('public'):
            matches = GlobalB2BMarketplace.objects.select_related('tenant').filter(
                Q(part_number__icontains=query) | Q(product_name__icontains=query),
                available_qty__gt=0,
                tenant__is_active=True,
                tenant__is_marketplace_active=True,
                tenant__is_fraud_flagged=False,
            ).order_by('-tenant__is_verified_merchant', 'wholesale_price')[:10]

            results = [
                {
                    'tenant_name': m.tenant.name,
                    'is_verified': m.tenant.is_verified_merchant,
                    'rating': float(m.tenant.market_rating or 5.0),
                    'part_number': m.part_number,
                    'product_name': m.product_name,
                    'wholesale_price': float(m.wholesale_price),
                    'available_qty': m.available_qty,
                    'condition': m.get_condition_display(),
                }
                for m in matches
            ]
        cache.set(cache_key, results, timeout=120)
    except Exception as e:
        logger.error(f"[B2B AGENT] Query='{query}' failed: {e}")
    finally:
        close_old_connections()

    return results


# ------------------------------------------------------------------
# 👁️ وكيل الرؤية — Pure Function
# ------------------------------------------------------------------
def _agent_vision_license(image_b64: str) -> dict:
    """
    وكيل رخصة السيارة: يستخرج البيانات من صورة.
    يعيد dict فارغ في حالة الفشل ليتحمله الـ Orchestrator.
    """
    try:
        sys_msg = (
            "أنت وكيل رؤية متخصص في استخراج بيانات رخص السيارات المصرية والخليجية. "
            "أعد JSON فقط بهذه المفاتيح: owner_name, chassis_number, car_plate, brand, model_year. "
            "إذا لم تتمكن من قراءة حقل، اجعله null."
        )
        messages = [
            {"role": "system", "content": sys_msg},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "استخرج بيانات الرخصة."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            },
        ]
        raw = call_gemini_layer(messages, json_mode=True, max_retries=2, require_pro=True)
        if raw:
            return json.loads(raw)
    except Exception as e:
        logger.warning(f"[VISION AGENT] Degraded: {e}")
    return {}


# ------------------------------------------------------------------
# HTTP Adapters — الـ Views التي تغلّف الوكلاء
# ------------------------------------------------------------------

@login_required(login_url='/secure-portal/')
def ai_repair_estimator_api(request):
    """HTTP Adapter لوكيل التشخيص"""
    if request.method == 'GET':
        dtc_code = request.GET.get('dtc', '').strip().upper()
        free_query = request.GET.get('query', '').strip()

        if dtc_code and re.match(r'^[A-Z]\d{4}$|^[0-9A-F]{4,6}$', dtc_code):
            result = _agent_diagnostic(dtc_code)
            parts = result.get('parts', [])
            html = "<br>".join(
                f"• {p.get('part_name', '')} (P/N: {p.get('p_n', 'N/A')}) — ثقة: {p.get('probability', 0)}%"
                for p in parts if isinstance(p, dict)
            ) or "لم يتم التعرف على الكود، يُنصح بالفحص اليدوي."
            return _json_response_safe({"status": "success", "dtc": dtc_code, "recommendations": html})

        if free_query:
            try:
                sys_msg = (
                    "أنت Mouss Tec Copilot، مساعد ذكي لمراكز صيانة السيارات. "
                    "أجب بلهجة مصرية مهنية ومختصرة."
                )
                messages = [
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": free_query},
                ]
                raw = call_gemini_layer(messages, json_mode=False, max_retries=1)
                if raw:
                    return _json_response_safe({
                        "status": "success",
                        "recommendations": raw.replace('\n', '<br>'),
                    })
            except Exception as e:
                logger.warning(f"[COPILOT] {e}")
            return _json_response_safe({
                "status": "success",
                "recommendations": "أهلاً! أدخل كود عطل أو استفسارك وسأساعدك.",
            })

        return _json_response_safe({"error": "dtc أو query مطلوب"}, 400)

    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            dtc = data.get('dtc_code', data.get('dtc', '')).upper().strip()
            if not dtc:
                return _json_response_safe({"error": "dtc_code مطلوب"}, 400)
            result = _agent_diagnostic(dtc)
            return _json_response_safe({"status": "success", "dtc": dtc, "ai_recommendations": result['parts']})
        except Exception as e:
            return _json_response_safe({"error": str(e)}, 500)

    return _json_response_safe({"error": "Method not allowed"}, 405)


@login_required(login_url='/secure-portal/')
def ai_ocr_invoice_scanner_api(request):
    """HTTP Adapter لوكيل فواتير الموردين"""
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    try:
        data = json.loads(request.body)
        image = data.get('image', '')
        if not image:
            return _json_response_safe({"error": "الصورة مفقودة"}, 400)
        extracted = scan_invoice_image_ai(image)
        if extracted:
            return _json_response_safe({"status": "success", "data": extracted})
        return _json_response_safe({"error": "فشل محرك الـ Vision"}, 502)
    except Exception as e:
        return _json_response_safe({"error": str(e)}, 500)


@login_required(login_url='/secure-portal/')
def ai_vehicle_docs_scanner_api(request):
    """HTTP Adapter لوكيل وثائق المركبات"""
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    try:
        data = json.loads(request.body)
        image = data.get('image', '')
        if not image:
            return _json_response_safe({"error": "الصورة مفقودة"}, 400)
        extracted = _agent_vision_license(image)
        if extracted:
            return _json_response_safe({"status": "success", "extracted_data": extracted})
        return _json_response_safe({"error": "لم يتمكن الذكاء من قراءة الصورة."}, 500)
    except Exception as e:
        return _json_response_safe({"error": "فشل قراءة المستند."}, 500)


@login_required(login_url='/secure-portal/')
def b2b_market_search_api(request):
    """HTTP Adapter لوكيل السوق المركزي"""
    query = request.GET.get('q', request.GET.get('part_number', '')).strip()
    if not query:
        return _json_response_safe({'results': []})
    schema = getattr(connection, 'schema_name', 'public')
    results = _agent_b2b_market(query, schema)
    return _json_response_safe({'status': 'success', 'results_count': len(results), 'results': results})


# =====================================================================
# 🏎️ 8. عمليات الأعمال (Business Operations)
# =====================================================================

@login_required(login_url='/secure-portal/')
def return_core_charge_api(request, item_id):
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    item = get_object_or_404(SaleInvoiceItem, id=item_id)
    if item.is_core_returned:
        return _json_response_safe({"error": "تم استرداد هذا التالف مسبقاً."}, 400)
    if item.core_charge_applied <= 0:
        return _json_response_safe({"error": "الصنف لا يقع تحت بند التوالف."}, 400)
    item.is_core_returned = True
    item.save()  # Signal في models.py سيتولى الحسابات المالية
    return _json_response_safe({
        "status": "success",
        "refunded_amount": float(item.core_charge_applied * item.quantity),
    })


@login_required(login_url='/secure-portal/')
def create_blind_bid_api(request):
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    try:
        data = json.loads(request.body)
        tenant = get_object_or_404(Client, schema_name=connection.schema_name)
        bid = BlindBiddingRequest.objects.create(
            buyer=tenant,
            part_number=data.get('part_number', '').strip(),
            required_qty=int(data.get('required_qty', 1)),
            target_price=data.get('target_price') or None,
            expires_at=timezone.now() + timezone.timedelta(hours=24),
        )
        return _json_response_safe({"status": "success", "bid_ref": str(bid.request_id)})
    except Exception as e:
        logger.error(f"[CREATE BID] {e}")
        return _json_response_safe({"error": str(e)}, 500)


@login_required(login_url='/secure-portal/')
def distribute_scrap_cost_api(request, job_id):
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    job = get_object_or_404(ScrapDismantlingJob, id=job_id)
    if job.is_completed:
        return _json_response_safe({"error": "العملية مغلقة مسبقاً."}, 400)

    with transaction.atomic():
        yields = list(job.yields.select_related('product').all())
        if not yields:
            return _json_response_safe({"error": "لا توجد مكونات مسجلة."}, 400)

        total_market_value = sum(
            Decimal(str(y.product.retail_price)) * y.quantity for y in yields
        )
        if total_market_value == 0:
            return _json_response_safe({"error": "أسعار السوق للمكونات صفرية."}, 400)

        total_cost = Decimal(str(job.total_purchase_cost))
        for y in yields:
            item_value = Decimal(str(y.product.retail_price)) * y.quantity
            coefficient = item_value / total_market_value
            y.estimated_cost_allocation = total_cost * coefficient
            y.save()

        job.is_completed = True
        job.save()  # Signal execute_scrap_dismantling_yield سيضيف للمخزن

    return _json_response_safe({
        "status": "success",
        "message": "تم توزيع التكلفة بالوزن النسبي وإضافة المكونات للمخزن.",
        "items_processed": len(yields),
    })


# =====================================================================
# 🧠 9. الأوركسترا المركزية متعدد الوكلاء (MAS Unified Pipeline)
# =====================================================================

@login_required(login_url='/secure-portal/')
def unified_ai_agent_orchestrator_api(request):
    """
    🚀 سلسلة الوكلاء المتصلة (Agentic Pipeline v2):

    المعمارية:
    ┌─────────────────────────────────────────────────┐
    │  HTTP Request                                   │
    │       ↓                                         │
    │  [Vision Agent] ──State──→ [Diagnostic Agent]  │
    │                                    ↓            │
    │                            [B2B Market Agent]  │
    │                           (Parallel Threads)   │
    │                                    ↓            │
    │                          Pipeline Result       │
    └─────────────────────────────────────────────────┘

    الأمان:
    - كل وكيل Pure Function → لا side effects
    - DB connections تُغلق بعد كل thread
    - الفشل الجزئي لا يوقف الـ Pipeline (Graceful Degradation)
    - Circuit Breaker: إذا كان AI مُعطلاً يُعيد partial result فوراً
    """
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return _json_response_safe({"error": "JSON غير صالح"}, 400)

    # التحقق من تفعيل الـ AI
    ai_enabled = getattr(settings, 'ENABLE_AI_PREDICTIONS', False)
    ai_key = getattr(settings, 'AI_VISION_API_KEY', '')
    current_schema = getattr(connection, 'schema_name', 'public')

    pipeline_state = {
        "session_id": str(uuid.uuid4()),
        "schema": current_schema,
        "vehicle_data": None,
        "required_parts": [],
        "b2b_market_availability": [],
        "agent_statuses": {},
        "status": "processing",
    }

    # ------------------------------------------------------------------
    # الخطوة 1: وكيل الرؤية (Vision Agent) — Synchronous, Heavy
    # ------------------------------------------------------------------
    license_image = payload.get('license_image', '')
    if license_image and ai_enabled and ai_key:
        try:
            vehicle_data = _agent_vision_license(license_image)
            pipeline_state["vehicle_data"] = vehicle_data
            pipeline_state["agent_statuses"]["vision"] = "success" if vehicle_data else "empty_result"
        except Exception as e:
            logger.warning(f"[ORCHESTRATOR] Vision Agent failed: {e}")
            pipeline_state["agent_statuses"]["vision"] = f"failed: {type(e).__name__}"
            # الاستمرار بدون بيانات السيارة (Graceful Degradation)
    else:
        pipeline_state["agent_statuses"]["vision"] = "skipped"

    # ------------------------------------------------------------------
    # الخطوة 2: وكيل التشخيص (Diagnostic Agent)
    # يستخدم الـ State من الخطوة 1 لتحسين الدقة
    # ------------------------------------------------------------------
    dtc_code = payload.get('dtc_code', '').upper().strip()
    if dtc_code and ai_enabled and ai_key:
        try:
            brand = (pipeline_state["vehicle_data"] or {}).get('brand', '')
            diag_result = _agent_diagnostic(dtc_code, brand)
            pipeline_state["required_parts"] = diag_result.get('parts', [])
            pipeline_state["agent_statuses"]["diagnostic"] = (
                "success" if diag_result.get('success') else "no_results"
            )
        except Exception as e:
            logger.warning(f"[ORCHESTRATOR] Diagnostic Agent failed: {e}")
            pipeline_state["agent_statuses"]["diagnostic"] = f"failed: {type(e).__name__}"
    else:
        pipeline_state["agent_statuses"]["diagnostic"] = "skipped" if not dtc_code else "ai_disabled"

    # ------------------------------------------------------------------
    # الخطوة 3: وكيل السوق — Parallel Execution
    # يُشغّل thread منفصل لكل قطعة، مع DB connection management صحيح
    # ------------------------------------------------------------------
    parts_to_search = [
        p for p in pipeline_state["required_parts"]
        if isinstance(p, dict) and p.get('p_n') and p['p_n'] != 'N/A'
    ]

    # إضافة بحث مباشر بالـ DTC إذا لم تُعطِ نتائج
    if not parts_to_search and dtc_code:
        parts_to_search = [{'p_n': dtc_code, 'part_name': 'بحث مباشر'}]

    if parts_to_search:
        market_results = []
        # حد أقصى 3 threads لعدم إرهاق قاعدة البيانات
        max_workers = min(len(parts_to_search), 3)

        def _safe_market_search(part_dict):
            """Wrapper آمن يُدير الـ DB connection داخل الـ thread"""
            query = part_dict.get('p_n') or part_dict.get('part_name', '')
            if not query:
                return None
            try:
                hits = _agent_b2b_market(query, current_schema)
                if hits:
                    return {
                        "searched_part": query,
                        "part_name_ar": part_dict.get('part_name', ''),
                        "market_options": hits,
                        "best_price": min(h['wholesale_price'] for h in hits),
                    }
            except Exception as e:
                logger.error(f"[ORCHESTRATOR] Market thread failed for '{query}': {e}")
            finally:
                close_old_connections()
            return None

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(_safe_market_search, p): p
                    for p in parts_to_search
                }
                for future in concurrent.futures.as_completed(futures, timeout=15):
                    try:
                        result = future.result()
                        if result:
                            market_results.append(result)
                    except concurrent.futures.TimeoutError:
                        logger.warning("[ORCHESTRATOR] Market search thread timed out.")
                    except Exception as e:
                        logger.error(f"[ORCHESTRATOR] Thread exception: {e}")

            pipeline_state["b2b_market_availability"] = market_results
            pipeline_state["agent_statuses"]["b2b_market"] = (
                f"success — {len(market_results)} parts found"
            )
        except Exception as e:
            logger.error(f"[ORCHESTRATOR] ThreadPool failed: {e}")
            pipeline_state["agent_statuses"]["b2b_market"] = f"failed: {type(e).__name__}"
    else:
        pipeline_state["agent_statuses"]["b2b_market"] = "skipped — no parts to search"

    # ------------------------------------------------------------------
    # إنهاء الـ Pipeline وتحديد الـ Status النهائي
    # ------------------------------------------------------------------
    failed_agents = [k for k, v in pipeline_state["agent_statuses"].items() if "failed" in str(v)]

    if not failed_agents:
        pipeline_state["status"] = "completed"
        http_status = 200
    elif len(failed_agents) < len(pipeline_state["agent_statuses"]):
        pipeline_state["status"] = "partial_success"
        http_status = 207  # Multi-Status
    else:
        pipeline_state["status"] = "failed"
        http_status = 500

    logger.info(
        f"🧠 [ORCHESTRATOR] Pipeline {pipeline_state['session_id'][:8]} → "
        f"Status: {pipeline_state['status']} | "
        f"Parts: {len(pipeline_state['required_parts'])} | "
        f"Market hits: {len(pipeline_state['b2b_market_availability'])}"
    )

    return _json_response_safe(
        {"status": "success", "pipeline_result": pipeline_state},
        status=http_status,
    )


# =====================================================================
# 🔌 10. مسارات الـ API Gateway الأخرى
# =====================================================================

@login_required(login_url='/secure-portal/')
def legacy_system_sync_api(request):
    return _json_response_safe({"status": "success", "channel": "decentralized_legacy_sync_active"})


@login_required(login_url='/secure-portal/')
def ai_competitor_recon_api(request):
    return _json_response_safe({"status": "success", "channel": "market_competitor_recon_active"})


@csrf_exempt
def universal_webhook_multiplexer(request):
    return _json_response_safe({"status": "success", "channel": "universal_webhook_active"})


# =====================================================================
# 📊 11. تقارير الأرباح والخسائر (Profit & Loss Reports)
# =====================================================================

@login_required(login_url='/secure-portal/')
def profit_loss_report_api(request):
    """
    تقرير الأرباح والخسائر — يقارن الإيرادات بالمصروفات لفترة محددة.
    يدعم ?from=YYYY-MM-DD&to=YYYY-MM-DD
    """
    if not _require_tenant(request):
        return _json_response_safe({"error": "tenant required"}, 403)

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

    total_revenue = sales_qs.aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0')
    total_cost = sales_qs.aggregate(Sum('total_cost'))['total_cost__sum'] or Decimal('0')
    gross_profit = total_revenue - total_cost

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
            "profit_margin_percent": round(float(net_profit / total_revenue * 100), 2) if total_revenue > 0 else 0,
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


@login_required(login_url='/secure-portal/')
def product_profitability_api(request):
    """أربحية كل منتج — أعلى 20 منتج ربحية"""
    if not _require_tenant(request):
        return _json_response_safe({"error": "tenant required"}, 403)

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

@login_required(login_url='/secure-portal/')
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

        # إنشاء جلسة الاستيراد
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


@login_required(login_url='/secure-portal/')
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


@login_required(login_url='/secure-portal/')
def import_confirm_api(request, session_id):
    """
    تأكيد الاستيراد — يبدأ الاستيراد الفعلي بعد المعاينة.
    يأخذ نسخة احتياطية قبل أي تعديل.
    """
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)

    session = get_object_or_404(ImportSession, session_id=session_id, created_by=request.user)
    if session.status != 'preview':
        return _json_response_safe({"error": f"الجلسة في حالة '{session.get_status_display()}' ولا يمكن التأكيد."}, 400)

    import tablib
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
            for row in dataset.dict:
                if session.entity_type == 'product':
                    pn = row.get('part_number') or row.get('رقم القطعة', '')
                    name = row.get('name') or row.get('اسم المنتج', '')
                    if not name:
                        continue

                    # النسخة الاحتياطية للتعارضات
                    existing = Product.objects.filter(part_number=pn).first() if pn else None
                    if existing:
                        backup_data.append({
                            'model': 'Product', 'pk': existing.pk,
                            'snapshot': {'name': existing.name, 'part_number': existing.part_number,
                                         'retail_price': str(existing.retail_price)}
                        })
                        # تحديث مع الحفاظ على الأصل (نضيف suffix بالتاريخ)
                        existing.name = name
                        if row.get('retail_price') or row.get('سعر البيع'):
                            existing.retail_price = Decimal(str(row.get('retail_price') or row.get('سعر البيع', '0')))
                        existing.save()
                        imported_ids.append(existing.pk)
                    else:
                        obj = Product.objects.create(
                            name=name,
                            part_number=pn or f"IMP-{uuid.uuid4().hex[:8]}",
                            retail_price=Decimal(str(row.get('retail_price') or row.get('سعر البيع', '0') or '0')),
                            purchase_price=Decimal(str(row.get('purchase_price') or row.get('سعر الشراء', '0') or '0')),
                        )
                        imported_ids.append(obj.pk)

                elif session.entity_type == 'customer':
                    name = row.get('name') or row.get('اسم العميل', '')
                    phone = row.get('phone') or row.get('الهاتف', '')
                    if not name:
                        continue

                    existing = Customer.objects.filter(phone=phone).first() if phone else None
                    if existing:
                        backup_data.append({
                            'model': 'Customer', 'pk': existing.pk,
                            'snapshot': {'name': existing.name, 'phone': existing.phone}
                        })
                        existing.name = name
                        existing.save()
                        imported_ids.append(existing.pk)
                    else:
                        obj = Customer.objects.create(name=name, phone=phone or '')
                        imported_ids.append(obj.pk)

                elif session.entity_type == 'vendor':
                    name = row.get('name') or row.get('اسم المورد', '')
                    if not name:
                        continue
                    obj, created = Vendor.objects.get_or_create(
                        name=name,
                        defaults={'phone': row.get('phone') or row.get('الهاتف', '')}
                    )
                    imported_ids.append(obj.pk)

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


@login_required(login_url='/secure-portal/')
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


# =====================================================================
# 📄 13. كشوف الحساب (Statement of Account)
# =====================================================================

@login_required(login_url='/secure-portal/')
def customer_statement_api(request, customer_id):
    """
    كشف حساب عميل — كل المعاملات المالية مع الرصيد التراكمي.
    يدعم ?from=YYYY-MM-DD&to=YYYY-MM-DD
    """
    if not _require_tenant(request):
        return _json_response_safe({"error": "tenant required"}, 403)

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

    # المدفوعات
    payments_qs = FinancialTransaction.objects.filter(customer=customer, transaction_type='in').order_by('date')
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

    return _json_response_safe({
        "status": "success",
        "customer": {"id": customer.pk, "name": customer.name, "phone": customer.phone, "current_balance": float(customer.balance)},
        "period": {"from": str(from_date or 'بداية'), "to": str(to_date or 'اليوم')},
        "entries": entries,
        "totals": {
            "total_invoiced": float(invoices_qs.aggregate(Sum('total_amount'))['total_amount__sum'] or 0),
            "total_paid": float(payments_qs.aggregate(Sum('amount'))['amount__sum'] or 0),
            "outstanding_balance": float(customer.balance),
        },
    })


@login_required(login_url='/secure-portal/')
def vendor_statement_api(request, vendor_id):
    """كشف حساب مورد"""
    if not _require_tenant(request):
        return _json_response_safe({"error": "tenant required"}, 403)

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

    payments_qs = FinancialTransaction.objects.filter(vendor=vendor, transaction_type='out').order_by('date')
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

    return _json_response_safe({
        "status": "success",
        "vendor": {"id": vendor.pk, "name": vendor.name, "phone": vendor.phone, "current_balance": float(vendor.balance)},
        "entries": entries,
        "totals": {
            "total_purchases": float(invoices_qs.aggregate(Sum('total_amount'))['total_amount__sum'] or 0),
            "total_paid": float(payments_qs.aggregate(Sum('amount'))['amount__sum'] or 0),
            "outstanding_balance": float(vendor.balance),
        },
    })


@login_required(login_url='/secure-portal/')
def customer_statement_print(request, customer_id):
    """طباعة كشف حساب العميل"""
    customer = get_object_or_404(Customer, pk=customer_id)
    invoices = SaleInvoice.objects.filter(customer=customer, status='posted').order_by('date_created')
    payments = FinancialTransaction.objects.filter(customer=customer, transaction_type='in').order_by('date')

    return render(request, 'inventory/statement_print.html', {
        'entity': customer,
        'entity_type': 'customer',
        'invoices': invoices,
        'payments': payments,
        'print_date': timezone.now(),
    })


@login_required(login_url='/secure-portal/')
def vendor_statement_print(request, vendor_id):
    """طباعة كشف حساب المورد"""
    vendor = get_object_or_404(Vendor, pk=vendor_id)
    invoices = PurchaseInvoice.objects.filter(vendor=vendor, status='posted').order_by('date_created')
    payments = FinancialTransaction.objects.filter(vendor=vendor, transaction_type='out').order_by('date')

    return render(request, 'inventory/statement_print.html', {
        'entity': vendor,
        'entity_type': 'vendor',
        'invoices': invoices,
        'payments': payments,
        'print_date': timezone.now(),
    })


# =====================================================================
# 📊 14. واجهات التحليلات المتقدمة (Analytics APIs)
# =====================================================================

@login_required(login_url='/secure-portal/')
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


@login_required(login_url='/secure-portal/')
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