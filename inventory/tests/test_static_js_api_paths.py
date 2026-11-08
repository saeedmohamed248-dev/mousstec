"""🔗 كل مسار API بيناديه dynamic_invoice.js (فاتورة الأدمن) لازم يكون موجود فعلاً.

🐛 مسح الباركود وفلترة عربيات العميل كانوا بينادوا /api/v1/… من غير /system/ ⇒ 404
من غير أي رسالة — الكاشير يمسح والصنف مايتحطّش، والعربيات ماتتفلترش.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import Resolver404, resolve

JS = Path(settings.BASE_DIR) / 'inventory' / 'static' / 'dynamic_invoice.js'


class DynamicInvoiceApiPathsTests(SimpleTestCase):
    def test_every_fetch_path_resolves(self):
        raw = re.findall(r"fetch\(`(/[^`]+)`", JS.read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(raw), 3)
        for path in raw:
            path = re.sub(r'\$\{[^}]+\}', '1', path).split('?')[0]
            with self.subTest(path=path):
                try:
                    resolve(path)
                except Resolver404:
                    self.fail(f'{path} مش موجود في الـ urls')
