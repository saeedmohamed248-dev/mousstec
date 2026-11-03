"""
💓 /healthz/ — فحص خفيف وعام: «السيرفر ده يقدر يخدم ناس دلوقتي؟»

بيستخدمه:
* Docker (healthcheck في docker-compose) + حارس autoheal: لو الويب فشل الفحص كام مرة ورا بعض
  بيتعمله restart لوحده — زي يوم «too many clients» اللي الـ restart كان هو الحل.
* سكربت النشر (deploy/deploy.sh): النسخة الجديدة ماتاخدش زوار غير لما تعدّي الفحص.
* أي مراقبة خارجية (UptimeRobot / Better Stack).

أول middleware في القائمة: بيرد قبل عزل الـ tenants وقبل فحص الـ Host، فبيشتغل على أي دومين
(حتى http://127.0.0.1:8000/healthz/ من جوه الكونتينر). مابيكشفش أي تفاصيل.
"""
from __future__ import annotations

import logging

from django.db import connection
from django.http import HttpResponse

logger = logging.getLogger('mouss_tec_core')

HEALTHZ_PATH = '/healthz/'


def _db_ok() -> bool:
    try:
        with connection.cursor() as cur:
            cur.execute('SELECT 1')
            cur.fetchone()
        return True
    except Exception:
        logger.warning('[HEALTHZ] database check failed', exc_info=True)
        return False


class HealthzMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path != HEALTHZ_PATH:
            return self.get_response(request)
        ok = _db_ok()
        resp = HttpResponse('ok' if ok else 'db unavailable', status=200 if ok else 503,
                            content_type='text/plain')
        resp['Cache-Control'] = 'no-store'
        return resp
