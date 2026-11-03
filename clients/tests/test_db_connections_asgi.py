"""
🛑 الموقع كله وقع بـ «FATAL: sorry, too many clients already».

الويب شغال على daphne (ASGI): كل طلب بيتنفّذ في thread مختلف، ومع اتصالات دائمة
(CONN_MAX_AGE > 0) كل thread بيسيب اتصاله بقاعدة البيانات مفتوح ⇒ بتتراكم لحد سقف
Postgres. الاختبار ده بيمنع إن حد يرجّعها تاني للويب.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class AsgiDatabaseConnectionsTests(SimpleTestCase):
    def test_persistent_connections_are_off_by_default(self):
        self.assertEqual(settings.DATABASES['default']['CONN_MAX_AGE'], 0)

    def test_web_service_forces_no_persistent_connections(self):
        compose = (Path(settings.BASE_DIR) / 'docker-compose.yml').read_text(encoding='utf-8')
        web = re.search(r'^  web:\n(.*?)^  \w+:\n', compose, re.S | re.M).group(1)
        self.assertIn('daphne', Path(settings.BASE_DIR, 'deploy/entrypoint.web.sh').read_text(encoding='utf-8'))
        # ثابت في الـ compose (مش ${…}) عشان قيمة في .env ماتقدرش تفتحها تاني
        self.assertRegex(web, r'\n\s+CONN_MAX_AGE: "0"\n')
