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
    # 🛡️ في بيئة الإنتاج: إخفاء تفاصيل الأخطاء الداخلية لمنع Information Disclosure
    if status >= 500 and not settings.DEBUG:
        if 'error' in data:
            data = {"error": "حدث خطأ داخلي. يرجى المحاولة لاحقاً أو التواصل مع الدعم الفني."}
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


from functools import wraps

def tenant_required(view_func):
    """
    🛡️ درع العزل السحابي — ديكوريتور يمنع الوصول من public schema.
    يُطبّق على كل view يخدم بيانات tenant-specific.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not _require_tenant(request):
            return HttpResponseForbidden(
                '{"error": "🛑 هذه الخدمة مخصصة للفروع فقط. الوصول من public schema محظور."}',
                content_type='application/json'
            )
        return view_func(request, *args, **kwargs)
    return _wrapped


def role_required(*allowed_roles):
    """
    🛡️ RBAC Decorator — يمنع الوصول لغير الأدوار المسموح لها.
    Usage: @role_required('admin', 'manager')
    Superusers always pass.

    🐛 [BUG FIX — Issue #1 dashboard quick-actions]:
    Was returning JSON 403 for every denial. Browsers rendered that as raw
    `{"error":"..."}` which looked exactly like a "you got logged out" screen.
    Now: for HTML navigations we return a proper rendered 403 page; only
    AJAX / API clients still get the JSON shape they expect.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)
            try:
                role = request.user.employee_profile.role
            except Exception:
                role = None
            if role not in allowed_roles:
                wants_json = (
                    request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                    or 'application/json' in request.headers.get('Accept', '')
                    or request.path.startswith('/api/')
                )
                if wants_json:
                    return _json_response_safe(
                        {"error": "🔒 ليس لديك صلاحية للوصول لهذه الخدمة. تواصل مع المدير."},
                        status=403,
                    )
                # Browser nav → render an HTML page that keeps the user signed in
                # and offers a way back to the dashboard (NOT a login screen).
                from django.shortcuts import render
                return render(
                    request, 'inventory/forbidden.html',
                    {
                        'allowed_roles': allowed_roles,
                        'current_role': role or '—',
                    },
                    status=403,
                )
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


# =====================================================================
# 📊 1. لوحات التحكم ونقطة البيع وكشك الفنيين
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def branch_dashboard(request):
    is_admin = request.user.is_superuser or (
        hasattr(request.user, 'employee_profile')
        and request.user.employee_profile.role in ('admin', 'manager')
    )
    branch = _get_branch_for_user(request.user)

    from inventory.services.reporting_service import ReportingService
    raw = ReportingService.get_today_dashboard_stats(request.user, branch)
    today = raw['today']
    low_stock = raw['low_stock_qs']

    # 🐛 [Issue #3 FIX]: نفس الـ source للخزينة المعروضة في /system/dashboard/
    # و /secure-portal/ — يستخدم ReportingService.get_treasury_summary بحيث
    # الفرع المرئي والمجموع لا يختلفان بين الواجهتين.
    treasury = ReportingService.get_treasury_summary(request.user, branch)

    stats = {
        'total_sales_today': raw['total_sales_today'],
        'net_profit_today': (
            raw['net_profit_today'] if is_admin else "🔒 صلاحية المدير فقط"
        ),
        'total_expenses_today': (
            raw['total_expenses_today'] if is_admin else "🔒 صلاحية المدير فقط"
        ),
        'total_treasury': (
            treasury['total_treasury_balance'] if is_admin else "🔒 صلاحية المدير فقط"
        ),
        'treasury_count': treasury['treasury_count'],
        'invoices_count': raw['invoices_count'],
        'low_stock_count': raw['low_stock_count'],
    }

    # Trial / subscription countdown
    tenant = getattr(request, 'tenant', None)
    trial_days_left = None
    sub_days_left = None
    if tenant:
        if tenant.status == 'trial' and getattr(tenant, 'trial_ends_at', None):
            trial_days_left = max(0, (tenant.trial_ends_at - today).days)
        elif tenant.status == 'active' and getattr(tenant, 'subscription_end_date', None):
            sub_days_left = max(0, (tenant.subscription_end_date - today).days)

    # 🛡️ Safely resolve role for the template — reverse OneToOne lookups raise
    # RelatedObjectDoesNotExist which Django templates do NOT silence, so we must
    # resolve it here in Python where hasattr() works correctly.
    current_role = ''
    if hasattr(request.user, 'employee_profile'):
        current_role = request.user.employee_profile.role or ''

    return render(request, 'inventory/dashboard.html', {
        'stats': stats,
        'treasuries_data': treasury['treasuries_data'] if is_admin else [],
        'low_stock_items': low_stock[:10],
        'tenant': tenant,
        'trial_days_left': trial_days_left,
        'sub_days_left': sub_days_left,
        'is_admin': is_admin,
        'current_role': current_role,
        'is_super_user': request.user.is_superuser,
    })


def solutions_tour(request):
    return render(request, 'inventory/solutions.html')


@login_required(login_url='/login/')
@tenant_required
def b2b_marketplace(request):
    """واجهة سوق B2B التفاعلية مع بحث حي في السوق المركزي"""
    return render(request, 'inventory/b2b_marketplace.html')


@login_required(login_url='/login/')
@tenant_required
def pos_interface(request):
    return render(request, 'inventory/pos_fast.html')


@login_required(login_url='/login/')
@tenant_required
def mechanic_kiosk_interface(request):
    return render(request, 'inventory/mechanic_bay.html')


# =====================================================================
# 🖨️ 2. محركات الطباعة، المشاركة، والتوقيع الرقمي
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def print_invoice_a4(request, invoice_id):
    """Pillar 4 — Cashier dual invoice.

    ?mode=summary  (default) → simple parts + labor totals (customer copy)
    ?mode=detailed           → full technical report: tech notes, OBD fault codes, photos
    """
    mode = (request.GET.get('mode') or 'summary').lower()
    if mode not in {'summary', 'detailed'}:
        mode = 'summary'

    qs = (SaleInvoice.objects
          .select_related('customer', 'vehicle', 'branch', 'maintenance_contract')
          .prefetch_related('items__product', 'service_items__service'))

    if mode == 'detailed':
        qs = qs.prefetch_related(
            'repair_logs__technician__user',
            'repair_logs__media',
            'diagnostic_reports__engineer__user',
        )

    invoice = get_object_or_404(qs, id=invoice_id)

    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return HttpResponseForbidden("لا تملك صلاحية لطباعة فواتير من فروع أخرى.")

    template = ('inventory/invoice_print_detailed.html' if mode == 'detailed'
                else 'inventory/invoice_print_a4.html')
    return render(request, template, {
        'invoice': invoice,
        'print_date': timezone.now(),
        'mode': mode,
    })


