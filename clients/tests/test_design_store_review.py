"""
🛍️ مراجعة متجر التصاميم (AI Designs Store).

* «سجّل شركتك الآن مجاناً» كان رايح على "/" (الصفحة الرئيسية) — العميل بيطلع
  برا خالص بدل صفحة تسجيل الشركات.
* شراء باقات المصممين (d20/d50/d100) كان بيتبعت لـ endpoint باقات العملاء
  فبيرد «الباقة المختارة غير متاحة» — مفيش مصمم كان يقدر يشتري.
* «دخول» كان بيودّي لاختيار القطاع، وبعد الدخول العميل بيتوه في اللوحة؛
  دلوقتي ‎?next=‎ بيرجّعه للمتجر (مسار داخلي بس).
* الصفحة كانت عربي ثابت — على موبايل إنجليزي كانت نص ونص («Enter»).
* طريقة الدفع كانت بتتقبل أي نص (حتى «منحة من الإدارة»)، والباقة المدفوعة
  كانت ممكن ترجع «في انتظار التأكيد» أو تترفض بعد ما اتدفعت.
"""
from __future__ import annotations

import json
import random
import re
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from clients.models import DesignPackage, DesignPurchase, ManualPaymentReceipt
from clients.tests.test_printing_customer_flow import (
    _client_for, _new_customer, _TenantDomainMixin,
)

STORE = '/marketplace/design-store/'


def _designer_pkg(slug='d20'):
    pkg, _ = DesignPackage.objects.update_or_create(slug=slug, defaults=dict(
        target_audience='designer', name_ar='باقة 20 تصميم — للمصممين',
        designs_count=20, designer_designs_count=20, price_egp=Decimal('149.00'),
        is_active=True, badge_text='الأفضل قيمة', quality_level='ultra'))
    return pkg


@override_settings(ALLOWED_HOSTS=['*'])
class DesignStorePageTests(_TenantDomainMixin, TestCase):

    def setUp(self):
        _designer_pkg()

    def test_register_company_button_goes_to_company_signup(self):
        html = self.client.get(STORE).content.decode()
        self.assertIn('href="/connect/signup/?industry=printing" class="erp-link"', html)
        self.assertNotIn('href="/" class="erp-link"', html)

    def test_guest_login_goes_to_printing_login_and_back_to_the_store(self):
        html = self.client.get(STORE).content.decode()
        self.assertIn('/marketplace/printing/?next=%2Fmarketplace%2Fdesign-store%2F', html)

    def test_designer_cards_use_the_designer_purchase_flow(self):
        html = _client_for(_new_customer()).get(STORE).content.decode()
        self.assertRegex(html, r'data-flow="legacy" data-slug="d20"')
        self.assertRegex(html, r'data-flow="topup" data-slug="cust_20"')

    def test_english_page_has_no_arabic_left(self):
        r = self.client.get(STORE, HTTP_ACCEPT_LANGUAGE='en')
        html = r.content.decode()
        self.assertIn('<html lang="en" dir="ltr">', html)
        self.assertIn('Sign in', html)
        self.assertIn('Register your company for free', html)
        self.assertIn('20-design pack — for designers', html)   # اسم الباقة من الـ DB
        self.assertIn('Best value', html)                        # الشارة
        self.assertIn('Ultra quality', html)
        self.assertNotIn('>Enter<', html)
        # كل اللي جوه <body> (من غير السكريبتات وويدجت المساعد) يبقى إنجليزي
        body = html.split('<body>', 1)[1].split("<div id=\"ai-chat", 1)[0]
        body = re.sub(r'<script.*?</script>', '', body, flags=re.S)
        body = re.sub(r'<!--.*?-->', '', body, flags=re.S)
        body = re.sub(r'\{#.*?#\}', '', body, flags=re.S)
        text = re.sub(r'<[^>]+>', ' ', body)
        arabic = [w for w in text.split() if re.search(r'[؀-ۿ]', w) and w != 'العربية']
        self.assertEqual(arabic, [])

    def test_price_per_design_is_not_rounded_to_whole_pounds(self):
        html = self.client.get(STORE).content.decode()
        self.assertIn('7.45', html)   # 149 / 20 — كانت بتظهر 7


