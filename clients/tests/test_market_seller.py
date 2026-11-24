"""
🏪 بائع السوق — مطبعة/محل بيعرض منتجاته ويتابع عملاءه من غير برنامج الإدارة:
* صفحة «اعرض منتجاتك» بتودّي على تسجيل البائع (مش تسجيل شركة ERP)، ومفيهاش وعد «فواتير، مخزون، حسابات».
* التسجيل بيعمل متجر ظاهر على طول وحساب بائع — ومن غير أي شركة/schema.
* المنتج اللي البائع يضيفه مخفي لحد اعتماد Mouss Tec، والمتجر مخفي لحد الموافقة.
* طلب السعر على منتجه بيظهر في «عملائي» عنده بس، وتغيير الحالة لـ«اتنفّذ» بيطلّع لينك تقييم.
* السوق المفتوح: البائع يقدّم عرض، والعميل يقبله ⇒ الصفقة والتقييم بيتحسبوا للبائع.
* شيت Excel/CSV، والحماية: كلمة سر غلط، حساب موقوف، بائع مايلمسش منتجات غيره.
"""
import re
import uuid
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client as HttpClient, TestCase, override_settings
from django.utils import timezone

from clients.models import (
    CatalogItem,
    CatalogQuoteRequest,
    CatalogStore,
    Client,
    MarketplaceCustomer,
    MarketSeller,
    ServiceRequest,
    TenderOffer,
)
from clients.services import market_seller as svc
from clients.tests.test_printing_customer_flow import _TenantDomainMixin


def _phone():
    return f'010{uuid.uuid4().int % 100000000:08d}'