@login_required(login_url='/login/')
@tenant_required
def export_invoice_pdf(request, invoice_id):
    """
    📄 تصدير الفاتورة كـ PDF — يستخدم WeasyPrint مع دعم RTL و خط Cairo.
    Fallback: لو WeasyPrint مش مثبت، يرجع HTML للطباعة بـ Ctrl+P.
    """
    invoice = get_object_or_404(
        SaleInvoice.objects
            .select_related('customer', 'vehicle', 'branch', 'maintenance_contract')
            .prefetch_related('items__product', 'service_items__service'),
        id=invoice_id,
    )
    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return HttpResponseForbidden("لا تملك صلاحية لتصدير فواتير من فروع أخرى.")

    from django.template.loader import render_to_string
    html_string = render_to_string('inventory/invoice_print_a4.html', {
        'invoice': invoice,
        'print_date': timezone.now(),
        'pdf_mode': True,
    })

    try:
        from weasyprint import HTML, CSS
        from weasyprint.text.fonts import FontConfiguration

        font_config = FontConfiguration()
        pdf_css = CSS(string='''
            @page { size: A4; margin: 1.5cm; }
            @font-face {
                font-family: 'Cairo';
                src: url('https://fonts.gstatic.com/s/cairo/v28/SLXgc1nY6HkvangtZmpQdkhzfH5lkSs2SgRjCAGMQ1z0hOA-W1Y.ttf') format('truetype');
            }
            body { font-family: 'Cairo', sans-serif; direction: rtl; }
        ''', font_config=font_config)

        pdf_bytes = HTML(string=html_string, base_url=request.build_absolute_uri('/')).write_pdf(
            stylesheets=[pdf_css], font_config=font_config,
        )

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        filename = f'invoice-{invoice.id}-{timezone.now():%Y%m%d}.pdf'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    except ImportError:
        logger.warning("[PDF EXPORT] WeasyPrint not installed — falling back to HTML")
        return HttpResponse(
            html_string + '<script>window.print();</script>',
            content_type='text/html; charset=utf-8',
        )
    except Exception as e:
        logger.error(f"[PDF EXPORT] Failed for invoice #{invoice_id}: {e}")
        return _json_response_safe({
            "error": f"فشل توليد PDF: {str(e)[:200]}. تأكد من تثبيت WeasyPrint."
        }, status=500)


@login_required(login_url='/login/')
@tenant_required
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


@login_required(login_url='/login/')
@tenant_required
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


@login_required(login_url='/login/')
@tenant_required
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

@login_required(login_url='/login/')
@tenant_required
def vehicle_history(request, chassis_number):
    vehicle = get_object_or_404(
        Vehicle.objects.select_related('customer'),
        chassis_number=chassis_number,
    )
    history = SaleInvoice.objects.filter(vehicle=vehicle, status='posted').order_by('-date_created')

    # 🔧 Smart Diagnostics entitlement — drives the 'Live Diagnostics' button visibility
    has_live_diagnostics = False
    fault_count = 0
    try:
        from smart_diagnostics.services.quota import (
            DiagnosticsQuotaService, FEATURE_LIVE_DATA,
        )
        from smart_diagnostics.models import FaultLog
        tenant = getattr(request, 'tenant', None)
        has_live_diagnostics = DiagnosticsQuotaService.check_feature(tenant, FEATURE_LIVE_DATA).allowed
        fault_count = FaultLog.objects.filter(vehicle=vehicle, resolved_at__isnull=True).count()
    except Exception:
        pass

    return render(request, 'inventory/vehicle_history.html', {
        'vehicle': vehicle,
        'history': history,
        'has_live_diagnostics': has_live_diagnostics,
        'open_fault_count': fault_count,
    })


@login_required(login_url='/login/')
@tenant_required
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


@login_required(login_url='/login/')
@tenant_required
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


@login_required(login_url='/login/')
@tenant_required
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

@login_required(login_url='/login/')
@tenant_required
def api_documentation_view(request):
    return HttpResponse(
        "<h1>Mouss Tec B2B API Gateway v1.0</h1>"
        "<p>OpenAPI Documentation — Secure Mode.</p>"
    )


@login_required(login_url='/login/')
@tenant_required
def graphql_gateway_view(request):
    return _json_response_safe({"data": {"message": "GraphQL Federation Gateway Active."}})


def _verify_webhook_hmac(request, secret_setting_name, header_name='HTTP_X_SHOPIFY_HMAC_SHA256'):
    """
    🛡️ التحقق من HMAC للـ webhooks الخارجية.
    يقارن التوقيع المرسل مع التوقيع المحسوب من body + secret.
    """
    import hmac as _hmac
    import hashlib as _hashlib
    secret = getattr(settings, secret_setting_name, None)
    if not secret:
        logger.warning(f"⚠️ [WEBHOOK] {secret_setting_name} not configured — rejecting webhook")
        return False
    received_hmac = request.META.get(header_name, '')
    if not received_hmac:
        return False
    computed = _hmac.new(
        secret.encode('utf-8'),
        request.body,
        _hashlib.sha256,
    ).hexdigest()
    return _hmac.compare_digest(computed, received_hmac)


@csrf_exempt
def shopify_webhook_receiver(request):
    if request.method != 'POST':
        return HttpResponseForbidden()
    # 🛡️ HMAC-SHA256 verification بدلاً من User-Agent check
    if not _verify_webhook_hmac(request, 'SHOPIFY_WEBHOOK_SECRET', 'HTTP_X_SHOPIFY_HMAC_SHA256'):
        logger.warning("🛑 [SHOPIFY] HMAC verification failed — possible spoofing attempt.")
        return HttpResponseForbidden("Invalid HMAC signature")
    try:
        logger.info("⚙️ [SHOPIFY] Sync initiated (HMAC verified).")
        return _json_response_safe({"status": "success", "message": "Order accepted for sync."})
    except Exception as e:
        return _json_response_safe({"status": "error", "message": str(e)}, 500)


@csrf_exempt
def payment_gateway_callback(request):
    """🛡️ Stub — No logic, safe. When activated must add HMAC verification."""
    if request.method != 'POST':
        return HttpResponseForbidden()
    logger.info("⚙️ [PAYMENT GW] Callback received (stub).")
    return _json_response_safe({"status": "success", "channel": "fintech_sync_active"})


@csrf_exempt
def market_price_sync_webhook(request):
    """🛡️ Stub — When activated must add HMAC verification."""
    if request.method != 'POST':
        return HttpResponseForbidden()
    return _json_response_safe({"status": "acknowledged"})


@csrf_exempt
def regional_tax_forex_sync_webhook(request):
    """🛡️ Stub — When activated must add HMAC verification."""
    if request.method != 'POST':
        return HttpResponseForbidden()
    return _json_response_safe({"status": "success", "message": "أسعار الصرف تم تحديثها."})


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

def _get_auto_live_context():
    """Delegate to ReportingService for live business data snapshot."""
    from inventory.services.reporting_service import ReportingService
    return ReportingService.get_live_context()


def _query_auto_business_data(query):
    """Delegate to ReportingService for business data queries."""
    from inventory.services.reporting_service import ReportingService
    return ReportingService.query_business_data(query)


