"""Customer-facing diagnostics — separate from the workshop (tenant) diag room.

Flow for a car owner:
  /marketplace/diagnostics/         → landing + status + scan launcher
  /marketplace/diagnostics/pricing/ → 3-tier pricing page (99/199/399)
  /marketplace/diagnostics/upgrade/<tier>/ → checkout (Paymob if configured,
                                              else dev-mode auto-activate)
  /marketplace/diagnostics/scan/    → POST, records a scan against the quota
  /marketplace/diagnostics/paymob-callback/ → server-to-server payment confirm

The DiagnosticsQuotaService in smart_diagnostics is tenant-scoped — we keep
customers on their own model (CustomerDiagnosticsSubscription) so the two
worlds never mix.
"""
from __future__ import annotations

import json
import logging
import uuid

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django_tenants.utils import schema_context

from clients.models import CustomerDiagnosticsSubscription
from clients.views._shared import _marketplace_auth

logger = logging.getLogger('mouss_tec_core')


def _sub_for(request):
    """Resolve customer + their subscription (creating a trial if missing —
    covers customers who registered before the trial feature shipped)."""
    customer = _marketplace_auth(request)
    if not customer:
        return None, None
    with schema_context('public'):
        sub = CustomerDiagnosticsSubscription.grant_trial(customer)
        sub.reset_period_if_needed()
    return customer, sub


def _tier_card(tier_key: str) -> dict:
    return {
        'key': tier_key,
        'label': dict(CustomerDiagnosticsSubscription.TIER_CHOICES).get(tier_key, tier_key),
        'price_egp': CustomerDiagnosticsSubscription.TIER_PRICES_EGP.get(tier_key),
        'quota': CustomerDiagnosticsSubscription.TIER_QUOTAS.get(tier_key),
        'features': CustomerDiagnosticsSubscription.TIER_FEATURES.get(tier_key, []),
    }


def diagnostics_landing(request):
    """صفحة التشخيص للعميل — لوحة الحالة + زر الفحص."""
    customer, sub = _sub_for(request)
    if not customer:
        return redirect('/marketplace/automotive/')

    return render(request, 'clients/marketplace/diagnostics_landing.html', {
        'customer': customer,
        'sub': sub,
        'is_active': sub.is_active(),
        'days_remaining': sub.days_remaining(),
        'quota_remaining': sub.quota_remaining(),
        'quota_total': CustomerDiagnosticsSubscription.TIER_QUOTAS.get(sub.tier, 0),
        'features': sub.TIER_FEATURES.get(sub.tier, []),
    })


def diagnostics_pricing(request):
    """صفحة الباقات — 3 شرائح + التجربة المجانية."""
    customer, sub = _sub_for(request)
    tiers = [_tier_card(t) for t in ('basic', 'pro', 'empire')]
    return render(request, 'clients/marketplace/diagnostics_pricing.html', {
        'customer': customer,
        'sub': sub,
        'tiers': tiers,
        'current_tier': sub.tier if sub else None,
        'days_remaining': sub.days_remaining() if sub else 0,
    })


@csrf_exempt
def diagnostics_upgrade(request, tier: str):
    """Start an upgrade. If Paymob is configured we hand off to it; otherwise
    (dev/staging) we activate immediately and log a synthetic ref."""
    customer, sub = _sub_for(request)
    if not customer:
        return JsonResponse({"error": "auth_required"}, status=401)

    if tier not in ('basic', 'pro', 'empire'):
        return JsonResponse({"error": "invalid tier"}, status=400)

    paymob_key = getattr(settings, 'PAYMOB_API_KEY', '')
    if not paymob_key:
        # Dev path — activate immediately so the flow is testable end-to-end.
        synthetic_ref = f"dev-{uuid.uuid4().hex[:16]}"
        with schema_context('public'):
            sub.upgrade(tier, payment_ref=synthetic_ref)
        logger.info(
            "[CUSTOMER DIAG] dev-mode upgrade: customer=%s tier=%s ref=%s",
            customer.pk, tier, synthetic_ref,
        )
        if request.method == 'POST':
            return JsonResponse({
                "status": "activated",
                "tier": tier,
                "redirect": "/marketplace/diagnostics/",
            })
        return redirect('/marketplace/diagnostics/?upgraded=1')

    # Paymob path — defer to the existing parts_checkout integration shape.
    # We surface only the iframe URL; the actual integration_id and billing
    # data is the same shape used elsewhere in this codebase.
    price = CustomerDiagnosticsSubscription.TIER_PRICES_EGP[tier]
    # Lazy import to avoid bootstrapping payment libs on every request.
    try:
        from clients.services.paymob import create_iframe_url  # type: ignore
    except Exception:
        create_iframe_url = None

    if create_iframe_url is None:
        return JsonResponse({
            "error": "payment_unavailable",
            "message": "بوابة الدفع غير متاحة حالياً. تواصل مع الدعم.",
        }, status=503)

    callback_url = request.build_absolute_uri('/marketplace/diagnostics/paymob-callback/')
    iframe_url = create_iframe_url(
        amount_egp=price,
        customer_phone=customer.phone,
        customer_name=customer.full_name,
        order_ref=f"diag-{customer.pk}-{tier}-{uuid.uuid4().hex[:8]}",
        callback_url=callback_url,
        metadata={'customer_pk': customer.pk, 'tier': tier},
    )
    return redirect(iframe_url)