@override_settings(ALLOWED_HOSTS=['*'], CATALOG_NOTIFY_SYNC=True)
class MarketSellerTests(_TenantDomainMixin, TestCase):
    def setUp(self):
        cache.clear()

    def _join(self, c=None, **over):
        c = c or HttpClient()
        data = {'store_name': 'مطبعة النيل', 'owner_name': 'أحمد', 'phone': _phone(), 'password': 'secret123',
                'sector': 'printing', 'city': 'القاهرة', 'email': f'shop{uuid.uuid4().hex[:6]}@example.com', 'tagline': ''}
        data.update(over)
        with mock.patch('clients.services.owner_notify.notify_owner') as owner:
            r = c.post('/marketplace/seller/join/', data)
        return c, r, data, owner

    def _approve(self, seller):
        CatalogStore.objects.filter(pk=seller.store_id).update(is_active=True)

    # ── صفحة الجذب ──
    def test_sell_page_points_to_seller_signup_not_erp(self):
        html = HttpClient().get('/marketplace/sell/').content.decode()
        self.assertIn('/marketplace/seller/join/', html)
        self.assertNotIn('برنامج إدارة كامل (فواتير، مخزون، حسابات)', html)
        self.assertIn('من غير فواتير ولا مخزون ولا حسابات', html)
        en = HttpClient(HTTP_ACCEPT_LANGUAGE='en')
        en.cookies['django_language'] = 'en'
        en_html = en.get('/marketplace/sell/').content.decode()
        self.assertIn('Register your store now', en_html)
        self.assertNotIn('with a full management system', en_html)
        self.assertNotIn('href="/connect/signup/" style="background', html)   # الزرار الأساسي مش تسجيل شركة

    # ── التسجيل ──
    def test_join_creates_store_under_review_without_tenant(self):
        tenants_before = Client.objects.count()
        c, r, data, owner = self._join()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], '/marketplace/seller/?welcome=1')
        seller = MarketSeller.objects.get(phone=svc.normalize_phone(data['phone']))
        self.assertTrue(seller.store.is_active)   # ظاهر على طول من غير موافقة
        self.assertEqual(seller.store.sector, 'printing')
        self.assertTrue(seller.check_password('secret123'))
        self.assertEqual(Client.objects.count(), tenants_before)   # مفيش شركة ولا schema
        owner.assert_called_once()
        # 🐛 [FIX]: لينك إشعار صاحب المنصة كان /admin/… والأدمن على ADMIN_URL (secure-portal) ⇒ 404
        from django.conf import settings as dj
        link = owner.call_args.args[2]
        self.assertTrue(link.startswith(f'/{dj.ADMIN_URL}/clients/marketseller/'), link)
        from django.contrib.auth.models import User
        boss = HttpClient()
        boss.force_login(User.objects.create(username='seller_boss', is_staff=True, is_superuser=True))
        self.assertEqual(boss.get(link).status_code, 200)
        # 🐛 [FIX]: البائع الجديد ماكانش بيظهر في صفحة السوبر أدمن
        sa = boss.get('/superadmin/').content.decode()
        self.assertIn('متاجر بائعين جديدة في آخر ٧ أيام', sa)
        self.assertIn(f'/{dj.ADMIN_URL}/clients/marketseller/', sa)
        dash = c.get('/marketplace/seller/?welcome=1').content.decode()
        self.assertIn('متجرك ظاهر للعملاء', dash)
        self.assertNotIn('/system/', dash)
        # 🐛 [FIX]: اسم عربي كان بيعمل slug عربي ⇒ صفحة المتجر 404
        self.assertRegex(seller.store.slug, r'^[-a-z0-9]+$')
        self.assertEqual(HttpClient().get(f'/marketplace/catalog/store/{seller.store.slug}/').status_code, 200)
        # صاحب المنصة يخفي المتجر ⇒ يختفي للعملاء والبائع بيتقاله
        CatalogStore.objects.filter(pk=seller.store_id).update(is_active=False)
        self.assertEqual(HttpClient().get(f'/marketplace/catalog/store/{seller.store.slug}/').status_code, 404)
        self.assertIn('متجرك متخفي عن العملاء', c.get('/marketplace/seller/').content.decode())

    def test_join_rejects_bad_and_duplicate_phone(self):
        _c, r, _d, _o = self._join(phone='123')
        self.assertEqual(r.status_code, 200)
        self.assertIn('رقم الموبايل مش مظبوط', r.content.decode())
        phone = _phone()
        self._join(phone=phone)
        _c, r, _d, _o = self._join(phone='+2' + phone, store_name='مطبعة تانية')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(MarketSeller.objects.filter(phone=svc.normalize_phone(phone)).count(), 1)

    def test_login_logout_and_bad_password(self):
        _c, _r, data, _o = self._join()
        c = HttpClient()
        self.assertEqual(c.get('/marketplace/seller/').status_code, 302)
        r = c.post('/marketplace/seller/login/', {'phone': data['phone'], 'password': 'wrong'})
        self.assertIn('غلط', r.content.decode())
        r = c.post('/marketplace/seller/login/', {'phone': data['phone'], 'password': 'secret123',
                                                   'next': '//evil.com'})
        self.assertEqual(r['Location'], '/marketplace/seller/')
        self.assertEqual(c.get('/marketplace/seller/').status_code, 200)
        c.post('/marketplace/seller/logout/')
        self.assertEqual(c.get('/marketplace/seller/').status_code, 302)
        MarketSeller.objects.update(is_blocked=True)
        r = c.post('/marketplace/seller/login/', {'phone': data['phone'], 'password': 'secret123'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('موقوف', r.content.decode())

    # ── المنتجات ──
    def test_product_live_immediately_and_admin_hide_sticks(self):
        c, _r, data, _o = self._join()
        seller = MarketSeller.objects.get()
        self._approve(seller)
        with mock.patch('clients.services.owner_notify.notify_owner'):
            r = c.post('/marketplace/seller/products/new/', {
                'title': 'كروت شخصية مط', 'description': 'وش وضهر', 'category': 'cards',
                'min_qty': '١٠٠ كارت', 'price_from': '250'})
        self.assertEqual(r.status_code, 302, r.content.decode()[:500])
        item = CatalogItem.objects.get(store=seller.store)
        self.assertEqual((item.is_active, item.pending_review), (True, False))   # ظاهر على طول
        html = HttpClient().get(f'/marketplace/catalog/item/{item.item_code}/').content.decode()
        self.assertIn('كروت شخصية مط', html)
        self.assertIn('متجر مستقل', html)
        self.assertNotIn('بيتنفّذ عن طريق Mouss Tec', html)
        # تعديل ⇒ يفضل ظاهر
        with mock.patch('clients.services.owner_notify.notify_owner'):
            c.post(f'/marketplace/seller/products/{item.item_code}/', {
                'title': 'كروت شخصية لامع', 'category': 'cards', 'min_qty': '', 'price_from': ''})
        item.refresh_from_db()
        self.assertEqual((item.title, item.is_active), ('كروت شخصية لامع', True))
        # البائع يخفي ويرجّع
        c.post(f'/marketplace/seller/products/{item.item_code}/visibility/', {'action': 'hide'})
        item.refresh_from_db()
        self.assertFalse(item.is_active)
        c.post(f'/marketplace/seller/products/{item.item_code}/visibility/', {'action': 'show'})
        item.refresh_from_db()
        self.assertTrue(item.is_active)
        # 🙈 صاحب المنصة يخفيه من الأدمن ⇒ البائع مايقدرش يرجّعه (لا بزرار ولا بتعديل)
        from clients.admin_catalog import CatalogItemAdmin
        from django.contrib.admin.sites import site
        CatalogItemAdmin(CatalogItem, site).reject_items(mock.Mock(), CatalogItem.objects.filter(pk=item.pk))
        c.post(f'/marketplace/seller/products/{item.item_code}/visibility/', {'action': 'show'})
        with mock.patch('clients.services.owner_notify.notify_owner'):
            c.post(f'/marketplace/seller/products/{item.item_code}/', {
                'title': 'تاني', 'category': 'cards', 'min_qty': '', 'price_from': ''})
        item.refresh_from_db()
        self.assertEqual((item.is_active, item.admin_hidden), (False, True))
        self.assertIn('مخفي من Mouss Tec', c.get('/marketplace/seller/products/').content.decode())

    def test_other_category_needs_type_and_details(self):
        c, *_ = self._join()
        seller = MarketSeller.objects.get()
        self._approve(seller)
        form = c.get('/marketplace/seller/products/new/').content.decode()
        self.assertIn('value="other"', form)
        with mock.patch('clients.services.owner_notify.notify_owner'):
            r = c.post('/marketplace/seller/products/new/', {'title': 'ستاند رول أب', 'category': 'other',
                                                             'custom_category': '', 'description': 'قصير'})
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('اكتب نوع المنتج', html)
        self.assertIn('تفاصيل أكتر', html)
        self.assertFalse(CatalogItem.objects.filter(store=seller.store).exists())
        with mock.patch('clients.services.owner_notify.notify_owner'):
            r = c.post('/marketplace/seller/products/new/', {
                'title': 'ستاند رول أب', 'category': 'other', 'custom_category': 'ستاندات رول أب',
                'description': 'ستاند ألومنيوم ٨٥×٢٠٠ سم مع شنطة، طباعة فينيل عالي الجودة'})
        self.assertEqual(r.status_code, 302)
        item = CatalogItem.objects.get(store=seller.store)
        self.assertEqual((item.category, item.custom_category), ('other', 'ستاندات رول أب'))
        page = HttpClient().get(f'/marketplace/catalog/item/{item.item_code}/').content.decode()
        self.assertIn('ستاندات رول أب', page)
        self.assertIn('ستاندات رول أب', c.get('/marketplace/seller/products/').content.decode())
        # الرجوع لقسم عادي بيمسح النوع
        with mock.patch('clients.services.owner_notify.notify_owner'):
            c.post(f'/marketplace/seller/products/{item.item_code}/', {
                'title': 'بنر', 'category': 'banners', 'custom_category': 'كلام قديم', 'description': ''})
        item.refresh_from_db()
        self.assertEqual((item.category, item.custom_category), ('banners', ''))

    def test_seller_cannot_touch_other_store(self):
        c1, *_ = self._join()
        c2, *_ = self._join(store_name='محل تاني')
        s1 = MarketSeller.objects.get(store__name='مطبعة النيل')
        item = CatalogItem.objects.create(store=s1.store, title='منتج', category='cards', is_active=True)
        self.assertEqual(c2.get(f'/marketplace/seller/products/{item.item_code}/').status_code, 404)
        self.assertEqual(c2.post(f'/marketplace/seller/products/{item.item_code}/visibility/',
                                 {'action': 'hide'}).status_code, 404)
        item.refresh_from_db()
        self.assertTrue(item.is_active)

    def test_import_csv_sheet(self):
        c, *_ = self._join()
        sheet = SimpleUploadedFile('p.csv', 'اسم المنتج,القسم,أقل كمية,السعر\nكوباية مطبوعة,mugs,٥٠,40\n,,,\n'
                                            'قلم,أقسام مش موجودة,,abc\n'.encode('utf-8-sig'), 'text/csv')
        with mock.patch('clients.services.owner_notify.notify_owner'):
            r = c.post('/marketplace/seller/products/import/', {'sheet': sheet})
        self.assertEqual(r['Location'], '/marketplace/seller/products/?imported=2')
        items = {i.title: i for i in CatalogItem.objects.filter(store__seller__isnull=False)}
        # قسم مش من أقسامنا ⇒ «أخرى» باسمه زي ما اتكتب (مش أول قسم بالغلط)
        self.assertEqual((items['قلم'].category, items['قلم'].custom_category), ('other', 'أقسام مش موجودة'))
        self.assertEqual(items['كوباية مطبوعة'].category, 'mugs')
        self.assertEqual(str(items['كوباية مطبوعة'].price_from), '40.00')
        self.assertIsNone(items['قلم'].price_from)
        self.assertTrue(all(i.is_active and not i.pending_review for i in items.values()))
        bad = SimpleUploadedFile('p.pdf', b'%PDF', 'application/pdf')
        r = c.post('/marketplace/seller/products/import/', {'sheet': bad})
        self.assertIn('Excel', r.content.decode())

    # ── عملائي ──
    def test_quote_reaches_seller_customers(self):
        c, *_ = self._join()
        seller = MarketSeller.objects.get()
        self._approve(seller)
        item = CatalogItem.objects.create(store=seller.store, title='بنر فينيل', category='banners', is_active=True)
        other_store = CatalogStore.objects.create(sector='printing', name='متجر Mouss Tec', slug='mt-other')
        other = CatalogItem.objects.create(store=other_store, title='منتج تاني', category='cards', is_active=True)
        with mock.patch('clients.services.whatsapp_notify.send_platform_whatsapp', return_value=True) as wa, \
                self.settings(WHATSAPP_ACCESS_TOKEN='t', WHATSAPP_PHONE_NUMBER_ID='p'), \
                self.captureOnCommitCallbacks(execute=True):
            HttpClient().post(f'/marketplace/catalog/item/{item.item_code}/',
                              {'name': 'منى', 'phone': '01011112222', 'quantity': '٢'})
        self.assertTrue(any(call.args and call.args[0] == seller.phone for call in wa.call_args_list))
        CatalogQuoteRequest.objects.create(item=other, item_title=other.title, sector='printing',
                                           name='مش عميله', phone='01000000000')
        html = c.get('/marketplace/seller/customers/').content.decode()
        self.assertIn('منى', html)
        self.assertNotIn('مش عميله', html)
        req = CatalogQuoteRequest.objects.get(item=item)
        c.post(f'/marketplace/seller/customers/{req.request_code}/', {'status': 'done'})
        req.refresh_from_db()
        self.assertEqual(req.status, 'done')
        self.assertIsNotNone(req.review_token)
        foreign = CatalogQuoteRequest.objects.get(item=other)
        self.assertEqual(c.post(f'/marketplace/seller/customers/{foreign.request_code}/',
                                {'status': 'done'}).status_code, 404)

    # ── السوق المفتوح ──
    def test_open_market_offer_accept_and_rate(self):
        c, *_ = self._join()
        seller = MarketSeller.objects.get()
        self._approve(seller)
        cust = MarketplaceCustomer.objects.create(customer_type='individual', full_name='عميل', sector='printing',
                                                  is_verified=True, phone='+20' + _phone()[1:])
        req = ServiceRequest.objects.create(customer=cust, sector='printing', title='٥٠٠ علبة مطبوعة',
                                            description='تفاصيل', status='open', is_approved=True,
                                            expires_at=timezone.now() + timedelta(days=3))
        auto = ServiceRequest.objects.create(customer=cust, sector='automotive', title='قطعة غيار',
                                             description='x', status='open', is_approved=True,
                                             expires_at=timezone.now() + timedelta(days=3))
        html = c.get('/marketplace/seller/requests/').content.decode()
        self.assertIn('٥٠٠ علبة مطبوعة', html)
        self.assertNotIn('قطعة غيار', html)
        self.assertEqual(c.post(f'/marketplace/seller/requests/{auto.request_code}/offer/',
                                {'price': '10', 'description': 'x'}).status_code, 404)
        r = c.post(f'/marketplace/seller/requests/{req.request_code}/offer/',
                   {'price': '1500', 'description': 'علب كرتون مطبوعة ٤ لون', 'estimated_days': '5'})
        self.assertEqual(r['Location'], '/marketplace/seller/requests/?sent=1')
        r = c.post(f'/marketplace/seller/requests/{req.request_code}/offer/', {'price': '1400', 'description': 'x'})
        self.assertIn('error=dup', r['Location'])
        offer = TenderOffer.objects.get(service_request=req)
        self.assertIsNone(offer.merchant_id)
        self.assertEqual((offer.provider_name, offer.provider_phone), ('مطبعة النيل', seller.phone))
        req.refresh_from_db()
        self.assertEqual(req.offers_count, 1)

        # العميل بيشوف العرض ويقبله
        cc = HttpClient()
        cc.cookies['mp_session'] = str(cust.session_token)
        self.assertEqual(cc.get(f'/marketplace/request/{req.request_code}/').status_code, 200)
        with mock.patch('clients.services.whatsapp_notify.send_platform_whatsapp', return_value=True) as wa, \
                mock.patch('clients.tasks.notify_merchant_offer_accepted.delay', side_effect=Exception('no broker')), \
                self.settings(WHATSAPP_ACCESS_TOKEN='t', WHATSAPP_PHONE_NUMBER_ID='p'), \
                self.captureOnCommitCallbacks(execute=True):
            r = cc.post(f'/marketplace/offer/{offer.offer_code}/accept/')
        self.assertEqual(r.status_code, 200, r.content)
        # 🔔 البائع بيوصله واتساب إن عرضه اتقبل (مالوش لوحة شركة)
        self.assertTrue(any(call.args and call.args[0] == seller.phone for call in wa.call_args_list))
        self.assertEqual(r.json()['merchant_name'], 'مطبعة النيل')
        seller.refresh_from_db()
        self.assertEqual(seller.successful_deals, 1)
        html = c.get('/marketplace/seller/requests/').content.decode()
        self.assertIn(cust.phone, html)   # بيانات العميل بتظهر للبائع بعد القبول
        r = cc.post(f'/marketplace/offer/{offer.offer_code}/rate/', data='{"rating": 4, "review": "كويس"}',
                    content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        seller.refresh_from_db()
        self.assertEqual(str(seller.market_rating), '4.00')

    def test_offer_must_have_exactly_one_provider(self):
        from django.db import IntegrityError, transaction
        cust = MarketplaceCustomer.objects.create(customer_type='individual', full_name='ع', sector='printing',
                                                  phone='+20' + _phone()[1:])
        req = ServiceRequest.objects.create(customer=cust, sector='printing', title='t', description='d',
                                            expires_at=timezone.now() + timedelta(days=1))
        with self.assertRaises(IntegrityError), transaction.atomic():
            TenderOffer.objects.create(service_request=req, price=1, description='x', merchant_city='c')

    def test_upgrade_links_tenant_and_prefills_signup(self):
        c, *_ = self._join()
        seller = MarketSeller.objects.get()
        html = c.get('/connect/signup/?industry=printing').content.decode()
        self.assertIn('مطبعة النيل', html)
        tenant = Client.objects.get(schema_name='public')
        req = mock.Mock(COOKIES={svc.COOKIE: str(seller.session_token)})
        self.assertEqual(svc.link_upgrade(req, tenant), seller)
        seller.refresh_from_db()
        self.assertEqual(seller.upgraded_to, tenant)
        self.assertIn(tenant.name, c.get('/marketplace/seller/upgrade/').content.decode())


    # ── نسيت كلمة السر ──
    def test_join_requires_email(self):
        _c, r, _d, _o = self._join(email='')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(MarketSeller.objects.exists())

    def test_password_reset_by_email_code(self):
        from django.core import mail
        _c, _r, data, _o = self._join()
        seller = MarketSeller.objects.get()
        c = HttpClient()
        r = c.post('/marketplace/seller/password/forgot/', {'phone': data['phone']})
        self.assertIn('/marketplace/seller/password/reset/', r.content.decode())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [data['email']])
        code = re.search(r'\b(\d{6})\b', mail.outbox[0].body).group(1)
        # رقم مش متسجّل ⇒ نفس الرد ومفيش إيميل
        r2 = c.post('/marketplace/seller/password/forgot/', {'phone': _phone()})
        self.assertIn('/marketplace/seller/password/reset/', r2.content.decode())
        self.assertEqual(len(mail.outbox), 1)
        # كود غلط
        wrong = '000000' if code != '000000' else '111111'
        r = c.post('/marketplace/seller/password/reset/', {'phone': data['phone'], 'code': wrong, 'password': 'newpass99'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('الكود غلط', r.content.decode())
        old_token = seller.session_token
        r = c.post('/marketplace/seller/password/reset/', {'phone': data['phone'], 'code': code, 'password': 'newpass99'})
        self.assertEqual(r['Location'], '/marketplace/seller/?reset=1')
        seller.refresh_from_db()
        self.assertTrue(seller.check_password('newpass99'))
        self.assertNotEqual(seller.session_token, old_token)   # الأجهزة التانية خرجت
        self.assertEqual(c.get('/marketplace/seller/').status_code, 200)
        # الكود مايتستخدمش مرتين
        r = c.post('/marketplace/seller/password/reset/', {'phone': data['phone'], 'code': code, 'password': 'again999'})
        self.assertIn('الكود غلط', r.content.decode())

    def test_reset_code_dies_after_five_wrong_tries(self):
        from django.core import mail
        _c, _r, data, _o = self._join()
        c = HttpClient()
        c.post('/marketplace/seller/password/forgot/', {'phone': data['phone']})
        code = re.search(r'\b(\d{6})\b', mail.outbox[-1].body).group(1)
        wrong = '000000' if code != '000000' else '111111'
        for _ in range(5):
            c.post('/marketplace/seller/password/reset/', {'phone': data['phone'], 'code': wrong, 'password': 'newpass99'})
        r = c.post('/marketplace/seller/password/reset/', {'phone': data['phone'], 'code': code, 'password': 'newpass99'})
        self.assertIn('الكود غلط', r.content.decode())
        self.assertFalse(MarketSeller.objects.get().check_password('newpass99'))


class PhoneNormalizeTests(TestCase):
    def test_forms(self):
        for raw in ('01012345678', '+201012345678', '00201012345678', '٠١٠١٢٣٤٥٦٧٨', '1012345678'):
            self.assertEqual(svc.normalize_phone(raw), '+201012345678', raw)
        self.assertEqual(svc.normalize_phone('123'), '')
