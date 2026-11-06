"""🛡️ حظر IP من «مركز الأمان» (clients.models.BlockedIP).

أول حاجة بعد /healthz/: أي طلب من IP محظور بياخد 403 قبل أي tenant/DB تانية.

الأمان قبل كل حاجة:
* قايمة الـ IPs المحظورة بتتقري من الداتا بيز مرة كل ٣٠ ثانية (كاش في الذاكرة) —
  مش query مع كل طلب. أي تعديل (حظر/رفع حظر) بيمسح الكاش فوراً في نفس العملية.
* أي خطأ (الجدول مش موجود لسه، الداتا بيز واقعة…) = الطلب يعدّي عادي. الحظر
  عمره ما يقفل الموقع على الكل.
* IP داخلي/محلي عمره ما يتحظر (can_block)، ولا الـ IP اللي بيعمل الحظر.
* طوارئ من السيرفر: `python manage.py unblock_ip --all`.
"""
import ipaddress
import logging
import time

from django.http import HttpResponseForbidden
from django.utils import timezone

from erp_core.client_ip import client_ip

logger = logging.getLogger('mouss_tec_core')

_TTL = 30
_cache = {'at': 0.0, 'ips': frozenset()}


def invalidate():
    _cache['at'] = 0.0


def can_block(ip, *, actor_ip=None):
    """(مسموح؟, السبب) — مايتحظرش IP غلط أو داخلي أو بتاع اللي بيحظر."""
    try:
        addr = ipaddress.ip_address((ip or '').strip())
    except ValueError:
        return False, 'ده مش IP سليم.'
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_unspecified:
        return False, 'ده IP داخلي (السيرفر نفسه أو شبكة داخلية) — مينفعش يتحظر.'
    if actor_ip and str(addr) == str(actor_ip):
        return False, 'ده الـ IP بتاعك انت — الحظر هيقفل عليك الموقع.'
    return True, ''


def _active_ips():
    now = time.monotonic()
    if now - _cache['at'] < _TTL:
        return _cache['ips']
    from django.db import connection
    from django.db.models import Q
    from clients.models import BlockedIP
    connection.set_schema_to_public()
    ips = frozenset(BlockedIP.objects.filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())).values_list('ip_address', flat=True))
    _cache.update(at=now, ips=ips)
    return ips


def _count_hit(ip):
    try:
        from django.db.models import F
        from clients.models import BlockedIP
        BlockedIP.objects.filter(ip_address=ip).update(hits=F('hits') + 1)
    except Exception:  # noqa: BLE001 — العدّاد مايوقّفش حاجة
        pass


class BlockedIPMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            ip = client_ip(request)
            blocked = bool(ip) and ip in _active_ips()
        except Exception:  # noqa: BLE001 — fail-open: الحظر مايقفلش الموقع
            logger.exception("[IP BLOCK] check failed — letting the request through")
            blocked = False
        if blocked:
            _count_hit(ip)
            return HttpResponseForbidden('Forbidden', content_type='text/plain')
        return self.get_response(request)
