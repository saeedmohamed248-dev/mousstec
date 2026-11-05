"""🩺 المستوى ٢ — علاج وتنبيه تلقائي للأخطاء (من غير ما حد يفتح مركز الأخطاء).

* بصمة لكل خطأ (`signature`) — نفس العطل في نفس الدالة = نفس البصمة حتى لو الرابط فيه أرقام
  مختلفة (/orders/12/ و /orders/99/) — فبنعرف «ده جديد» ولا «ده بيتكرر».
* `on_error_recorded` بتتنده بعد تسجيل أي خطأ حقيقي (مش بوت) وبتقرر من عدّادات في الكاش
  (من غير queries تقيلة في طلب العميل):
    - 🔥 عطل 500 أول مرة يظهر (آخر ٧ أيام)  ⇒ إيميل فوري للمالك + يتبعت لبوت التصليح (المستوى ٣)
    - 📈 نفس الخطأ اتكرر كتير في ١٠ دقايق       ⇒ إيميل («حاجة بايظة دلوقتي»)
    - 🏢 نفس الخطأ عند ٣ شركات أو أكتر في ساعة  ⇒ إيميل («مشكلة عامة مش عند عميل واحد»)
  كل تنبيه بيتبعت مرة واحدة في الساعة لنفس البصمة — مفيش سيل إيميلات.
* `autoclose_stale` (يومي) — خطأ مفتوح ماتكررش ١٤ يوم ⇒ بيتقفل لوحده (زي Sentry).
"""
import hashlib
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from clients.services.error_insight import classify, code_location, explain

logger = logging.getLogger(__name__)

NEW_SIGNATURE_WINDOW = 7 * 24 * 3600     # «جديد» = مااتشافش آخر ٧ أيام
SPIKE_WINDOW_MIN = 10
SPIKE_LIMITS = {'code': 5, 'csrf': 15, 'bad_request': 25, 'broken_link': 20, 'permission': 20, 'not_found': 40}
TENANTS_ALERT_AT = 3
ALERT_COOLDOWN = 3600
STALE_DAYS = 14

_ID_SEGMENT = re.compile(r'/(\d+|[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{24,})(?=/|$)', re.I)


def normalize_path(path):
    """/orders/12/edit/ ⇒ /orders/{id}/edit/ — عشان نفس الصفحة تتجمّع مع بعض."""
    return _ID_SEGMENT.sub('/{id}', path or '')


def signature(status, path, exception_class='', tb=''):
    loc = code_location(tb) if tb else None
    if exception_class or (status or 0) >= 500:
        where = f"{loc['file']}:{loc['func']}" if loc else normalize_path(path)
        key = f"500|{exception_class}|{where}"
    else:
        key = f"{status}|{normalize_path(path)}"
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]


def signature_of(err):
    return signature(err.status_code, err.path, err.exception_class, err.traceback)


def _kind(err):
    rd = err.request_data if isinstance(err.request_data, dict) else {}
    return classify(err.status_code, err.path, err.user_id, err.exception_class,
                    rd.get('UA', ''), rd.get('REF', ''), err.method)


def _bump(key, ttl):
    try:
        return cache.incr(key)
    except ValueError:
        cache.add(key, 0, ttl)
        try:
            return cache.incr(key)
        except ValueError:
            return 1


def _enqueue(task, *args):
    try:
        task.delay(*args)
    except Exception:  # noqa: BLE001 — الـ broker واقع: التنبيه مايوقعش طلب العميل
        logger.exception("error_autoheal: couldn't enqueue %s", getattr(task, 'name', task))


