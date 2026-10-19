"""🧪 مشغّل الاختبارات (TEST_RUNNER) — DiscoverRunner + تنظيف اتصالات قاعدة البيانات.

اختبارات الـ WebSocket (channels) بتشغّل استعلامات الـ ORM في threads خلفية
(database_sync_to_async)، وكل thread بيفتح اتصال خاص بيه مابيتقفلش. في الآخر
`DROP DATABASE test_…` كان بيفشل «is being accessed by other users» — السويت
كلها OK بس الأمر بيخرج بـ 1، فـ CI يقع على الفاضي.
"""
from django.db import connection
from django.test.runner import DiscoverRunner


def terminate_stray_sessions():
    """يقفل أي اتصال تاني على قاعدة الاختبار (غير اتصالنا) قبل ما تتمسح."""
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()")


class ERPTestRunner(DiscoverRunner):
    def teardown_databases(self, old_config, **kwargs):
        terminate_stray_sessions()
        super().teardown_databases(old_config, **kwargs)