@csrf_exempt
def diagnostics_scan(request):
    """POST {vin?, dtc_codes?} — records one scan against the quota.

    Does NOT execute the actual AI diagnosis here; that's handled by the
    smart_diagnostics service. This endpoint is the gate + counter so the
    business rule (quota per tier) lives in one place.
    """
    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)
    customer, sub = _sub_for(request)
    if not customer:
        return JsonResponse({"error": "auth_required"}, status=401)

    ok, reason = sub.can_scan()
    if not ok:
        return JsonResponse({
            "error": "quota_exceeded",
            "reason": reason,
            "upgrade_url": "/marketplace/diagnostics/pricing/",
        }, status=402)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        payload = {}

    sub.record_scan()
    logger.info(
        "[CUSTOMER DIAG] scan recorded: customer=%s tier=%s used=%s/%s",
        customer.pk, sub.tier, sub.scans_used, sub.TIER_QUOTAS.get(sub.tier),
    )

    # ── Translate DTC codes via the shared catalog ─────────────────────
    # The catalog lives in `diagnostics_catalog` and is seeded with 96+
    # OBD2 generic codes + Arabic descriptions + severity.
    raw_codes = [str(c).upper().strip() for c in payload.get('dtc_codes', []) if c]
    raw_codes = list(dict.fromkeys(raw_codes))[:20]  # dedupe + cap.
    diagnoses = []
    if raw_codes:
        try:
            from diagnostics_catalog.models import DTCDefinition
            defs = {d.code: d for d in DTCDefinition.objects.filter(code__in=raw_codes)}
        except Exception as e:
            logger.warning("[CUSTOMER DIAG] catalog lookup failed: %s", e)
            defs = {}
        severity_label = {
            'low':      ('منخفض',  '#10b981'),
            'medium':   ('متوسط',  '#f59e0b'),
            'high':     ('مرتفع',  '#ef4444'),
            'critical': ('حرج',    '#dc2626'),
        }
        for code in raw_codes:
            d = defs.get(code)
            if d:
                sev_ar, sev_color = severity_label.get(d.severity, ('متوسط', '#f59e0b'))
                diagnoses.append({
                    'code': code,
                    'known': True,
                    'system': d.get_system_display(),
                    'short': d.short_description,
                    'full': d.full_description or '',
                    'severity': d.severity,
                    'severity_label': sev_ar,
                    'severity_color': sev_color,
                    'guided_steps': d.guided_steps or [],
                    'likely_parts': d.likely_oem_parts or [],
                })
            else:
                diagnoses.append({
                    'code': code,
                    'known': False,
                    'short': 'كود غير موجود في القاعدة المعرفية بعد — يحتاج تحليل يدوي.',
                    'severity': 'medium',
                    'severity_label': 'غير معروف',
                    'severity_color': '#64748b',
                })

    return JsonResponse({
        "status": "ok",
        "vin": (payload.get('vin') or '').upper()[:17],
        "diagnoses": diagnoses,
        "quota_remaining": sub.quota_remaining(),
        "tier": sub.tier,
    })


# =====================================================================
# 🤖 AI Diagnostic Chat — tier-gated copilot for the car owner
# =====================================================================
# Tier matrix (mirrors CustomerDiagnosticsSubscription.TIER_FEATURES):
#   trial / basic → symptom triage only (no DTC reasoning, no vision,
#                   no live data, no pin-level guidance).
#   pro           → + DTC decode + repair-plan guidance + image vision.
#                   No live OBD telemetry stream, no pin voltages.
#   empire        → full master-tech mode (live data, pin voltages, scope).
#
# Session conversation lives in request.session to stay stateless on the
# server. Cap at 8 turns to keep token cost predictable.
_CHAT_SESSION_KEY = 'customer_diag_chat_v1'
_CHAT_MAX_HISTORY = 8
_CHAT_RATE_PER_MIN = 12
_CHAT_RATE_PER_HOUR = 200


