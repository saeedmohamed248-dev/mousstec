"""
🏪 بائع السوق — مطبعة أو محل قطع غيار بيعرض منتجاته ويتابع عملاءه من غير برنامج الإدارة.

* الدخول بالموبايل + كلمة سر، والجلسة في كوكي ``ms_session`` (منفصلة عن عميل السوق ``mp_session``).
* الإشعارات (طلب سعر جديد على منتجاته / طلب جديد في السوق المفتوح / عرضه اتقبل) واتساب + إيميل.
* الترقية لبرنامج الإدارة: التسجيل العادي ``/connect/signup/`` وهو داخل كبائع ⇒ الشركة الجديدة
  بتتربط بحسابه (``MarketSeller.upgraded_to``) ومتجره ومنتجاته وعروضه بيفضلوا زي ما هم.
"""
from __future__ import annotations

import logging
import threading

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction

log = logging.getLogger('mouss_tec_core')

COOKIE = 'ms_session'
COOKIE_AGE = 60 * 60 * 24 * 30


def normalize_phone(raw: str) -> str:
    """01xxxxxxxxx / 2001… / +20… ⇒ +201xxxxxxxxx (نفس قاعدة عميل السوق). غير كده ⇒ ''."""
    raw = (raw or '').strip().translate(str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789'))
    digits = ''.join(c for c in raw if c.isdigit())
    if digits.startswith('0020'):
        digits = digits[2:]
    if digits.startswith('20') and len(digits) == 12:
        digits = digits[2:]
    digits = digits.lstrip('0')
    if len(digits) == 10 and digits.startswith('1'):
        return f'+20{digits}'
    return ''


def local_phone(phone: str) -> str:
    return phone.replace('+20', '0', 1) if phone.startswith('+20') else phone


def seller_from_request(request):
    """البائع من كوكي الجلسة — أو None (كوكي ناقص/مكسور/حساب موقوف)."""
    from clients.models import MarketSeller
    token = request.COOKIES.get(COOKIE)
    if not token:
        return None
    try:
        return MarketSeller.objects.select_related('store').get(session_token=token, is_blocked=False)
    except (MarketSeller.DoesNotExist, ValueError, ValidationError):
        return None


def login_response(response, seller):
    response.set_cookie(COOKIE, str(seller.session_token), max_age=COOKIE_AGE, httponly=True,
                        samesite='Lax', secure=not settings.DEBUG)
    return response


def _base_url():
    return (getattr(settings, 'PRINT_MARKET_BASE_URL', '') or 'https://mousstec.com').rstrip('/')


def _send(seller_id, subject, line, path):
    from clients.models import MarketSeller
    seller = MarketSeller.objects.select_related('store').filter(pk=seller_id, is_blocked=False).first()
    if seller is None:
        return
    link = f'{_base_url()}{path}'
    if seller.email:
        try:
            from django.core.mail import send_mail
            send_mail(subject, f'أهلاً {seller.owner_name}،\n\n{line}\n\n{link}\n\n— سوق Mouss Tec',
                      settings.DEFAULT_FROM_EMAIL, [seller.email], fail_silently=True)
        except Exception:  # noqa: BLE001
            log.exception('[SELLER NOTIFY] email failed for seller %s', seller_id)
    if getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '') and getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', ''):
        try:
            from clients.services.whatsapp_notify import send_platform_whatsapp
            send_platform_whatsapp(seller.phone, text=f'🛍️ سوق Mouss Tec\n{seller.store.name}، {line}\n{link}'[:1500])
        except Exception:  # noqa: BLE001
            log.exception('[SELLER NOTIFY] whatsapp failed for seller %s', seller_id)


def notify_seller(seller, subject, line, path='/marketplace/seller/'):
    """إشعار البائع بعد الـ commit — في thread عشان الصفحة ماتستناش (أو على طول في الاختبارات)."""
    seller_id = seller.pk

    def _run():
        if getattr(settings, 'CATALOG_NOTIFY_SYNC', False):
            _send(seller_id, subject, line, path)
            return

        def _thread():
            try:
                _send(seller_id, subject, line, path)
            finally:
                connection.close()
        threading.Thread(target=_thread, daemon=True).start()

    transaction.on_commit(_run)


def notify_sellers_new_request(svc_request):
    """🔔 طلب اتوافق عليه في السوق المفتوح ⇒ كل بائع متجره ظاهر في نفس القطاع."""
    from clients.models import MarketSeller
    sellers = MarketSeller.objects.filter(store__sector=svc_request.sector, store__is_active=True,
                                          is_blocked=False, upgraded_to__isnull=True)
    budget = f' — الميزانية حتى {svc_request.max_budget:g} ج.م' if svc_request.max_budget else ''
    line = f'طلب جديد في السوق المفتوح: «{svc_request.title[:120]}»{budget} — قدّم عرضك قبل غيرك.'
    n = 0
    for s in sellers:
        notify_seller(s, f'Mouss Tec — طلب جديد: {svc_request.title[:80]}', line,
                      f'/marketplace/seller/requests/?req={svc_request.request_code}')
        n += 1
    return n


def link_upgrade(request, tenant):
    """⬆️ بائع داخل بحسابه وسجّل شركة ⇒ نربطها بيه (مرة واحدة). مابيوقّعش التسجيل لو فشل."""
    try:
        seller = seller_from_request(request)
        if seller is None or seller.upgraded_to_id:
            return None
        from clients.models import MarketSeller
        MarketSeller.objects.filter(pk=seller.pk, upgraded_to__isnull=True).update(upgraded_to=tenant)
        return seller
    except Exception:  # noqa: BLE001
        log.exception('[SELLER] upgrade link failed')
        return None
