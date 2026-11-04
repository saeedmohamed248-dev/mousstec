"""🔑 استرجاع كلمة سر عميل السوق (Marketplace) بكود على الموبايل.

قبل كده: مفيش أي طريقة لعميل نسي الباسورد يرجع حسابه، والحسابات القديمة اللي من غير
باسورد كان أي حد يعرف رقمها يدخل عليها ويحط لها باسورد من عنده (استيلاء على الحساب).

الخطوات:
  GET  /marketplace/password/forgot/          ← الصفحة
  POST /marketplace/password/forgot/send/     {phone}                  ← يبعت كود ٦ أرقام
  POST /marketplace/password/forgot/confirm/  {phone, code, password}  ← يغيّر الباسورد ويدخّله

  «phone» ممكن يكون رقم موبايل أو إيميل. الكود بيتبعت على إيميل الحساب (لو موجود)
  بنظام الإيميل العادي، وكمان على الموبايل لو OTP_DELIVERY_PROVIDER مضبوط (واتساب/SMS).

🛡️ الحماية:
  • الرد واحد سواء الرقم متسجّل أو لأ (مفيش كشف مين عنده حساب).
  • الكود متخزّن hash في الكاش ١٠ دقايق، و٥ محاولات غلط بتلغيه.
  • حد إرسال: ٣ أكواد للرقم في الساعة، و١٠ طلبات للـ IP في الساعة.
  • تغيير الباسورد بيغيّر session_token ⇒ أي جلسة قديمة (على جهاز تاني) بتخرج.
  • CSRF شغال عادي (مش csrf_exempt).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import uuid

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from django_tenants.utils import schema_context

from clients.models import MarketplaceCustomer
from erp_core.client_ip import client_ip

from ._shared import _send_otp_via_channel

logger = logging.getLogger('mouss_tec_core')

CODE_TTL = 600            # ١٠ دقايق
MAX_ATTEMPTS = 5          # محاولات الكود الغلط قبل ما يتلغي
SENDS_PER_PHONE_HOUR = 3
REQUESTS_PER_IP_HOUR = 10

GENERIC_SENT = ("لو الحساب ده متسجّل عندنا، هيوصلك كود من ٦ أرقام على الإيميل (وعلى الموبايل لو متاح) "
                "خلال دقيقة. بُص في الـ Spam لو ماوصلش، أو كلّم الدعم.")
BAD_CODE = "الكود غلط أو انتهت صلاحيته — اطلب كود جديد."


def normalize_phone(phone: str) -> str:
    """نفس توحيد الأرقام المصرية اللي في التسجيل والدخول (01xxxxxxxxx ⇒ +201xxxxxxxxx)."""
    phone = (phone or '').strip()
    if phone.startswith('+'):
        return phone
    digits = ''.join(c for c in phone if c.isdigit())
    no_lead = digits.lstrip('0')
    if len(no_lead) == 10 and no_lead.startswith('1'):
        return f'+20{no_lead}'
    if len(digits) == 12 and digits.startswith('201'):
        return f'+{digits}'
    return phone


def _code_key(customer_pk):
    return f'mp_pwreset:{customer_pk}'


def _hash_code(customer_pk, code):
    return hmac.new(settings.SECRET_KEY.encode(), f'{customer_pk}:{code}'.encode(), hashlib.sha256).hexdigest()


def _find_customer(identifier):
    """العميل من رقم موبايل أو إيميل (عملاء نشطين بس). بيرجّع (customer, مفتاح الحد)."""
    identifier = (identifier or '').strip()
    with schema_context('public'):
        qs = MarketplaceCustomer.objects.filter(is_blocked=False, is_deleted=False)
        if '@' in identifier:
            email = identifier.lower()
            # الإيميل مش unique في الموديل — لو متكرر مانخمّنش مين، نرفض بصمت.
            matches = list(qs.filter(email__iexact=email)[:2])
            return (matches[0] if len(matches) == 1 else None), email
        phone = normalize_phone(identifier)
        return qs.filter(phone=phone).first(), phone


def _hit(key, ttl):
    """يزوّد عدّاد في الكاش ويرجّع القيمة الجديدة."""
    if cache.add(key, 1, ttl):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, ttl)
        return 1


def _json_body(request):
    try:
        return json.loads(request.body or b'{}')
    except (ValueError, TypeError):
        return None


@ensure_csrf_cookie
def password_forgot_page(request):
    return render(request, 'clients/marketplace/password_forgot.html', {
        'prefill_phone': (request.GET.get('phone') or '')[:20],
    })


@require_POST
def password_forgot_send(request):
    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "بيانات غير صالحة"}, status=400)
    identifier = str(data.get('phone') or '').strip()
    if '@' not in identifier and len(normalize_phone(identifier)) < 10:
        return JsonResponse({"error": "اكتب رقم الموبايل أو الإيميل صح"}, status=400)

    ip = client_ip(request) or 'unknown'
    if _hit(f'mp_pwreset_ip:{ip}', 3600) > REQUESTS_PER_IP_HOUR:
        return JsonResponse({"error": "طلبات كتير — حاول بعد ساعة."}, status=429)

    customer, _ = _find_customer(identifier)

    # الرد نفسه في كل الحالات — عشان محدش يعرف من الصفحة دي مين عنده حساب.
    if customer is None:
        return JsonResponse({"status": "sent", "message": GENERIC_SENT})
    if _hit(f'mp_pwreset_sends:{customer.pk}', 3600) > SENDS_PER_PHONE_HOUR:
        return JsonResponse({"status": "sent", "message": GENERIC_SENT})

    code = f'{secrets.randbelow(1_000_000):06d}'
    cache.set(_code_key(customer.pk), {'h': _hash_code(customer.pk, code), 'tries': 0}, CODE_TTL)

    channels = []
    # 📧 الإيميل: نظام الإيميل العادي بتاع السيستم (نفس اللي بيبعت للعملاء) — شغال دايماً.
    if customer.email and _send_otp_via_channel(customer.phone, code, email=customer.email, provider='email'):
        channels.append('email')
    # 📱 الموبايل: بس لو بوابة SMS/واتساب مضبوطة (مش console) ومش هي نفسها الإيميل.
    provider = getattr(settings, 'OTP_DELIVERY_PROVIDER', 'console')
    if provider not in ('console', 'email', '') and _send_otp_via_channel(customer.phone, code):
        channels.append(provider)
    if channels:
        logger.info("[MP PW-RESET] code sent to customer %s via %s", customer.pk, ','.join(channels))
    else:
        logger.error("[MP PW-RESET] delivery failed for customer %s (email=%s provider=%s)",
                     customer.pk, bool(customer.email), provider)
    return JsonResponse({"status": "sent", "message": GENERIC_SENT})


@require_POST
def password_forgot_confirm(request):
    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "بيانات غير صالحة"}, status=400)
    identifier = str(data.get('phone') or '').strip()
    code = ''.join(c for c in str(data.get('code') or '') if c.isdigit())
    password = str(data.get('password') or '')
    if not identifier or len(code) != 6:
        return JsonResponse({"error": "اكتب الكود اللي وصلك (٦ أرقام)"}, status=400)

    customer, _ = _find_customer(identifier)
    if customer is None:
        return JsonResponse({"error": BAD_CODE}, status=400)
    key = _code_key(customer.pk)
    entry = cache.get(key)
    if not entry:
        return JsonResponse({"error": BAD_CODE}, status=400)
    if not hmac.compare_digest(entry['h'], _hash_code(customer.pk, code)):
        entry['tries'] = entry.get('tries', 0) + 1
        if entry['tries'] >= MAX_ATTEMPTS:
            cache.delete(key)
        else:
            cache.set(key, entry, CODE_TTL)
        return JsonResponse({"error": BAD_CODE}, status=400)

    # الكود صح — نتحقق من قوة الباسورد قبل ما نستهلك الكود (عشان يقدر يصحّح ويعيد).
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError
    try:
        validate_password(password)
    except ValidationError as e:
        return JsonResponse({"error": ' • '.join(e.messages)}, status=400)

    with schema_context('public'):
        customer.set_password(password)
        customer.is_verified = True
        customer.session_token = uuid.uuid4()   # يخرّج أي جلسة قديمة
        customer.last_login_at = timezone.now()
        customer.save(update_fields=['password_hash', 'is_verified', 'session_token', 'last_login_at'])
    cache.delete(key)
    cache.delete(f'mp_login_fail_phone:{customer.phone}')
    logger.info("[MP PW-RESET] password reset OK for customer %s", customer.pk)

    response = JsonResponse({
        "status": "reset",
        "message": "تم تغيير كلمة السر ودخلت حسابك ✅",
        "redirect": "/marketplace/dashboard/",
    })
    response.set_cookie(
        'mp_session', str(customer.session_token),
        max_age=60 * 60 * 24 * 30, httponly=True, samesite='Lax',
        secure=not settings.DEBUG,
    )
    return response
