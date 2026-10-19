"""🧪 مشغّل الاختبارات (TEST_RUNNER) — DiscoverRunner + تنظيف اتصالات قاعدة البيانات.

اختبارات الـ WebSocket (channels) بتشغّل استعلامات الـ ORM في threads خلفية
(database_sync_to_async)، وكل thread بيفتح اتصال خاص بيه مابيتقفلش. في الآخر
`DROP DATABASE test_…` كان بيفشل «is being accessed by other users» — السويت
كلها OK بس الأمر بيخرج بـ 1، فـ CI يقع على الفاضي.

وفي GitHub Actions: كل اختبار وقع بيطلع annotation على الـ PR نفسه (الملف والسطر
والرسالة) — مش لازم حد يفتح لوج ١٥٠٠ سطر عشان يعرف إيه اللي وقع.
"""
import os
import traceback
import unittest
from pathlib import Path

from django.conf import settings
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


def _escape(text):
    # صيغة أوامر GitHub Actions: % و\r و\n لازم يتعملهم escape
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def github_annotation(test, err, kind):
    """سطر `::error` للاختبار اللي وقع: آخر frame جوه المشروع = الملف والسطر."""
    exc_type, exc, tb = err
    root = str(Path(settings.BASE_DIR).resolve())
    where = ''
    for frame in reversed(traceback.extract_tb(tb)):
        if frame.filename.startswith(root) and '/site-packages/' not in frame.filename:
            where = f'file={os.path.relpath(frame.filename, root)},line={frame.lineno},'
            break
    message = ''.join(traceback.format_exception_only(exc_type, exc)).strip()[-1500:]
    return f'::error {where}title={_escape(f"{kind}: {test.id()}")}::{_escape(message)}'


class GitHubAnnotationsResult(unittest.TextTestResult):
    # السطر الجديد الأول: في وضع النقط (-v1) الأمر لازم يبدأ سطر لوحده عشان GitHub يقراه
    def addError(self, test, err):
        super().addError(test, err)
        self.stream.write('\n' + github_annotation(test, err, 'ERROR') + '\n')

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.stream.write('\n' + github_annotation(test, err, 'FAIL') + '\n')


class ERPTestRunner(DiscoverRunner):
    def get_resultclass(self):
        resultclass = super().get_resultclass()
        if resultclass is None and os.getenv('GITHUB_ACTIONS') == 'true':
            return GitHubAnnotationsResult
        return resultclass

    def teardown_databases(self, old_config, **kwargs):
        terminate_stray_sessions()
        super().teardown_databases(old_config, **kwargs)