@override_settings(ALLOWED_HOSTS=['*'])
class DesignStorePurchaseTests(_TenantDomainMixin, TestCase):

    def setUp(self):
        self.pkg = _designer_pkg()
        self.customer = _new_customer()
        self.c = _client_for(self.customer)

    def test_designer_package_purchase_with_vodafone_cash(self):
        r = self.c.post(f'{STORE}buy/d20/', {'payment_method': 'vodafone_cash'})
        self.assertEqual(r.status_code, 200, r.content)
        purchase = DesignPurchase.objects.get(customer=self.customer)
        self.assertEqual((purchase.package, purchase.status, purchase.price_paid),
                         (self.pkg, 'pending', Decimal('149.00')))
        receipt = ManualPaymentReceipt.objects.get(purchase_type='design', purchase_id=purchase.pk)
        self.assertEqual(r.json()['redirect'], f'/payment/manual/upload/{receipt.receipt_code}/')

    def test_customer_cannot_pick_an_admin_only_payment_method(self):
        for method in ('admin_grant', 'cash_collect', 'x' * 40):
            with self.subTest(method=method):
                r = self.c.post(f'{STORE}buy/d20/', {'payment_method': method})
                self.assertEqual(r.status_code, 400)
                r = self.c.post('/printing-copilot/api/topup/purchase/', json.dumps(
                    {'package_slug': 'cust_20', 'audience': 'customer', 'payment_method': method}),
                    content_type='application/json')
                self.assertEqual(r.status_code, 400)
        self.assertFalse(DesignPurchase.objects.filter(customer=self.customer).exists())

    def test_customer_topup_with_instapay(self):
        r = self.c.post('/printing-copilot/api/topup/purchase/', json.dumps(
            {'package_slug': 'cust_40', 'audience': 'customer', 'payment_method': 'instapay'}),
            content_type='application/json')
        self.assertTrue(r.json()['success'], r.content)
        self.assertTrue(r.json()['checkout_url'].startswith('/payment/manual/upload/'))
        purchase = DesignPurchase.objects.get(customer=self.customer)
        self.assertEqual((purchase.designs_total, purchase.price_paid, purchase.status),
                         (40, Decimal('120.00'), 'pending'))

    def test_paid_purchase_is_not_sent_back_to_awaiting_confirm(self):
        purchase = DesignPurchase.objects.create(
            customer=self.customer, package=self.pkg, designs_total=20,
            price_paid=Decimal('149.00'), payment_method='vodafone_cash', status='paid')
        r = self.c.post(f'{STORE}payment/{purchase.purchase_code}/', {'txn_ref': '123'})
        self.assertEqual(r.status_code, 409)
        purchase.refresh_from_db()
        self.assertEqual((purchase.status, purchase.payment_reference), ('paid', ''))

    def test_admin_cannot_reject_a_paid_purchase_and_confirm_is_idempotent(self):
        purchase = DesignPurchase.objects.create(
            customer=self.customer, package=self.pkg, designs_total=20,
            price_paid=Decimal('149.00'), payment_method='vodafone_cash', status='awaiting_confirm')
        admin = Client()
        admin.force_login(get_user_model().objects.create_superuser('store_boss', 'b@x.com', 'x'))
        url = f'{STORE}confirm-payment/{purchase.pk}/'
        with mock.patch('clients.permissions.is_platform_owner', return_value=True):
            self.assertEqual(admin.post(url, {'action': 'confirm'}).status_code, 200)
            purchase.refresh_from_db()
            paid_at = purchase.paid_at
            self.assertEqual(purchase.status, 'paid')

            self.assertEqual(admin.post(url, {'action': 'confirm'}).status_code, 200)
            purchase.refresh_from_db()
            self.assertEqual(purchase.paid_at, paid_at)

            self.assertEqual(admin.post(url, {'action': 'reject'}).status_code, 409)
            purchase.refresh_from_db()
            self.assertEqual(purchase.status, 'paid')


def _ip():
    # التسجيل/الدخول عليهم rate limit بالـ IP في الكاش — كل طلب بـ IP لوحده
    return '10.77.%d.%d' % (random.randint(0, 255), random.randint(1, 254))


