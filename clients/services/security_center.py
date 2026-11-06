"""🛡️ مركز الأمان (السوبر أدمن) — بيانات حقيقية من السجلات، وإجراءات سريعة.

* محاولات الدخول الفاشلة والحسابات المقفولة (django-axes) — من كل الشركات
  (axes شغال جوه schema كل شركة لوحدها + الـ public).
* السكانرات: طلبات على مسارات ثغرات (.env / .git / wp-admin …) من SystemErrorLog، مجمّعة بالـ IP.
* أكتر IPs بتعمل أخطاء 4xx.
* حظر/رفع حظر IP (clients.models.BlockedIP) وفك قفل دخول — وكل إجراء بيتسجّل في PlatformEvent.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Max, Q
from django.utils import timezone
from django_tenants.utils import get_tenant_model, schema_context

logger = logging.getLogger('mouss_tec_core')

MAX_SCHEMAS = 400   # سقف أمان للّفة على الشركات


def _cooloff():
    v = getattr(settings, 'AXES_COOLOFF_TIME', 1)
    return v if isinstance(v, timedelta) else timedelta(hours=float(v or 0))


def _schemas():
    names = ['public']
    names += list(get_tenant_model().objects.exclude(schema_name='public')
                  .order_by('-id').values_list('schema_name', flat=True)[:MAX_SCHEMAS])
    return names


def failed_logins(hours=24):
    """محاولات الدخول الفاشلة من كل الشركات: (صفوف، إجمالي، عدد المقفولين)."""
    from axes.models import AccessAttempt
    since = timezone.now() - timedelta(hours=hours)
    lock_after = getattr(settings, 'AXES_FAILURE_LIMIT', 5)
    lock_since = timezone.now() - _cooloff()
    names = dict(get_tenant_model().objects.values_list('schema_name', 'name'))
    rows = []
    for schema in _schemas():
        try:
            with schema_context(schema):
                for a in AccessAttempt.objects.filter(attempt_time__gte=since).order_by('-attempt_time')[:200]:
                    rows.append({
                        'schema': schema, 'tenant': names.get(schema, '') if schema != 'public' else 'المنصة',
                        'ip': a.ip_address, 'username': a.username, 'failures': a.failures_since_start,
                        'last': a.attempt_time, 'path': a.path_info, 'ua': (a.user_agent or '')[:120],
                        'locked': a.failures_since_start >= lock_after and a.attempt_time >= lock_since,
                    })
        except Exception:  # noqa: BLE001 — شركة schema بايظة مابتوقفش الصفحة
            logger.warning("[SECURITY] reading axes attempts from %s failed", schema, exc_info=True)
    rows.sort(key=lambda r: (not r['locked'], -r['failures'], -r['last'].timestamp()))
    return rows, sum(r['failures'] for r in rows), sum(1 for r in rows if r['locked'])


def unlock_login(schema, ip=None, username=None):
    """فك قفل دخول (يمسح محاولات axes للـ IP/المستخدم في الشركة دي). بيرجّع العدد."""
    from axes.models import AccessAttempt
    if not (ip or username):
        return 0
    with schema_context(schema or 'public'):
        q = Q()
        if ip:
            q |= Q(ip_address=ip)
        if username:
            q |= Q(username=username)
        n, _ = AccessAttempt.objects.filter(q).delete()
    return n


def scanner_ips(hours=24, limit=30):
    """IPs بتجرّب مسارات ثغرات (.env/.git/wp-…) — مجمّعة: عدد، آخر مرة، عينة مسارات، الشركات."""
    from clients.models import SystemErrorLog
    from clients.services.error_insight import SCANNER_PATH_SQL
    since = timezone.now() - timedelta(hours=hours)
    base = SystemErrorLog.objects.filter(created_at__gte=since, ip_address__isnull=False,
                                         path__iregex=SCANNER_PATH_SQL)
    rows = list(base.values('ip_address').annotate(n=Count('id'), last=Max('created_at'),
                                                   tenants=Count('tenant_schema', distinct=True))
                .order_by('-n')[:limit])
    blocked = _blocked_map()
    for r in rows:
        r['paths'] = list(base.filter(ip_address=r['ip_address']).values_list('path', flat=True)
                          .distinct()[:5])
        r['blocked'] = blocked.get(r['ip_address'])
    return rows, base.count()


def noisy_ips(hours=24, limit=15):
    """أكتر IPs بتعمل أخطاء (4xx/5xx) — ممكن بوت، أو عميل واقف في مشكلة."""
    from clients.models import SystemErrorLog
    since = timezone.now() - timedelta(hours=hours)
    rows = list(SystemErrorLog.objects.filter(created_at__gte=since, ip_address__isnull=False)
                .values('ip_address').annotate(
                    n=Count('id'), last=Max('created_at'),
                    tenants=Count('tenant_schema', distinct=True),
                    users=Count('user_id', distinct=True),
                    server=Count('id', filter=Q(status_code__gte=500)))
                .order_by('-n')[:limit])
    blocked = _blocked_map()
    for r in rows:
        r['blocked'] = blocked.get(r['ip_address'])
    return rows


def _blocked_map():
    from clients.models import BlockedIP
    now = timezone.now()
    return {b.ip_address: b for b in BlockedIP.objects.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))}


def blocked_list():
    from clients.models import BlockedIP
    return list(BlockedIP.objects.order_by('-created_at')[:200])


DURATIONS = {'1h': timedelta(hours=1), '24h': timedelta(hours=24), '7d': timedelta(days=7), 'forever': None}


def block_ip(ip, *, duration='24h', reason='', user=None, actor_ip=None):
    """(تم؟, رسالة). بيرفض IP داخلي أو IP اللي بيحظر نفسه."""
    from clients.models import BlockedIP, PlatformEvent
    from erp_core.ip_block import can_block, invalidate
    ok, why = can_block(ip, actor_ip=actor_ip)
    if not ok:
        return False, why
    if duration not in DURATIONS:
        duration = '24h'
    delta = DURATIONS[duration]
    expires = timezone.now() + delta if delta else None
    obj, created = BlockedIP.objects.update_or_create(
        ip_address=ip.strip(),
        defaults={'reason': (reason or '')[:300], 'created_by': getattr(user, 'username', '') or '',
                  'expires_at': expires})
    invalidate()
    PlatformEvent.objects.create(
        event_type='other', user_name=getattr(user, 'username', '') or '', ip_address=actor_ip,
        description=f"🛡️ حظر IP {obj.ip_address} ({duration}) — {reason}"[:500])
    return True, f"اتحظر {obj.ip_address} {'لحد ما ترفع الحظر' if expires is None else 'لحد ' + timezone.localtime(expires).strftime('%Y-%m-%d %H:%M')}."


def unblock_ip(ip, *, user=None, actor_ip=None):
    from clients.models import BlockedIP, PlatformEvent
    from erp_core.ip_block import invalidate
    n, _ = BlockedIP.objects.filter(ip_address=(ip or '').strip()).delete()
    invalidate()
    if n:
        PlatformEvent.objects.create(
            event_type='other', user_name=getattr(user, 'username', '') or '', ip_address=actor_ip,
            description=f"🛡️ رفع الحظر عن IP {ip}"[:500])
    return n


def recent_security_events(limit=30):
    """أحداث حساسة: حظر/رفع حظر، تعليق، احتيال، حذف شركة، دخول السوبر أدمن على شركة (event_type=login)."""
    from clients.models import PlatformEvent
    return list(PlatformEvent.objects.filter(
        Q(event_type__in=('login', 'suspension', 'fraud_flag', 'tenant_deleted'))
        | Q(description__startswith='🛡️')).order_by('-timestamp')[:limit])