def _tier_system_prompt(tier: str) -> str:
    """Tier-aware system prompt — narrower scope for cheaper tiers."""
    base = (
        "أنت 'مساعد تشخيص العربيات' لـ Mouss Tec — بتكلم صاحب العربية "
        "نفسه (مش فني ورشة). اشرح بلغة بسيطة، وقت ما تحتاج تقني — "
        "وضح المصطلح. ركز على: هل المشكلة طارئة؟ أنصحه يكمل قيادة "
        "ولا يدخل ورشة فوراً؟ متوسط تكلفة الإصلاح بمصر تقريباً.\n\n"
    )
    if tier in ('trial', 'basic'):
        return base + (
            "🔒 صلاحياتك في الباقة الحالية: تحليل أعراض فقط (صوت غريب، رائحة، "
            "لمبة على التابلوه، استهلاك بنزين عالي...). \n"
            "❌ ممنوع: تفاصيل أكواد DTC، قراءات live data، voltages، صور.\n"
            "لو العميل طلب حاجة من دي، قوله: 'الميزة دي متاحة في باقة Pro/Empire — "
            "اعمل ترقية من /marketplace/diagnostics/pricing/'.\n"
            "هدفك: تنصحه هل يروح ورشة دلوقتي ولا يستنى، وإيه الأولوية."
        )
    if tier == 'pro':
        return base + (
            "🟦 صلاحيات Pro: تحليل الأعراض + تفسير أكواد OBD-II بصياغة بسيطة + "
            "نصيحة بخطة إصلاح من الأرخص للأغلى + قراءة صور (لمبة dashboard، "
            "محرك، صورة من scanner). \n"
            "❌ ممنوع: قراءات live telemetry (rpm/coolant) في الوقت الحقيقي، "
            "voltages بِنّة بِنّة، pin-level testing — دي للـ Empire / فنيين.\n"
            "هدفك: تشرح للعميل الكود، الإصلاح المتوقع، التكلفة التقريبية، "
            "والأسئلة اللي يسألها للورشة قبل ما تبدأ تصلح."
        )
    # empire — full unlocked
    return base + (
        "💎 صلاحيات Empire (مفتوحة بالكامل): زي خبير الفنيين — DTCs + Live Data "
        "+ صور + pin voltages + scope hints. لو ظهرت قراءات live في السياق "
        "اللي جاي، استخدمها. لكن خلي اللغة قابلة للفهم — العميل مش بالضرورة فني."
    )


def _serialize_subscription(sub) -> dict:
    return {
        'tier': sub.tier,
        'tier_label': dict(sub.TIER_CHOICES).get(sub.tier, sub.tier),
        'is_active': sub.is_active(),
        'quota_remaining': sub.quota_remaining(),
        'features': sub.TIER_FEATURES.get(sub.tier, []),
    }


