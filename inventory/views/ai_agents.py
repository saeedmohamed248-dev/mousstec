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


# AI agents (diagnostic, b2b, vision) + repair estimator + OCR + orchestrator + ai_diag print/share.



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
@require_feature('b2b_marketplace')
def b2b_market_search_api(request):
    """HTTP Adapter لوكيل السوق المركزي.

    🔒 Same gate as the b2b_marketplace page — closes the API back door.
    """
    query = request.GET.get('q', request.GET.get('part_number', '')).strip()
    if not query:
        return _json_response_safe({'results': []})
    schema = getattr(connection, 'schema_name', 'public')
    results = _agent_b2b_market(query, schema)
    return _json_response_safe({'status': 'success', 'results_count': len(results), 'results': results})


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


@login_required(login_url='/login/')
@tenant_required
def ai_competitor_recon_api(request):
    return _json_response_safe({"status": "success", "channel": "market_competitor_recon_active"})


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