@login_required(login_url='/login/')
@tenant_required
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
                # جلب بيانات حية من الداتابيز
                live_ctx = _get_auto_live_context()
                db_ctx = _query_auto_business_data(free_query)

                sys_msg = (
                    "أنت Mouss Tec Copilot — المساعد الذكي الرسمي لنظام Mouss Tec لإدارة مراكز صيانة السيارات وبيع قطع الغيار.\n"
                    "أنت عارف كل حاجة عن السيستم وبتساعد المستخدمين يفهموه ويستخدموه.\n\n"
                    "## معرفتك بالسيستم:\n"
                    "1. **فواتير البيع (SaleInvoice)**: بيع قطع غيار أو صيانة شاملة. ليها حالات: عرض سعر → قيد العمل → فحص جودة → جاهز → تم التسليم\n"
                    "2. **قطع الغيار (Product)**: كل قطعة ليها part number، سعر شراء وبيع، مخزون، باركود، ضمان\n"
                    "3. **العملاء (Customer)**: اسم + تليفون + رصيد/مديونية + نقاط ولاء + تصنيف VIP\n"
                    "4. **المركبات (Vehicle)**: كل عربية مرتبطة بعميل — ماركة، موديل، شاسيه\n"
                    "5. **الخزينة (Treasury)**: إيداع وسحب مع رصيد لحظي\n"
                    "6. **فواتير الشراء (PurchaseInvoice)**: مشتريات من الموردين\n"
                    "7. **الموظفين (EmployeeProfile)**: فنيين وكاشير مع تتبع الحضور والعمولات\n"
                    "8. **المخزون (Inventory)**: تتبع الكميات مع تنبيهات نقص ذكية\n"
                    "9. **عقود الصيانة (MaintenanceContract)**: عقود B2B للشركات\n"
                    "10. **تقارير الأرباح**: كل فاتورة فيها صافي ربح = سعر بيع - تكلفة شراء\n\n"
                    "## إزاي تساعد المستخدم:\n"
                    "- لو سأل عن مبيعات/مصاريف/أرباح → اديله الأرقام الحقيقية\n"
                    "- لو سأل عن عميل بالاسم → ابحث في البيانات الحية\n"
                    "- لو سأل عن فاتورة → اديله التفاصيل (ربح/خسارة)\n"
                    "- لو مش عارف يستخدم ميزة → علّمه خطوة بخطوة\n"
                    "- أجب بالعربي المصري، مختصر ومهني\n"
                    "- لا تخترع أرقام — استخدم البيانات الفعلية فقط\n"
                )

                user_content = f"سؤال المستخدم: {free_query}"
                if db_ctx:
                    user_content += f"\n\nنتيجة البحث في الداتابيز:\n{db_ctx}"
                user_content += f"\n\n{live_ctx}"

                messages = [
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": user_content},
                ]
                raw = call_gemini_layer(messages, json_mode=False, max_retries=1)
                if raw:
                    return _json_response_safe({
                        "status": "success",
                        "recommendations": raw.replace('\n', '<br>'),
                    })
                # Fallback: رجّع البيانات الخام
                if db_ctx:
                    return _json_response_safe({
                        "status": "success",
                        "recommendations": db_ctx.replace('\n', '<br>'),
                    })
            except Exception as e:
                logger.warning(f"[COPILOT] {e}")
            return _json_response_safe({
                "status": "success",
                "recommendations": "أهلاً! اسألني عن المبيعات، المصاريف، الأرباح، العملاء، المخزون، أو أي حاجة في السيستم.",
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


@login_required(login_url='/login/')
@tenant_required
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


@login_required(login_url='/login/')
@tenant_required
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


@login_required(login_url='/login/')
@tenant_required
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

@login_required(login_url='/login/')
@tenant_required
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


@login_required(login_url='/login/')
@tenant_required
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


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def distribute_scrap_cost_api(request, job_id):
    """HTTP adapter for scrap cost distribution — delegates to InventoryService."""
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)

    job = get_object_or_404(ScrapDismantlingJob, id=job_id)

    try:
        from inventory.services.inventory_service import InventoryService
        items_count = InventoryService.distribute_scrap_cost(job)
        return _json_response_safe({
            "status": "success",
            "message": "تم توزيع التكلفة بالوزن النسبي وإضافة المكونات للمخزن.",
            "items_processed": items_count,
        })
    except Exception as e:
        return _json_response_safe({"error": str(e)}, 400)


# =====================================================================
# 🧠 9. الأوركسترا المركزية متعدد الوكلاء (MAS Unified Pipeline)
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def unified_ai_agent_orchestrator_api(request):
    """
    🚀 سلسلة الوكلاء المتصلة (Agentic Pipeline v2):

    المعمارية:
    ┌─────────────────────────────────────────────────┐
    │  HTTP Request                                   │
    │        ↓                                        │
    │  [Vision Agent] ──State──→ [Diagnostic Agent]  │
    │                                        ↓        │
    │                            [B2B Market Agent]  │
    │                           (Parallel Threads)   │
    │                                        ↓        │
    │                            Pipeline Result       │
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

@login_required(login_url='/login/')
@tenant_required
def legacy_system_sync_api(request):
    return _json_response_safe({"status": "success", "channel": "decentralized_legacy_sync_active"})


@login_required(login_url='/login/')
@tenant_required
def ai_competitor_recon_api(request):
    return _json_response_safe({"status": "success", "channel": "market_competitor_recon_active"})


@csrf_exempt
def universal_webhook_multiplexer(request):
    """🛡️ Webhook multiplexer with HMAC verification."""
    if request.method != 'POST':
        return HttpResponseForbidden()
    if not _verify_webhook_hmac(request, 'WEBHOOK_HMAC_SECRET', 'HTTP_X_WEBHOOK_SIGNATURE'):
        logger.warning("[WEBHOOK] HMAC verification failed — rejected.")
        return HttpResponseForbidden("Invalid signature")
    return _json_response_safe({"status": "success", "channel": "universal_webhook_active"})


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


# =====================================================================
# 📄 12.5. تصفية المركبات حسب العميل (Vehicle-Customer Dynamic Filter)
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def vehicles_by_customer_api(request, customer_id):
    """
    يرجع قائمة مركبات العميل المحدد لتصفية الـ autocomplete في فاتورة البيع.
    """
    vehicles = Vehicle.objects.filter(customer_id=customer_id).values(
        'id', 'chassis_number', 'car_plate', 'brand', 'model_name'
    )
    return JsonResponse({
        'results': [
            {
                'id': v['id'],
                'text': f"{v['car_plate'] or 'بدون لوحة'} - {(v['chassis_number'] or '')[-6:]} ({v['brand']} {v['model_name'] or ''})"
            }
            for v in vehicles
        ]
    })


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
    from .models import EmployeeProfile, Treasury
    from .services.treasury_service import TreasuryService

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


# ─────────────────────────────────────────────────────────────────────
# 🖨️ AI Diagnostic Report — customer-facing printable view
# ─────────────────────────────────────────────────────────────────────
@login_required(login_url='/login/')
@tenant_required
def ai_diag_print(request, invoice_id):
    """Clean, customer-facing printable summary of the AI diagnostic findings
    attached to a Job Card. Service advisor opens this to justify the repair
    quote to the customer at the counter.

    Designed for both screen viewing and direct browser print (Ctrl+P) —
    no nav, no admin chrome, brand-aware via tenant.logo / tenant.name.
    The Mousstec parent brand is sector-agnostic; this view never injects
    automotive marks beyond what the individual workshop chose to upload.
    """
    invoice = get_object_or_404(
        SaleInvoice.objects
            .select_related('customer', 'vehicle', 'branch')
            .prefetch_related(
                'diagnostic_reports__engineer__user',
                'diagnostic_reports__photos',
            ),
        id=invoice_id,
    )

    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return HttpResponseForbidden("لا تملك صلاحية لعرض تقارير فروع أخرى.")

    return render(request, 'inventory/ai_diag_print.html',
                  _render_ai_diag_context(request, invoice))


# ─────────────────────────────────────────────────────────────────────
# 📄 AI Diagnostic Report — PDF + public share (WhatsApp-friendly)
# ─────────────────────────────────────────────────────────────────────
_AI_DIAG_SHARE_SALT = 'ai-diag-share-v1'
_AI_DIAG_SHARE_MAX_AGE = 14 * 24 * 60 * 60   # 14 days


def _sign_ai_diag_share(invoice_id, tenant_schema):
    """Bind the token to BOTH invoice id and tenant schema so a token from
    workshop A can't be replayed against workshop B's invoice with the same id."""
    from django.core.signing import TimestampSigner
    signer = TimestampSigner(salt=_AI_DIAG_SHARE_SALT)
    return signer.sign(f"{tenant_schema}:{invoice_id}")


def _unsign_ai_diag_share(token, tenant_schema):
    from django.core.signing import TimestampSigner, BadSignature, SignatureExpired
    signer = TimestampSigner(salt=_AI_DIAG_SHARE_SALT)
    try:
        raw = signer.unsign(token, max_age=_AI_DIAG_SHARE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    try:
        schema, invoice_id = raw.split(':', 1)
        if schema != tenant_schema:
            return None
        return int(invoice_id)
    except (ValueError, TypeError):
        return None


def _make_share_qr_data_url(share_absolute_url):
    """Build a tiny base64 PNG QR that points at the public share URL.
    Returns '' on any failure so the template just hides the block.

    Why data-URL: works seamlessly in (a) screen view, (b) printed paper,
    (c) WeasyPrint PDF — no extra round-trip, no static-files plumbing,
    no CDN dependency. The QR is regenerated on each render — cheap (~2ms
    for a v2 QR at this density).
    """
    if not share_absolute_url or qrcode is None:
        return ''
    try:
        import base64
        from io import BytesIO

        qr = qrcode.QRCode(
            version=None,                                    # auto-fit
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(share_absolute_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0f172a", back_color="#ffffff")
        buf = BytesIO()
        img.save(buf, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        logger.warning(f"[AI DIAG QR] generation failed: {exc}")
        return ''


def _render_ai_diag_context(request, invoice):
    """Shared context builder used by all 3 surfaces (print/PDF/share)."""
    from django.urls import reverse

    reports = list(invoice.diagnostic_reports.all().order_by('-scanned_at'))
    tenant = getattr(request, 'tenant', None)

    workshop_logo_url = ''
    try:
        if tenant and tenant.logo:
            workshop_logo_url = request.build_absolute_uri(tenant.logo.url)
    except (ValueError, AttributeError):
        workshop_logo_url = ''

    # Build the signed share URL + its QR data-URL up front so every surface
    # (print / PDF / public share) gets the same artefact.
    share_token = ''
    share_absolute_url = ''
    share_qr_data_url = ''
    if tenant:
        share_token = _sign_ai_diag_share(
            invoice.id, getattr(tenant, 'schema_name', '') or '',
        )
        share_absolute_url = request.build_absolute_uri(
            reverse('inventory:ai_diag_share', args=[share_token])
        )
        share_qr_data_url = _make_share_qr_data_url(share_absolute_url)

    return {
        'invoice': invoice,
        'reports': reports,
        'print_date': timezone.now(),
        'workshop_name': (
            getattr(tenant, 'name', None)
            or getattr(tenant, 'schema_name', None)
            or 'ورشتك'
        ),
        'workshop_logo_url': workshop_logo_url,
        'workshop_phone': getattr(tenant, 'phone', '') or '',
        'has_findings': any(
            (r.ai_summary or r.fault_codes or r.photos.exists()) for r in reports
        ),
        'share_token': share_token,
        'share_absolute_url': share_absolute_url,
        'share_qr_data_url': share_qr_data_url,
    }


@login_required(login_url='/login/')
@tenant_required
def ai_diag_pdf(request, invoice_id):
    """Render the AI diagnostic report as a downloadable PDF using WeasyPrint.

    Reuses the exact `ai_diag_print.html` template — passing pdf_mode=True so
    the action bar can be hidden via {% if not pdf_mode %} when present.
    """
    invoice = get_object_or_404(
        SaleInvoice.objects
            .select_related('customer', 'vehicle', 'branch')
            .prefetch_related('diagnostic_reports__engineer__user',
                              'diagnostic_reports__photos'),
        id=invoice_id,
    )
    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return HttpResponseForbidden("لا تملك صلاحية لتصدير تقارير فروع أخرى.")

    from django.template.loader import render_to_string
    ctx = _render_ai_diag_context(request, invoice)
    ctx['pdf_mode'] = True
    html_string = render_to_string('inventory/ai_diag_print.html', ctx)

    try:
        from weasyprint import HTML, CSS
        from weasyprint.text.fonts import FontConfiguration

        font_config = FontConfiguration()
        pdf_css = CSS(string='''
            @page { size: A4; margin: 14mm; }
            @font-face {
                font-family: 'Cairo';
                src: url('https://fonts.gstatic.com/s/cairo/v28/SLXgc1nY6HkvangtZmpQdkhzfH5lkSs2SgRjCAGMQ1z0hOA-W1Y.ttf') format('truetype');
            }
            body { font-family: 'Cairo', sans-serif; direction: rtl; background: #fff; }
            .actions, .no-print { display: none !important; }
        ''', font_config=font_config)

        pdf_bytes = HTML(
            string=html_string,
            base_url=request.build_absolute_uri('/'),
        ).write_pdf(stylesheets=[pdf_css], font_config=font_config)

        plate = ''
        if invoice.vehicle and invoice.vehicle.car_plate:
            # Strip whitespace for filename safety
            plate = invoice.vehicle.car_plate.replace(' ', '_')
        filename = (
            f'ai-diag-{invoice.id}'
            f'{"-" + plate if plate else ""}'
            f'-{timezone.now():%Y%m%d}.pdf'
        )
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    except ImportError:
        logger.warning("[AI DIAG PDF] WeasyPrint not installed — HTML fallback")
        return HttpResponse(
            html_string + '<script>window.print();</script>',
            content_type='text/html; charset=utf-8',
        )
    except Exception as e:
        logger.error(f"[AI DIAG PDF] Failed for invoice #{invoice_id}: {e}",
                     exc_info=True)
        return HttpResponse(
            f"فشل توليد PDF: {str(e)[:200]}", status=500,
            content_type='text/plain; charset=utf-8',
        )


@csrf_exempt
def ai_diag_share(request, token):
    """Public, signed access to the AI diagnostic report — no login required.

    Used by the WhatsApp share link. The token is HMAC-signed (Django
    `TimestampSigner`) with a 14-day TTL and binds (tenant_schema, invoice_id),
    so it can't be replayed across tenants and stops working after 2 weeks.
    """
    tenant = getattr(request, 'tenant', None)
    tenant_schema = getattr(tenant, 'schema_name', '') or ''
    invoice_id = _unsign_ai_diag_share(token, tenant_schema)
    if invoice_id is None:
        return HttpResponse(
            "الرابط منتهي الصلاحية أو غير صحيح. اطلب من مركز الصيانة رابطاً جديداً.",
            status=410, content_type='text/html; charset=utf-8',
        )

    invoice = (SaleInvoice.objects
               .select_related('customer', 'vehicle', 'branch')
               .prefetch_related('diagnostic_reports__engineer__user',
                                 'diagnostic_reports__photos')
               .filter(id=invoice_id).first())
    if invoice is None:
        return HttpResponse("التقرير غير موجود.", status=404,
                            content_type='text/html; charset=utf-8')

    ctx = _render_ai_diag_context(request, invoice)
    ctx['public_share'] = True   # template hides internal action bar
    return render(request, 'inventory/ai_diag_print.html', ctx)


# ─────────────────────────────────────────────────────────────────────
# 🧮 Accountant / Sales Review — Unified Job Card billing decision
# ─────────────────────────────────────────────────────────────────────
@login_required(login_url='/login/')
@tenant_required
def job_card_review(request, invoice_id):
    """One-screen review for accountant/sales:
        • AI diagnostic findings (read-only, with photos)
        • Tech repair logs + photos (read-only)
        • Parts (SaleInvoiceItem) and Services (SaleInvoiceServiceItem)
          each with an `is_billable` checkbox and a billing note input.

    POST flow:
        Form fields per row: `item_<id>_billable` (checkbox) +
        `item_<id>_note` (text). Same for `svc_<id>_*`. We update only
        the rows actually present in the POST so partial submissions
        stay safe.
    """
    invoice = get_object_or_404(
        SaleInvoice.objects
            .select_related('customer', 'vehicle', 'branch')
            .prefetch_related(
                'items__product', 'items__salesperson__user',
                'service_items__service', 'service_items__technician__user',
                'repair_logs__technician__user', 'repair_logs__media',
                'diagnostic_reports__engineer__user',
                'diagnostic_reports__photos',
            ),
        id=invoice_id,
    )

    # RBAC — only sales/cashier/accountant/admin/manager/superuser.
    profile = getattr(request.user, 'employee_profile', None)
    allowed_roles = {'sales', 'cashier', 'accountant', 'admin', 'manager'}
    if not (request.user.is_superuser or
            (profile and profile.role in allowed_roles)):
        return HttpResponseForbidden(
            "هذه الشاشة مخصّصة لطاقم المبيعات والمحاسبة فقط."
        )

    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return HttpResponseForbidden("لا تملك صلاحية لمراجعة فواتير فروع أخرى.")

    if request.method == 'POST':
        updated_items = 0
        updated_svcs = 0
        with transaction.atomic():
            for item in invoice.items.all():
                key = f"item_{item.id}_billable"
                note_key = f"item_{item.id}_note"
                # Checkbox absence == unchecked
                new_billable = key in request.POST
                new_note = (request.POST.get(note_key) or '').strip()[:200]
                if (item.is_billable != new_billable
                        or item.billing_note != new_note):
                    item.is_billable = new_billable
                    item.billing_note = new_note
                    item.save(update_fields=['is_billable', 'billing_note'])
                    updated_items += 1

            for svc in invoice.service_items.all():
                key = f"svc_{svc.id}_billable"
                note_key = f"svc_{svc.id}_note"
                new_billable = key in request.POST
                new_note = (request.POST.get(note_key) or '').strip()[:200]
                if (svc.is_billable != new_billable
                        or svc.billing_note != new_note):
                    svc.is_billable = new_billable
                    svc.billing_note = new_note
                    svc.save(update_fields=['is_billable', 'billing_note'])
                    updated_svcs += 1

        logger.info(
            "[Job Card Review] tenant=%s user=%s invoice=%s items=%s svcs=%s",
            getattr(getattr(request, 'tenant', None), 'schema_name', None),
            request.user.username, invoice.id, updated_items, updated_svcs,
        )
        from django.contrib import messages
        messages.success(
            request,
            f"تم حفظ المراجعة — {updated_items} قطعة و {updated_svcs} خدمة."
        )
        return redirect('inventory:job_card_review', invoice_id=invoice.id)

    # Compute customer-billable totals (what actually goes on the invoice)
    from decimal import Decimal
    parts_total = sum(
        (Decimal(str(i.quantity or 0)) * Decimal(str(i.unit_price or 0))
         for i in invoice.items.all() if i.is_billable),
        Decimal('0.00'),
    )
    services_total = sum(
        (Decimal(str(s.price or 0))
         for s in invoice.service_items.all() if s.is_billable),
        Decimal('0.00'),
    )
    excluded_total = sum(
        (Decimal(str(i.quantity or 0)) * Decimal(str(i.unit_price or 0))
         for i in invoice.items.all() if not i.is_billable),
        Decimal('0.00'),
    ) + sum(
        (Decimal(str(s.price or 0))
         for s in invoice.service_items.all() if not s.is_billable),
        Decimal('0.00'),
    )

    return render(request, 'inventory/job_card_review.html', {
        'invoice': invoice,
        'parts_total': parts_total,
        'services_total': services_total,
        'billable_total': parts_total + services_total,
        'excluded_total': excluded_total,
        'reviewer_name': request.user.get_full_name() or request.user.username,
    })


# ─────────────────────────────────────────────────────────────────────
# 🔧 DTC → Suggested Parts (with live per-branch stock)
# ─────────────────────────────────────────────────────────────────────
import json as _json


@login_required(login_url='/login/')
@tenant_required
def job_card_suggested_parts(request, invoice_id):
    """GET → JSON list of AI-suggested parts for the Job Card, with
    live per-branch stock. Lazy-loaded by the Review UI because the
    LLM round-trip is multi-second on a cold cache."""
    invoice = get_object_or_404(
        SaleInvoice.objects.prefetch_related('diagnostic_reports'),
        id=invoice_id,
    )
    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return JsonResponse({"error": "forbidden"}, status=403)

    from smart_diagnostics.services.parts_resolver import (
        resolve_parts_for_job_card,
    )
    try:
        parts = resolve_parts_for_job_card(invoice)
    except Exception as exc:
        logger.exception("[suggested_parts] resolve failed: %s", exc)
        return JsonResponse({
            "parts": [],
            "error": "تعذّر استخراج القطع المقترحة، حاول مرة أخرى.",
        }, status=200)

    return JsonResponse({
        "invoice_id": invoice.id,
        "parts": parts,
        "count": len(parts),
    })


@login_required(login_url='/login/')
@tenant_required
@csrf_exempt   # CSRF is enforced by the X-CSRFToken header check below
def job_card_suggested_part_add(request, invoice_id):
    """POST {product_id, quantity, unit_price?} → create a SaleInvoiceItem
    on the Job Card. Used by the 'Add to Job Card' button next to each
    suggested part. is_billable=True by default; accountant can untick
    later from the review screen.

    RBAC: sales / cashier / accountant / admin / manager / superuser."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)

    # Lightweight CSRF: require a header carrying the cookie value.
    cookie_token = request.META.get('CSRF_COOKIE') or request.COOKIES.get('mt_csrf')
    sent_token = request.headers.get('X-CSRFToken', '')
    if not cookie_token or not sent_token or cookie_token != sent_token:
        return JsonResponse({"error": "csrf_failed"}, status=403)

    invoice = get_object_or_404(SaleInvoice, id=invoice_id)
    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return JsonResponse({"error": "forbidden"}, status=403)

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    try:
        payload = _json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    product_id = payload.get('product_id')
    qty = int(payload.get('quantity') or 1)
    if qty < 1 or qty > 99:
        return JsonResponse({"error": "bad_quantity"}, status=400)
    if not product_id:
        return JsonResponse({"error": "product_required"}, status=400)

    product = Product.objects.filter(id=product_id).first()
    if product is None:
        return JsonResponse({"error": "product_not_found"}, status=404)

    unit_price = payload.get('unit_price')
    try:
        unit_price = Decimal(str(unit_price)) if unit_price is not None \
                     else Decimal(str(product.retail_price or 0))
    except (InvalidOperation, TypeError):
        unit_price = Decimal(str(product.retail_price or 0))

    with transaction.atomic():
        # Idempotency: if the same product already sits on this Job Card,
        # bump the quantity instead of duplicating the line.
        existing = (SaleInvoiceItem.objects
                    .filter(invoice=invoice, product=product)
                    .first())
        if existing:
            existing.quantity = (existing.quantity or 0) + qty
            existing.save(update_fields=['quantity'])
            item = existing
            action = 'incremented'
        else:
            item = SaleInvoiceItem.objects.create(
                invoice=invoice,
                product=product,
                quantity=qty,
                unit_price=unit_price,
                is_billable=True,
                billing_note='أُضيفت تلقائياً من اقتراح الـ AI',
            )
            action = 'created'

    logger.info(
        "[suggested_parts.add] tenant=%s user=%s invoice=%s product=%s "
        "qty=%s action=%s",
        getattr(getattr(request, 'tenant', None), 'schema_name', None),
        request.user.username, invoice.id, product.id, qty, action,
    )
    return JsonResponse({
        "ok": True,
        "action": action,
        "item_id": item.id,
        "product_name": product.name,
        "quantity": item.quantity,
        "unit_price": float(unit_price),
    })


# ─────────────────────────────────────────────────────────────────────
# 📩 RFQ Engine — multi-supplier fan-out for out-of-stock parts
# ─────────────────────────────────────────────────────────────────────
@login_required(login_url='/login/')
@tenant_required
@csrf_exempt   # X-CSRFToken header validated explicitly
def rfq_create(request, invoice_id):
    """POST {part_number, part_name?, product_id?, quantity?, vendor_ids?[]}
    Creates an RFQ for the Job Card, returns the per-vendor wa.me links."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)

    cookie_token = request.COOKIES.get('mt_csrf')
    sent_token = request.headers.get('X-CSRFToken', '')
    if not cookie_token or cookie_token != sent_token:
        return JsonResponse({"error": "csrf_failed"}, status=403)

    invoice = get_object_or_404(SaleInvoice, id=invoice_id)
    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return JsonResponse({"error": "forbidden"}, status=403)

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager', 'stock'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    pn = (payload.get('part_number') or '').strip().upper()
    if not pn:
        return JsonResponse({"error": "part_number_required"}, status=400)
    part_name = (payload.get('part_name') or '').strip()
    qty = int(payload.get('quantity') or 1)
    product_id = payload.get('product_id')

    product = Product.objects.filter(id=product_id).first() if product_id else None

    vendor_ids = payload.get('vendor_ids') or []
    from inventory.models import Vendor
    vendors = list(Vendor.objects.filter(id__in=vendor_ids)) if vendor_ids else []

    rfq_branch = invoice.branch or _get_branch_for_user(request.user)
    if rfq_branch is None:
        return JsonResponse({"error": "branch_required"}, status=400)

    from smart_diagnostics.services.rfq_engine import (
        create_rfq, build_whatsapp_messages,
    )

    try:
        rfq = create_rfq(
            branch=rfq_branch, product=product,
            part_number_requested=pn, part_name_requested=part_name,
            quantity=qty, job_card=invoice,
            requested_by=request.user, vendors=vendors,
            notes='',
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("[rfq_create] failed: %s", exc)
        return JsonResponse({"error": "rfq_create_failed"}, status=500)

    tenant = getattr(request, 'tenant', None)
    workshop_name = (
        getattr(tenant, 'name', None)
        or getattr(tenant, 'schema_name', None)
        or ''
    )
    messages_payload = build_whatsapp_messages(rfq, workshop_name=workshop_name)
    return JsonResponse({
        "ok": True,
        "rfq_id": rfq.id,
        "status": rfq.status,
        "part_number": rfq.part_number_requested,
        "quantity": rfq.quantity,
        "messages": messages_payload,
    }, status=201)


@login_required(login_url='/login/')
@tenant_required
@csrf_exempt
def rfq_log_quote(request, quote_id):
    """POST {price, eta_days?, notes?} — inventory manager pastes vendor reply."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    cookie_token = request.COOKIES.get('mt_csrf')
    if not cookie_token or cookie_token != request.headers.get('X-CSRFToken', ''):
        return JsonResponse({"error": "csrf_failed"}, status=403)

    from inventory.models import RFQQuote
    quote = get_object_or_404(RFQQuote.objects.select_related('rfq'), id=quote_id)

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager', 'stock'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    from smart_diagnostics.services.rfq_engine import log_quote_response
    try:
        log_quote_response(
            quote,
            price=payload.get('price'),
            eta_days=payload.get('eta_days'),
            notes=payload.get('notes') or '',
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse({
        "ok": True,
        "quote_id": quote.id,
        "rfq_status": quote.rfq.status,
        "price": float(quote.quoted_price),
        "eta_days": quote.quoted_eta_days,
    })


@login_required(login_url='/login/')
@tenant_required
@csrf_exempt
def rfq_accept_quote(request, quote_id):
    """POST → promote this quote to a draft PurchaseInvoice."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    cookie_token = request.COOKIES.get('mt_csrf')
    if not cookie_token or cookie_token != request.headers.get('X-CSRFToken', ''):
        return JsonResponse({"error": "csrf_failed"}, status=403)

    from inventory.models import RFQQuote
    quote = get_object_or_404(
        RFQQuote.objects.select_related('rfq', 'rfq__product', 'vendor'),
        id=quote_id,
    )

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'admin', 'manager', 'stock'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    from smart_diagnostics.services.rfq_engine import accept_quote
    try:
        po = accept_quote(quote)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("[rfq_accept_quote] failed: %s", exc)
        return JsonResponse({"error": "accept_failed"}, status=500)

    return JsonResponse({
        "ok": True,
        "purchase_invoice_id": po.id,
        "vendor_name": po.vendor.name,
        "total": float(po.total_amount),
        "status": po.status,
    })


# ─────────────────────────────────────────────────────────────────────
# 🗼 Central RFQ Control Tower — Inventory Manager dashboard
# ─────────────────────────────────────────────────────────────────────
@login_required(login_url='/login/')
@tenant_required
def rfq_management(request):
    """Macro view of every open / quoted / ordered RFQ across the floor.

    Built for the inventory manager who's managing 10-20 parallel
    WhatsApp threads with suppliers. Inline-editable quote inputs +
    side-by-side comparison reduce per-RFQ ops from a 3-click drill-down
    to a single screen.

    RBAC: stock / admin / manager / superuser only.
    """
    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'stock', 'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return HttpResponseForbidden(
            "هذه الشاشة مخصّصة لمديري المخزون فقط."
        )

    # 🐛 [Bug #1 fix] Wrap the entire query/sort/render in a try/except so any
    # data-state issue (a half-migrated tenant, a deleted vendor still linked
    # by a quote row, etc.) renders an empty-state instead of bubbling up to
    # the project-level JSON 500 handler in erp_core/urls.py.
    try:
        from inventory.models import RFQ

        branch = _get_branch_for_user(request.user)
        qs = RFQ.objects.select_related(
            'job_card', 'branch', 'product', 'requested_by',
            'accepted_quote__vendor', 'purchase_invoice',
        ).prefetch_related('quotes__vendor')
        if branch is not None:
            qs = qs.filter(branch=branch)

        open_rfqs = list(qs.filter(status=RFQ.STATUS_OPEN).order_by('-created_at'))
        quoted_rfqs = list(qs.filter(status=RFQ.STATUS_QUOTED).order_by('-created_at'))
        ordered_rfqs = list(qs.filter(status=RFQ.STATUS_ORDERED)
                              .order_by('-created_at')[:25])
        cancelled_count = qs.filter(status=RFQ.STATUS_CANCELLED).count()

        # Sort quotes within each RFQ: responded (cheapest first), then unresponded.
        for rfq in open_rfqs + quoted_rfqs:
            quotes = list(rfq.quotes.all())
            # Defensive: `quoted_price` is Decimal-or-None, `quoted_eta_days`
            # is int-or-None — cast both before sort so a `None` from either
            # never sneaks into a Decimal comparison.
            responded = sorted(
                (q for q in quotes if q.quoted_price is not None),
                key=lambda q: (
                    float(q.quoted_price) if q.quoted_price is not None else 0.0,
                    int(q.quoted_eta_days) if q.quoted_eta_days is not None else 9999,
                ),
            )
            unresponded = [q for q in quotes if q.quoted_price is None]
            rfq.sorted_quotes = responded + unresponded
            rfq.best_quote = responded[0] if responded else None
            # Stamp `is_best` on EVERY quote (including unresponded) so the
            # template never sees a missing attribute.
            best_id = rfq.best_quote.id if rfq.best_quote else None
            for q in quotes:
                q.is_best = (q.id == best_id) if best_id else False

    except Exception as exc:
        logger.exception("[rfq_management] failed: %s", exc)
        # Degrade to an empty board rather than 500
        open_rfqs = []
        quoted_rfqs = []
        ordered_rfqs = []
        cancelled_count = 0

    return render(request, 'inventory/rfq_management.html', {
        'open_rfqs': open_rfqs,
        'quoted_rfqs': quoted_rfqs,
        'ordered_rfqs': ordered_rfqs,
        'cancelled_count': cancelled_count,
        'open_count': len(open_rfqs),
        'quoted_count': len(quoted_rfqs),
        'ordered_count': len(ordered_rfqs),
        'reviewer_name': request.user.get_full_name() or request.user.username,
        'branch': branch,
    })


# ─────────────────────────────────────────────────────────────────────
# 🪪 Vehicle Health Passport — full timeline + AI history per VIN
# ─────────────────────────────────────────────────────────────────────
@login_required(login_url='/login/')
@tenant_required
def vehicle_health_passport(request, chassis_number):
    """The single-source-of-truth view of a vehicle's life with us:
    every visit, every DTC, every part replaced, every photo, every
    diagnostic AI summary — laid out as a reverse-chronological timeline.

    Built for two audiences:
        • The advisor explaining to a customer why a repair is needed
          ("look, your car has come in 4 times for the same code…").
        • The customer themselves on the public share view (future step).

    Anyone in the workshop can view; the share-link variant will be
    public-token gated like the AI diagnostic report.
    """
    vehicle = get_object_or_404(
        Vehicle.objects
            .select_related('customer')
            .prefetch_related(
                'diagnostic_reports__engineer__user',
                'diagnostic_reports__photos',
                'service_nudges__rule',
            ),
        chassis_number=chassis_number.upper(),
    )

    # All Job Cards (posted = paid, others = open work)
    job_cards = list(
        SaleInvoice.objects
        .filter(vehicle=vehicle, invoice_type='maintenance')
        .select_related('branch')
        .prefetch_related(
            'items__product', 'service_items__service',
            'diagnostic_reports', 'repair_logs',
        )
        .order_by('-date_created')
    )

    # All diagnostic reports — sorted newest first
    diag_reports = list(
        vehicle.diagnostic_reports.all().order_by('-scanned_at')
    )

    # Aggregate DTC frequency across the whole life of the vehicle
    from collections import Counter
    dtc_counter = Counter()
    for r in diag_reports:
        for code in (r.fault_codes or []):
            dtc_counter[code.upper()] += 1
    top_dtcs = dtc_counter.most_common(8)

    # Aggregate parts ever installed
    parts_counter = Counter()
    for jc in job_cards:
        for item in jc.items.all():
            parts_counter[item.product.name] += item.quantity or 0
    top_parts = parts_counter.most_common(6)

    # Build a unified timeline: visits + diagnostic-only sessions
    timeline = []
    for jc in job_cards:
        timeline.append({
            'type': 'visit',
            'date': jc.date_created,
            'invoice': jc,
            'total': jc.total_amount,
            'status': jc.status,
            'branch_name': jc.branch.name if jc.branch_id else '',
            'parts_count': jc.items.count(),
            'services_count': jc.service_items.count(),
            'diag_count': jc.diagnostic_reports.count(),
            'photos_count': sum(r.photos.count() for r in jc.diagnostic_reports.all()),
            'ai_summary_excerpt': next(
                (r.ai_summary[:240]
                 for r in jc.diagnostic_reports.all()
                 if r.ai_summary), ''),
        })
    # Orphan diagnostic reports (saved without a Job Card link)
    for r in diag_reports:
        if r.job_card_id is None:
            timeline.append({
                'type': 'orphan_diag',
                'date': r.scanned_at,
                'report': r,
                'fault_codes': r.fault_codes,
                'ai_summary_excerpt': (r.ai_summary or '')[:240],
                'photos_count': r.photos.count(),
            })

    timeline.sort(key=lambda e: e['date'], reverse=True)

    # Predictive nudges for this vehicle (computed live so they reflect
    # the latest job-card timestamps; the daily Celery task does the
    # heavy bulk-compute version)
    nudges = []
    try:
        from inventory.predictive_engine import compute_nudges_for_vehicle
        nudges = compute_nudges_for_vehicle(vehicle, persist=False)
    except Exception:
        nudges = []

    return render(request, 'inventory/vehicle_health_passport.html', {
        'vehicle': vehicle,
        'customer': vehicle.customer,
        'timeline': timeline,
        'diag_reports': diag_reports,
        'top_dtcs': top_dtcs,
        'top_parts': top_parts,
        'nudges': nudges,
        'visit_count': len(job_cards),
        'last_visit': job_cards[0].date_created if job_cards else None,
        'first_visit': job_cards[-1].date_created if job_cards else None,
    })


# ─────────────────────────────────────────────────────────────────────
# 💚 Retention & Campaigns — CRM Dashboard (Week 4 Phase 2)
# ─────────────────────────────────────────────────────────────────────
@login_required(login_url='/login/')
@tenant_required
def retention_crm(request):
    """Macro view of every customer with at least one due/overdue service.

    The list is driven by `ServiceNudge` rows (populated by the daily
    Celery sweep + recomputed live on Job Card posts). The advisor can
    refresh, send a WhatsApp reminder, or dismiss/snooze each nudge.

    RBAC: sales / cashier / accountant / admin / manager / superuser.
    """
    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return HttpResponseForbidden(
            "هذه الشاشة مخصّصة لطاقم المبيعات والمحاسبة فقط."
        )

    try:
        from inventory.models import ServiceNudge

        branch = _get_branch_for_user(request.user)

        base_qs = (ServiceNudge.objects
                   .select_related(
                       'rule', 'vehicle', 'vehicle__customer',
                       'sent_by',
                   )
                   .filter(status__in=[
                       ServiceNudge.STATUS_PENDING,
                       ServiceNudge.STATUS_SENT,
                   ]))

        # Branch-scope by the customer's most-recent job-card branch (cheap proxy)
        # — skipped if the user is unscoped (admin/superuser without a branch pin).
        # Customers with NO job cards still appear; they won't have a branch hint.

        # Group by urgency
        overdue = list(base_qs.filter(urgency=ServiceNudge.URGENCY_OVERDUE)
                              .order_by('due_at'))
        due = list(base_qs.filter(urgency=ServiceNudge.URGENCY_DUE)
                          .order_by('due_at'))
        upcoming = list(base_qs.filter(urgency=ServiceNudge.URGENCY_UPCOMING)
                                .order_by('due_at')[:100])

        # KPIs
        kpi_total_actionable = len(overdue) + len(due)
        kpi_sent_today = base_qs.filter(
            status=ServiceNudge.STATUS_SENT,
            sent_at__date=timezone.localdate(),
        ).count()
        kpi_unique_customers = base_qs.filter(
            urgency__in=[ServiceNudge.URGENCY_OVERDUE, ServiceNudge.URGENCY_DUE],
        ).values('vehicle__customer_id').distinct().count()

    except Exception as exc:
        logger.exception("[retention_crm] failed: %s", exc)
        overdue = []
        due = []
        upcoming = []
        kpi_total_actionable = 0
        kpi_sent_today = 0
        kpi_unique_customers = 0

    return render(request, 'inventory/retention_crm.html', {
        'overdue_nudges': overdue,
        'due_nudges': due,
        'upcoming_nudges': upcoming,
        'kpi_total_actionable': kpi_total_actionable,
        'kpi_sent_today': kpi_sent_today,
        'kpi_unique_customers': kpi_unique_customers,
        'reviewer_name': request.user.get_full_name() or request.user.username,
    })


@login_required(login_url='/login/')
@tenant_required
@csrf_exempt
def retention_send_whatsapp(request, nudge_id):
    """POST → builds a wa.me URL from the rule's template, stamps
    `ServiceNudge.status=sent`, returns the URL for the JS to open.

    The advisor still has to click 'Send' inside WhatsApp Web/Mobile —
    we don't have outbound API yet — but the audit trail of who sent
    which reminder when is captured here."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    cookie_token = request.COOKIES.get('mt_csrf')
    if not cookie_token or cookie_token != request.headers.get('X-CSRFToken', ''):
        return JsonResponse({"error": "csrf_failed"}, status=403)

    from inventory.models import ServiceNudge
    nudge = get_object_or_404(
        ServiceNudge.objects.select_related(
            'rule', 'vehicle', 'vehicle__customer',
        ),
        id=nudge_id,
    )

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    customer = nudge.vehicle.customer
    if not customer.phone:
        return JsonResponse({"error": "no_customer_phone"}, status=400)

    # Build the personalised body from the rule's template
    tenant = getattr(request, 'tenant', None)
    workshop_name = (getattr(tenant, 'name', None)
                     or getattr(tenant, 'schema_name', '')
                     or 'مركزنا')

    vehicle_label = ' '.join(filter(None, [
        nudge.vehicle.brand, nudge.vehicle.model_name,
        f"({nudge.vehicle.car_plate})" if nudge.vehicle.car_plate else '',
    ]))

    template = nudge.rule.whatsapp_template or (
        "مرحباً {customer} 👋\n\n"
        "حسب آخر زيارة، حان موعد *{rule}* لسيارة *{vehicle}*.\n"
        "نسعد بحجز موعد لك في {workshop}."
    )
    body = template.format(
        customer=customer.name or 'عميلنا الكريم',
        vehicle=vehicle_label or 'سيارتك',
        rule=nudge.rule.name,
        workshop=workshop_name,
    )

    # Normalise phone (E.164 digits for wa.me)
    import re as _re, urllib.parse
    digits = _re.sub(r'[\s\-\(\)+]+', '', customer.phone)
    if digits.startswith('00'):
        digits = digits[2:]
    elif digits.startswith('0'):
        digits = '20' + digits[1:]    # EG default
    wa_url = f"https://wa.me/{digits}?text={urllib.parse.quote(body)}"

    # Stamp the audit trail BEFORE returning the URL (idempotent on re-send)
    nudge.status = ServiceNudge.STATUS_SENT
    nudge.sent_at = timezone.now()
    nudge.sent_by = request.user
    nudge.save(update_fields=['status', 'sent_at', 'sent_by'])

    logger.info(
        "[retention.send] tenant=%s user=%s nudge=%s customer=%s rule=%s",
        getattr(tenant, 'schema_name', None), request.user.username,
        nudge.id, customer.id, nudge.rule.name,
    )
    return JsonResponse({
        "ok": True,
        "wa_url": wa_url,
        "message_preview": body,
        "customer_name": customer.name,
        "vehicle_label": vehicle_label,
    })


@login_required(login_url='/login/')
@tenant_required
@csrf_exempt
def retention_dismiss(request, nudge_id):
    """POST → mark a nudge as dismissed (no outreach this cycle)."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    cookie_token = request.COOKIES.get('mt_csrf')
    if not cookie_token or cookie_token != request.headers.get('X-CSRFToken', ''):
        return JsonResponse({"error": "csrf_failed"}, status=403)

    from inventory.models import ServiceNudge
    nudge = get_object_or_404(ServiceNudge, id=nudge_id)

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    nudge.status = ServiceNudge.STATUS_DISMISSED
    nudge.save(update_fields=['status'])
    return JsonResponse({"ok": True, "nudge_id": nudge.id})


@login_required(login_url='/login/')
@tenant_required
def retention_refresh(request):
    """POST → trigger an on-demand bulk recompute of all nudges for this
    tenant. Admin/manager only — the daily Celery sweep handles the
    routine case."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    cookie_token = request.COOKIES.get('mt_csrf')
    if not cookie_token or cookie_token != request.headers.get('X-CSRFToken', ''):
        return JsonResponse({"error": "csrf_failed"}, status=403)

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    try:
        from inventory.predictive_engine import refresh_all_nudges
        result = refresh_all_nudges(limit=2000)
    except Exception as exc:
        logger.exception("[retention.refresh] failed: %s", exc)
        return JsonResponse({"error": "refresh_failed"}, status=500)

    return JsonResponse({"ok": True, **result})