@override_settings(ALLOWED_HOSTS=['*'])
class MarketplaceNextRedirectTests(_TenantDomainMixin, TestCase):
    PASSWORD = 'Str0ng!Pass#2026'
    PHONE = '01055501234'

    def _post(self, url, data, client=None):
        return (client or Client()).post(url, json.dumps(data), content_type='application/json',
                                         REMOTE_ADDR=_ip())

    def _register(self, nxt):
        return self._post('/marketplace/register/', {
            'customer_type': 'individual', 'full_name': 'Next Tester', 'phone': self.PHONE,
            'password': self.PASSWORD, 'sector': 'printing', 'next': nxt})

    def _login(self, nxt):
        return self._post('/marketplace/login/', {'phone': self.PHONE, 'password': self.PASSWORD, 'next': nxt})

    def test_register_and_login_return_to_the_store(self):
        r = self._register(STORE)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['redirect'], STORE)
        r = self._login(STORE)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['redirect'], STORE)

    def test_outside_next_is_ignored(self):
        r = self._register('https://evil.example/')
        self.assertEqual(r.json()['redirect'], '/marketplace/dashboard/')
        for nxt in ('https://evil.example/', '//evil.example/x', '/\\evil.example', 'javascript:alert(1)', 42):
            with self.subTest(nxt=nxt):
                r = self._login(nxt)
                self.assertEqual(r.status_code, 200, r.content)
                self.assertEqual(r.json()['redirect'], '/marketplace/dashboard/')

    def test_logged_in_customer_is_sent_to_next(self):
        c = _client_for(_new_customer())
        r = c.get('/marketplace/printing/?next=' + STORE)
        self.assertRedirects(r, STORE, fetch_redirect_response=False)
        r = c.get('/marketplace/?next=https://evil.example/')
        self.assertRedirects(r, '/marketplace/dashboard/', fetch_redirect_response=False)

    def test_sector_chooser_keeps_next(self):
        html = self.client.get('/marketplace/?next=' + STORE).content.decode()
        self.assertIn('/marketplace/printing/?next=/marketplace/design-store/', html)


@override_settings(ALLOWED_HOSTS=['*'])
class ManualPaymentPageTests(_TenantDomainMixin, TestCase):
    """صفحة رفع الإيصال اللي المتجر بيودّي عليها: كانت عربي ثابت، «بفودافون كاش»
    حتى لو العميل اختار InstaPay، و«الرئيسية» بترميه على "/"."""

    def _receipt(self, method):
        customer = _new_customer()
        purchase = DesignPurchase.objects.create(
            customer=customer, package=_designer_pkg(), designs_total=20,
            price_paid=Decimal('149.00'), payment_method=method, status='pending')
        return ManualPaymentReceipt.objects.create(
            purchase_type='design', purchase_id=purchase.pk, amount=purchase.price_paid,
            payment_method=method, customer=customer, sender_phone='', txn_reference='')

    def test_instapay_receipt_in_english(self):
        receipt = self._receipt('instapay')
        html = self.client.get(f'/payment/manual/upload/{receipt.receipt_code}/',
                               HTTP_ACCEPT_LANGUAGE='en').content.decode()
        self.assertIn('Complete your payment with InstaPay', html)
        self.assertIn('href="/marketplace/design-store/"', html)
        self.assertIn('<details class="bg-slate-900/50 rounded-xl border border-slate-700/40" open>', html)
        body = html.split('<body', 1)[1].split('<div id="ai-chat', 1)[0]
        body = re.sub(r'<script.*?</script>|<!--.*?-->', '', body, flags=re.S)
        arabic = [w for w in re.sub(r'<[^>]+>', ' ', body).split()
                  if re.search(r'[؀-ۿ]', w) and w != 'العربية']
        self.assertEqual(arabic, [])

    def test_vodafone_receipt_in_arabic(self):
        receipt = self._receipt('vodafone_cash')
        html = self.client.get(f'/payment/manual/upload/{receipt.receipt_code}/').content.decode()
        self.assertIn('إتمام الدفع بفودافون كاش', html)
        self.assertIn('01094850763', html)
