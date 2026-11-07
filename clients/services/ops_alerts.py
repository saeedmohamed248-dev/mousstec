"""
🚨 تنبيهات التشغيل لصاحب المنصة
================================
فحص دوري (كل ١٠ دقايق من Celery beat) لقاعدة البيانات والكاش والهارد والذاكرة.
لو حاجة اتدهورت ⇒ إيميل لصاحب المنصة **قبل** ما العملاء يحسّوا.

🐛 [FIX]: حادثة Redis (كتابة ~٧٨ جيجا على الهارد) اتكشفت بعد ما Postgres اتخنق —
صفحة /superadmin/system/ كانت بتعرض المشكلة بس محدش بيبص عليها. دلوقتي بيوصلك إيميل.

* نفس المشكلة مابتتبعتش أكتر من مرة كل `OPS_ALERT_REPEAT_HOURS` (افتراضي ٦ ساعات).
* لما المشكلة تتحل بيوصلك إيميل «اتحلت».
* المستلمين: `OPS_ALERT_EMAILS` (مفصولة بفاصلة) وإلا إيميلات الـ superusers.
* `send_ops_alert()` بيستخدمه كمان `deploy/backup.sh` لو النسخة الاحتياطية فشلت
  (عن طريق `manage.py ops_alert`).
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMessage

logger = logging.getLogger(__name__)

_STATE_KEY = 'ops_alerts:active'      # {اسم الفحص: التفاصيل} للمشاكل المفتوحة
_SENT_KEY = 'ops_alerts:sent:{}'      # منع التكرار لكل فحص
_BAD = {'degraded', 'down'}


def alert_recipients() -> list[str]:
    raw = getattr(settings, 'OPS_ALERT_EMAILS', '') or ''
    emails = [e.strip() for e in str(raw).split(',') if e.strip()]
    if emails:
        return emails
    from django.contrib.auth import get_user_model
    from django_tenants.utils import schema_context
    with schema_context('public'):
        return list(
            get_user_model().objects.filter(is_superuser=True, is_active=True)
            .exclude(email='').values_list('email', flat=True)
        )


def send_ops_alert(subject: str, body: str) -> bool:
    """يبعت إيميل تنبيه. مابيرميش استثناء أبداً (التنبيه مايوقّعش الفحص)."""
    to = alert_recipients()
    if not to:
        logger.warning('ops_alert بدون مستلمين: %s', subject)
        return False
    try:
        EmailMessage(subject=f'[Mouss Tec] {subject}', body=body, to=to).send(fail_silently=False)
        return True
    except Exception:
        logger.exception('فشل إرسال تنبيه التشغيل: %s', subject)
        return False


def collect_checks() -> list[dict]:
    from clients.views.saas_admin_views import _check_cache, _check_database, _check_resources
    checks = []
    for fn in (_check_database, _check_cache):
        try:
            checks.append(fn())
        except Exception as e:      # الفحص نفسه وقع ⇒ ده في حد ذاته مشكلة
            checks.append({'name': fn.__name__, 'status': 'down', 'detail': str(e)[:120]})
    checks += _check_resources()
    return checks


def run_ops_health_check() -> dict:
    """يرجّع {'problems': [...], 'sent': n, 'recovered': [...]}."""
    repeat_secs = int(getattr(settings, 'OPS_ALERT_REPEAT_HOURS', 6)) * 3600
    checks = collect_checks()
    problems = {c['name']: c for c in checks if c.get('status') in _BAD}
    # الكاش نفسه ممكن يكون هو اللي واقع — الحالة تتقرا وتتكتب من غير ما توقّع الفحص
    try:
        previous = cache.get(_STATE_KEY) or {}
    except Exception:
        previous = {}

    sent = 0
    for name, c in problems.items():
        key = _SENT_KEY.format(name)
        try:
            if cache.get(key):
                continue
        except Exception:
            pass
        level = '⛔ واقع' if c['status'] == 'down' else '⚠️ في خطر'
        body = (f'{level}: {name}\n{c.get("detail", "")}\n\n'
                'كل الفحوصات:\n' +
                '\n'.join(f'• {x["name"]}: {x.get("status")} — {x.get("detail", "")}' for x in checks) +
                '\n\nالتفاصيل: /superadmin/system/')
        if send_ops_alert(f'{level}: {name}', body):
            sent += 1
            try:
                cache.set(key, 1, repeat_secs)
            except Exception:
                pass

    recovered = [n for n in previous if n not in problems]
    for name in recovered:
        send_ops_alert(f'✅ اتحلت: {name}', f'{name} رجع طبيعي.')
        try:
            cache.delete(_SENT_KEY.format(name))
        except Exception:
            pass
    try:
        cache.set(_STATE_KEY, {n: c.get('detail', '') for n, c in problems.items()}, None)
    except Exception:
        pass
    return {'problems': list(problems), 'sent': sent, 'recovered': recovered}
