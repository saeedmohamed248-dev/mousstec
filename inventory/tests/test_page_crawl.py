"""🕷️ زحف على كل صفحات الشركة اللي مالهاش باراميترات، كمالك، لشركة سيارات ولمطبعة.

أي صفحة بترجع 500 (أو بترمي استثناء) بتوقّع الاختبار وبتتكتب في الرسالة — عشان صفحة
بتقع ماتوصلش للعملاء. أول تشغيل (نوفمبر ٢٠٢٦): ٢٨٥ صفحة سيارات و١٣٢ مطابع من غير ولا 500.
CRAWL_OUT=<مجلد> بيكتب تقرير بكل الأكواد.
"""
import os

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings
from django.urls import URLPattern, URLResolver, get_resolver

from .base import ERPTenantTestCase

SKIP = ('logout', 'export', 'download', 'delete', 'auto-login', 'impersonate', 'webhook', 'callback',
        'stream', 'sse', 'healthz', 'internal/', '/api/', 'pdf', 'xlsx', 'csv', 'print', 'sw.js', 'manifest')


def _walk(patterns, prefix=''):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            route = prefix + str(p.pattern)
            if '<' in route or '(?P' in route or '\\' in route:
                continue
            yield '/' + route.lstrip('^').rstrip('$').replace('^', '')


@override_settings(ALLOWED_HOSTS=['*'])
class PageCrawlTests(ERPTenantTestCase):
    def _crawl(self, industry):
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry=industry)
        u = User.objects.create_user(f'crawl_{industry}', password='x', is_superuser=True, is_staff=True)
        c = Client(HTTP_HOST=self.domain.domain, raise_request_exception=False)
        c.force_login(u)
        bad = []
        stats = {}
        urls = sorted(set(_walk(get_resolver().url_patterns)))
        for url in urls:
            if not url.startswith(('/system/', '/printing/', '/hr/', '/secure-portal/', '/repair-atlas/',
                                   '/robot/', '/omnichannel/', '/social', '/account/', '/workspace')):
                continue
            if any(s in url for s in SKIP):
                continue
            try:
                r = c.get(url)
                code = r.status_code
            except Exception as e:  # noqa: BLE001
                code = f'EXC {e.__class__.__name__}: {str(e)[:120]}'
            stats[code if isinstance(code, int) else 'exc'] = stats.get(code if isinstance(code, int) else 'exc', 0) + 1
            if code == 500 or isinstance(code, str):
                bad.append((url, code))
            connection.set_tenant(self.tenant)
        self.assertGreater(stats.get(200, 0), 50, f'الزحف ماوصلش لصفحات كفاية: {stats}')
        self.assertEqual(bad, [], f'صفحات بترجع خطأ سيرفر ({industry}): {bad}')
        out = os.environ.get('CRAWL_OUT')
        if out:
            with open(f'{out}/crawl_{industry}.txt', 'w') as f:
                f.write(f'checked {len(urls)} urls, visited {stats}\n')
                for b in bad:
                    f.write(f'{b}\n')

    def test_automotive(self):
        self._crawl('automotive')

    def test_printing(self):
        self._crawl('printing')
