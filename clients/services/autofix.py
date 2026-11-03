"""🤖 المستوى ٣ — بوت التصليح: عطل جديد ⇒ GitHub Actions ⇒ Claude يكتب التصليح + اختبار ⇒ PR.

السيرفر بيبعت `repository_dispatch` (event_type=autofix-error) فيه المشكلة فين بالظبط
(الملف والسطر والـ traceback والصفحة)، والـ workflow `.github/workflows/autofix.yml`
بيشغّل Claude Code يصلّح ويكتب اختبار ويفتح PR. **مفيش نشر أوتوماتيك** — الـ PR بيعدّي على
CI كامل وصاحب المشروع بيوافق وينشر زي أي تعديل (السيستم فيه فلوس).

بيشتغل بس لو `AUTOFIX_GITHUB_TOKEN` متظبط (توكن GitHub بصلاحية Contents: write على الريبو).

خصوصية: مفيش أسماء شركات ولا مستخدمين ولا IPs ولا أرقام موبايلات/إيميلات بتطلع برّه —
الأرقام اللي في الروابط بتبقى {id} والرسالة والـ traceback بيتنضّفوا.
"""
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

EVENT_TYPE = 'autofix-error'
DEDUPE_TTL = 7 * 24 * 3600
_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
_LONG_NUM = re.compile(r'(?<![\w.])\+?\d[\d -]{6,}\d(?![\w.])')
_QUOTED = re.compile(r"(?<=[=:(,]\s)'[^'\n]{3,}'")   # قيم بين ' ' في رسائل SQL/Validation


def enabled():
    return bool(getattr(settings, 'AUTOFIX_GITHUB_TOKEN', '') and getattr(settings, 'AUTOFIX_GITHUB_REPO', ''))


def scrub(text, limit):
    text = _EMAIL.sub('<email>', text or '')
    text = _LONG_NUM.sub('<num>', text)
    text = _QUOTED.sub("'<value>'", text)
    return text[:limit]


def sent_at(sig):
    """امتى البصمة دي اتبعتت للبوت (نص ISO) — أو None."""
    return cache.get(f'autofix:sent:{sig}')


def build_payload(err):
    from clients.models import Client, SystemErrorLog
    from clients.services.error_autoheal import normalize_path, signature_of
    from clients.services.error_insight import code_location, explain
    sig = signature_of(err)
    i = explain(err)
    rd = err.request_data if isinstance(err.request_data, dict) else {}
    industry = (Client.all_objects.filter(schema_name=err.tenant_schema)
                .values_list('industry', flat=True).first()) or ''
    since = timezone.now() - timedelta(hours=24)
    base = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
    return {
        'signature': sig,
        'error_id': err.pk,
        'kind': i['kind'],
        'status_code': err.status_code,
        'method': err.method,
        'path': normalize_path(err.path),
        'page': str(i['page']),
        'from_page': normalize_path((i['from_page'] or {}).get('path', '')) if (i['from_page'] or {}).get('internal') else '',
        'exception_class': err.exception_class,
        'message': scrub(err.message, 800),
        'traceback': scrub(err.traceback, 6000),
        'code_at': code_location(err.traceback),
        'industry': industry,
        'logged_in': bool(err.user_id),
        'device': str(i['device']),
        'query_keys': sorted((rd.get('GET') or {}).keys())[:20],
        'occurrences_24h': SystemErrorLog.objects.filter(
            status_code=err.status_code, path=err.path, exception_class=err.exception_class,
            created_at__gte=since).count(),
        'error_center_url': f'https://{base}/superadmin/errors/?show=all',
    }


def dispatch(err, *, force=False):
    """يبعت الخطأ لبوت التصليح. بيرجّع (تم؟, رسالة بالعربي)."""
    if not enabled():
        return False, 'بوت التصليح مش متفعّل — محتاج AUTOFIX_GITHUB_TOKEN في .env'
    from clients.services.error_autoheal import signature_of
    sig = signature_of(err)
    if not force and sent_at(sig):
        return False, 'الخطأ ده اتبعت للبوت قبل كده (آخر ٧ أيام)'
    day_key = f"autofix:day:{timezone.localdate():%Y%m%d}"
    limit = int(getattr(settings, 'AUTOFIX_DAILY_LIMIT', 5) or 0)
    cache.add(day_key, 0, 26 * 3600)
    try:
        used = cache.incr(day_key)
    except ValueError:
        used = 1
    if used > limit:
        return False, f'وصلنا الحد اليومي لبوت التصليح ({limit}) — هيكمّل بكرة، أو اطلبه يدوي من مركز الأخطاء'

    import requests
    repo = settings.AUTOFIX_GITHUB_REPO
    try:
        r = requests.post(
            f'https://api.github.com/repos/{repo}/dispatches',
            json={'event_type': EVENT_TYPE, 'client_payload': {'error': build_payload(err)}},
            headers={'Authorization': f'Bearer {settings.AUTOFIX_GITHUB_TOKEN}',
                     'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28'},
            timeout=15,
        )
    except requests.RequestException as e:
        logger.warning("autofix dispatch failed: %s", e)
        return False, 'GitHub مش بيرد دلوقتي — جرّب تاني بعد شوية'
    if r.status_code != 204:
        logger.warning("autofix dispatch HTTP %s: %s", r.status_code, r.text[:300])
        return False, f'GitHub رفض الطلب ({r.status_code}) — راجع صلاحيات التوكن'
    cache.set(f'autofix:sent:{sig}', timezone.now().isoformat(timespec='minutes'), DEDUPE_TTL)
    return True, f'اتبعت لبوت التصليح — هيفتح PR على GitHub ({repo}) لما يخلص'