def on_error_recorded(err, kind=None):
    """بتتنده بعد ما ErrorTrackingMiddleware تسجّل خطأ مفتوح. بترجّع أسباب التنبيه (للاختبارات)."""
    kind = kind or _kind(err)
    if kind in ('bot', 'asset'):
        return []
    sig = signature_of(err)
    reasons = []

    if kind == 'code' and cache.add(f'errsig:seen:{sig}', 1, NEW_SIGNATURE_WINDOW):
        reasons.append('new')

    bucket = timezone.now().strftime('%Y%m%d%H') + str(timezone.now().minute // SPIKE_WINDOW_MIN)
    n = _bump(f'errsig:n:{sig}:{bucket}', SPIKE_WINDOW_MIN * 60 + 60)
    if n == SPIKE_LIMITS.get(kind, 30):
        reasons.append('spike')

    if err.tenant_schema and err.tenant_schema != 'public':
        hour = timezone.now().strftime('%Y%m%d%H')
        if cache.add(f'errsig:t:{sig}:{hour}:{err.tenant_schema}', 1, 3700):
            if _bump(f'errsig:tn:{sig}:{hour}', 3700) == TENANTS_ALERT_AT:
                reasons.append('tenants')

    reasons = [r for r in reasons if r == 'new' or cache.add(f'errsig:alerted:{r}:{sig}', 1, ALERT_COOLDOWN)]
    if not reasons:
        return []

    from clients import tasks
    _enqueue(tasks.send_error_alert, err.pk, reasons)
    if 'new' in reasons:
        from clients.services import autofix
        if autofix.enabled():
            _enqueue(tasks.request_error_autofix, err.pk)
    return reasons


_REASON_TEXT = {
    'new':     '🔥 عطل جديد أول مرة يظهر',
    'spike':   '📈 الخطأ ده بيتكرر كتير دلوقتي (أكتر من الطبيعي في ١٠ دقايق)',
    'tenants': f'🏢 نفس الخطأ ظهر عند {TENANTS_ALERT_AT} شركات أو أكتر في ساعة — مشكلة عامة',
}


def alert_text(err, reasons):
    """(عنوان، نص) إيميل التنبيه — بالعربي وفيه المشكلة فين بالظبط."""
    from clients.models import Domain
    hosts = Domain.objects.filter(tenant__schema_name=err.tenant_schema).values_list('domain', flat=True)
    i = explain(err, set(hosts))
    base = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
    lines = [' + '.join(_REASON_TEXT[r] for r in reasons), '',
             f"الشركة: {err.tenant_name or '—'} ({err.tenant_schema or 'public'})",
             f"النوع: {i['label']} — {err.status_code}",
             f"الصفحة: {i['page']}  [{err.method} {err.path}]"]
    if i['from_page']:
        lines.append(f"كان واقف في: {i['from_page']['title']}  [{i['from_page']['path']}]")
    if i['code_at']:
        c = i['code_at']
        lines.append(f"في الكود: {c['file']}:{c['line']} — {c['func']}()\n    {c['src']}")
    if err.exception_class:
        lines.append(f"الاستثناء: {err.exception_class}: {err.message[:300]}")
    lines += [f"مين: {i['who']} · {i['device']}", f"المطلوب: {i['hint']}", '',
              f"مركز الأخطاء: https://{base}/superadmin/errors/?show=open&tenant={err.tenant_schema}"]
    subject = f"[Mouss Tec] {_REASON_TEXT[reasons[0]].split(' ', 1)[1]} — {err.status_code} {normalize_path(err.path)[:60]}"
    return subject, '\n'.join(str(x) for x in lines)


def autoclose_stale(days=STALE_DAYS):
    """يقفل الأخطاء المفتوحة اللي ماتكررتش `days` يوم + مسارات السكانرات. بيرجّع العدد."""
    from django.db.models import Exists, OuterRef
    from clients.models import SystemErrorLog
    from clients.services.error_insight import SCANNER_PATH_SQL
    now = timezone.now()
    # 🤖 مسارات السكانرات (actuator / wp- / .env …) اللي اتسجّلت مفتوحة قبل التصنيف الجديد — تتقفل فوراً
    scanners = (SystemErrorLog.objects
                .filter(is_resolved=False, status_code__lt=500, exception_class='', path__iregex=SCANNER_PATH_SQL)
                .update(is_resolved=True, resolved_at=now, resolved_by=None))
    # 🤖 POST مرفوض (403) من زائر مش داخل ومن غير Referer = بوت (error_insight.classify) — اللي اتسجّل
    #    قبل التصنيف ده كان «فشل تحقق الأمان — مفتوح».
    scanners += (SystemErrorLog.objects
                 .filter(is_resolved=False, status_code=403, method='POST', user_id__isnull=True,
                         exception_class='', request_data__REF='')
                 .update(is_resolved=True, resolved_at=now, resolved_by=None))
    cutoff = now - timedelta(days=days)
    recent = SystemErrorLog.objects.filter(
        status_code=OuterRef('status_code'), path=OuterRef('path'),
        exception_class=OuterRef('exception_class'), created_at__gte=cutoff)
    return (SystemErrorLog.objects
            .filter(is_resolved=False, created_at__lt=cutoff)
            .filter(~Exists(recent))
            .update(is_resolved=True, resolved_at=now, resolved_by=None)) + scanners
