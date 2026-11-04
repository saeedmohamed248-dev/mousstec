"""🔑 استرجاع كلمة سر عميل السوق (Marketplace) بكود على الموبايل.

قبل كده: مفيش أي طريقة لعميل نسي الباسورد يرجع حسابه، والحسابات القديمة اللي من غير
باسورد كان أي حد يعرف رقمها يدخل عليها ويحط لها باسورد من عنده (استيلاء على الحساب).

الخطوات:
  GET  /marketplace/password/forgot/          ← الصفحة
  POST /marketplace/password/forgot/send/     {phone}                  ← يبعت كود ٦ أرقام
  POST /marketplace/password/forgot/confirm/  {phone, code, password}  ← يغيّر الباسورد ويدخّله

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

GENERIC_SENT = ("لو الرقم ده متسجّل عندنا، هيوصلك كود من ٦ أرقام خلال دقيقة. "
                "لو ماوصلش، كلّم الدعم.")
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


def _code_key(phone):
    return f'mp_pwreset:{phone}'


def _hash_code(phone, code):
    return hmac.new(settings.SECRET_KEY.encode(), f'{phone}:{code}'.encode(), hashlib.sha256).hexdigest()


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
    phone = normalize_phone(str(data.get('phone') or ''))
    if len(phone) < 10:
        return JsonResponse({"error": "اكتب رقم الموبايل صح"}, status=400)

    ip = client_ip(request) or 'unknown'
    if _hit(f'mp_pwreset_ip:{ip}', 3600) > REQUESTS_PER_IP_HOUR:
        return JsonResponse({"error": "طلبات كتير — حاول بعد ساعة."}, status=429)

    with schema_context('public'):
        customer = MarketplaceCustomer.objects.filter(
            phone=phone, is_blocked=False, is_deleted=False).first()

    # الرد نفسه في كل الحالات — عشان محدش يعرف من الصفحة دي مين عنده حساب.
    if customer is None:
        return JsonResponse({"status": "sent", "message": GENERIC_SENT})
    if _hit(f'mp_pwreset_sends:{phone}', 3600) > SENDS_PER_PHONE_HOUR:
        return JsonResponse({"status": "sent", "message": GENERIC_SENT})

    code = f'{secrets.randbelow(1_000_000):06d}'
    cache.set(_code_key(phone), {'h': _hash_code(phone, code), 'tries': 0}, CODE_TTL)
    sent = _send_otp_via_channel(phone, code, email=customer.email or '')
    if not sent:
        logger.error("[MP PW-RESET] delivery failed for %s*** (provider=%s)",
                     phone[:6], getattr(settings, 'OTP_DELIVERY_PROVIDER', ''))
    else:
        logger.info("[MP PW-RESET] code sent to %s***", phone[:6])
    return JsonResponse({"status": "sent", "message": GENERIC_SENT})


@require_POST
def password_forgot_confirm(request):
    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "بيانات غير صالحة"}, status=400)
    phone = normalize_phone(str(data.get('phone') or ''))
    code = ''.join(c for c in str(data.get('code') or '') if c.isdigit())
    password = str(data.get('password') or '')
    if not phone or len(code) != 6:
        return JsonResponse({"error": "اكتب الكود اللي وصلك (٦ أرقام)"}, status=400)

    key = _code_key(phone)
    entry = cache.get(key)
    if not entry:
        return JsonResponse({"error": BAD_CODE}, status=400)
    if not hmac.compare_digest(entry['h'], _hash_code(phone, code)):
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
        customer = MarketplaceCustomer.objects.filter(
            phone=phone, is_blocked=False, is_deleted=False).first()
        if customer is None:
            cache.delete(key)
            return JsonResponse({"error": BAD_CODE}, status=400)
        customer.set_password(password)
        customer.is_verified = True
        customer.session_token = uuid.uuid4()   # يخرّج أي جلسة قديمة
        customer.last_login_at = timezone.now()
        customer.save(update_fields=['password_hash', 'is_verified', 'session_token', 'last_login_at'])
    cache.delete(key)
    cache.delete(f'mp_login_fail_phone:{phone}')
    logger.info("[MP PW-RESET] password reset OK for %s***", phone[:6])

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
