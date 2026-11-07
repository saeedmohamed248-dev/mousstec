"""
🌐 زرار تبديل اللغة على الموبايل.

🐛 كان مستخبي على الموبايل (`hidden sm:flex`) ومفيش غير نسخة في آخر قائمة ☰ تحت ~٣٠ رابط —
صاحب شركة فاتح الإنجليزي من الموبايل مالقاش أي طريقة يرجع للعربي.
دلوقتي في الشريط اللي فوق زرار باللغة التانية ظاهر على كل المقاسات.
"""
import re

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from .base import ERPTenantTestCase


@override_settings(ALLOWED_HOSTS=['*'])
class MobileLanguageSwitchTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.user = User.objects.create_user('lang_owner', password='x', is_superuser=True, is_staff=True)
        self.c = Client(HTTP_HOST=self.domain.domain)
        self.c.force_login(self.user)

    def _header_form(self, lang):
        self.c.cookies['django_language'] = lang
        r = self.c.get('/system/parties/import/', HTTP_ACCEPT_LANGUAGE=lang)   # صفحة بسيطة بنفس القالب الأساسي
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        m = re.search(r'<form[^>]*data-lang-switch[^>]*>(.*?)</form>', html, re.S)
        self.assertIsNotNone(m, 'زرار اللغة مش موجود في الشريط اللي فوق')
        self.assertNotIn('hidden sm:flex', html[m.start():m.start() + 200])
        return m.group(1)

    def _btn_classes(self, form, value):
        return re.search(rf'value="{value}"[^>]*class="([^"]*)"', form).group(1).split()

    def test_english_page_shows_arabic_button_on_mobile(self):
        form = self._header_form('en')
        self.assertNotIn('hidden', self._btn_classes(form, 'ar'))   # «ع» ظاهر على الموبايل
        self.assertIn('hidden', self._btn_classes(form, 'en'))      # اللغة الحالية بس على الكبير

    def test_arabic_page_shows_english_button_on_mobile(self):
        form = self._header_form('ar')
        self.assertNotIn('hidden', self._btn_classes(form, 'en'))
        self.assertIn('hidden', self._btn_classes(form, 'ar'))