@csrf_exempt
def diagnostics_chat(request):
    """POST {message, image_data_url?, dtc_codes?, snapshot?} → AI reply.

    Tier-gated:
      • trial/basic → text only, no image, no DTC/snapshot context.
      • pro         → + image + DTC reasoning, no live snapshot.
      • empire      → full context (image + DTC + live snapshot).
    """
    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    customer, sub = _sub_for(request)
    if not customer:
        return JsonResponse({"error": "auth_required"}, status=401)

    if not sub.is_active():
        return JsonResponse({
            "error": "subscription_inactive",
            "message": "اشتراكك في التشخيص منتهي. جدّد للاستمرار.",
            "upgrade_url": "/marketplace/diagnostics/pricing/",
        }, status=402)

    # 🛡️ Rate limit per customer (chat is much chattier than scans)
    from erp_core.ai._safety import check_ai_rate_limit
    ok, msg = check_ai_rate_limit(
        f'cust_diag_chat:{customer.pk}',
        per_minute=_CHAT_RATE_PER_MIN,
        per_hour=_CHAT_RATE_PER_HOUR,
    )
    if not ok:
        return JsonResponse({"error": "rate_limited", "message": msg}, status=429)

    try:
        payload = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    user_message = (payload.get('message') or '').strip()
    if not user_message:
        return JsonResponse({
            "reply": "اكتب سؤالك أو وصف المشكلة في العربية وأنا هساعدك.",
            "subscription": _serialize_subscription(sub),
        })
    if len(user_message) > 1500:
        return JsonResponse({"error": "message_too_long",
                             "message": "اختصر السؤال (1500 حرف كحد أقصى)."}, status=400)

    # Tier-based feature gating
    has_dtc = sub.has_feature('vehicle_history') or sub.tier in ('pro', 'empire')
    has_vision = sub.tier in ('pro', 'empire')
    has_live = sub.has_feature('live_data')

    dtcs = []
    if has_dtc:
        dtcs = [str(c).upper().strip() for c in (payload.get('dtc_codes') or []) if c][:10]

    image_data_url = payload.get('image_data_url') if has_vision else None
    snapshot = payload.get('snapshot') if has_live else {}
    vin = (payload.get('vin') or '').upper().strip()[:17] or None

    # Conversation history in session
    session = request.session
    history = list(session.get(_CHAT_SESSION_KEY, []))
    history.append({'role': 'user', 'text': user_message})
    history = history[-(_CHAT_MAX_HISTORY * 2):]  # *2 since each turn has user+assistant

    try:
        from erp_core.ai.diagnostic_room_ai import answer_room_turn
        # Inject our tier-aware system prompt by temporarily wrapping it.
        # `answer_room_turn` builds its own messages list — we instead call
        # call_llm_layer directly here to keep the tier prompt as the system.
        from inventory.ai_services import call_llm_layer
        from erp_core.ai.diagnostic_room_ai import (
            _build_context_block,
            _validate_image_data_url,
        )

        context_block = _build_context_block(
            snapshot=snapshot or {}, dtcs=dtcs or [],
            vehicle_hint={}, vin=vin,
        )
        messages = [{'role': 'system', 'content': _tier_system_prompt(sub.tier)}]
        # Replay last few turns
        for turn in history[:-1][-_CHAT_MAX_HISTORY:]:
            role = 'user' if turn.get('role') == 'user' else 'assistant'
            text = str(turn.get('text', '')).strip()
            if text:
                messages.append({'role': role, 'content': text})

        text_payload = (
            f"{context_block}\n\nسؤال العميل: {user_message}"
            if (has_dtc or has_live) else
            f"سؤال العميل: {user_message}"
        )
        safe_image = _validate_image_data_url(image_data_url) if has_vision else None
        if safe_image:
            messages.append({
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': text_payload + '\n\n📷 العميل رفع صورة، حللها واربطها بسؤاله.'},
                    {'type': 'image_url', 'image_url': {'url': safe_image}},
                ],
            })
        else:
            messages.append({'role': 'user', 'content': text_payload})

        answer = call_llm_layer(messages, json_mode=False, max_retries=2)
    except Exception as e:
        logger.exception("[CUSTOMER DIAG CHAT] pipeline failed")
        return JsonResponse({
            "error": "ai_unavailable",
            "message": "المساعد مش متاح حالياً، جرب تاني خلال لحظات.",
        }, status=503)

    if not answer:
        return JsonResponse({
            "error": "ai_empty",
            "message": "المساعد مش متاح حالياً، جرب تاني خلال لحظات.",
        }, status=503)

    answer = answer.strip()
    history.append({'role': 'assistant', 'text': answer})
    session[_CHAT_SESSION_KEY] = history[-(_CHAT_MAX_HISTORY * 2):]
    session.modified = True

    return JsonResponse({
        "reply": answer,
        "subscription": _serialize_subscription(sub),
        "context_used": {
            'dtcs': len(dtcs),
            'vision': bool(safe_image) if has_vision else False,
            'live_snapshot': bool(snapshot) if has_live else False,
            'vin_decoded': bool(vin),
        },
    })


@csrf_exempt
def diagnostics_chat_reset(request):
    """Clear conversation history without affecting subscription."""
    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)
    request.session.pop(_CHAT_SESSION_KEY, None)
    request.session.modified = True
    return JsonResponse({"status": "ok"})


@csrf_exempt
def diagnostics_paymob_callback(request):
    """Server-to-server payment confirmation. Paymob posts here; we activate
    the subscription based on metadata."""
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    obj = data.get('obj', {})
    if not obj.get('success'):
        logger.warning("[CUSTOMER DIAG] paymob callback: not success — %s", obj.get('id'))
        return JsonResponse({"status": "ignored"})

    metadata = obj.get('payment_key_claims', {}).get('extra', {}) or {}
    customer_pk = metadata.get('customer_pk')
    tier = metadata.get('tier')
    paymob_id = obj.get('id', '')

    if not customer_pk or tier not in ('basic', 'pro', 'empire'):
        logger.error("[CUSTOMER DIAG] paymob callback: missing metadata — %s", metadata)
        return JsonResponse({"error": "metadata"}, status=400)

    with schema_context('public'):
        try:
            sub = CustomerDiagnosticsSubscription.objects.get(customer_id=customer_pk)
        except CustomerDiagnosticsSubscription.DoesNotExist:
            return JsonResponse({"error": "subscription_not_found"}, status=404)
        sub.upgrade(tier, payment_ref=f"paymob-{paymob_id}")

    logger.info(
        "[CUSTOMER DIAG] paymob upgrade OK: customer=%s tier=%s paymob_id=%s",
        customer_pk, tier, paymob_id,
    )
    return JsonResponse({"status": "ok"})
