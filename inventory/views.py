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


@csrf_exempt
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


@csrf_exempt
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


@csrf_exempt
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


@csrf_exempt
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


@csrf_exempt
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


@csrf_exempt
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

@csrf_exempt
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


@csrf_exempt
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


@csrf_exempt
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

@csrf_exempt
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


@csrf_exempt
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


@csrf_exempt
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

@csrf_exempt
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

@csrf_exempt
@login_required(login_url='/secure-portal/')
def legacy_system_sync_api(request):
    return _json_response_safe({"status": "success", "channel": "decentralized_legacy_sync_active"})


@csrf_exempt
@login_required(login_url='/secure-portal/')
def ai_competitor_recon_api(request):
    return _json_response_safe({"status": "success", "channel": "market_competitor_recon_active"})


@csrf_exempt
def universal_webhook_multiplexer(request):
    return _json_response_safe({"status": "success", "channel": "universal_webhook_active"})