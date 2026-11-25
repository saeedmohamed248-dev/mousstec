"""
📈 بورصة أسعار المطابع:
* البنود المبدئية موجودة (منتجات + خامات) بمواصفة ثابتة.
* البائع بيسعّر ⇒ سعر واحد لكل بند في اليوم (التعديل في نفس اليوم مابيكرّرش)، وأرقام غلط بتترفض.
* «ادّي تاخد»: متوسط السوق مقفول لحد ما المطبعة تسعّر النهارده.
* «أسعاري زي امبارح» بتأكّد آخر سعر بتاريخ النهارده — من اللوحة ومن لينك الواتساب (POST بس).
* المؤشر العام: نطاق من غير أسماء، وبس من ٣ مطابع؛ الأسعار القديمة والشاذة مابتدخلش.
* الشركة على السيستم (ERP) بتسعّر كمان، والتذكير اليومي بيروح للي ماسعّرش بس.
"""
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client as HttpClient, TestCase, override_settings

from clients.models import CatalogStore, Client, MarketSeller, PriceQuote, PriceSpec
from clients.services import market_seller as seller_svc
from clients.services import price_board as pb
from clients.tests.test_printing_customer_flow import _TenantDomainMixin


def _seller(name='مطبعة', city='القاهرة'):
    store = CatalogStore.objects.create(sector='printing', name=name, slug=f's-{uuid.uuid4().hex[:8]}',
                                        city=city, is_active=True)
    s = MarketSeller(store=store, owner_name='x', phone=f'+2010{uuid.uuid4().int % 100000000:08d}',
                     email='a@example.com')
    s.set_password('secret123')
    s.save()
    return s


def _login(seller):
    c = HttpClient()
    c.cookies[seller_svc.COOKIE] = str(seller.session_token)
    return c


@override_settings(ALLOWED_HOSTS=['*'])
class PriceBoardTests(_TenantDomainMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.cards = PriceSpec.objects.get(code='cards-1000')
        self.banner = PriceSpec.objects.get(code='banner-flex-m2')

    def test_seeded_specs(self):
        self.assertGreaterEqual(PriceSpec.objects.filter(kind='product').count(), 10)
        self.assertGreaterEqual(PriceSpec.objects.filter(kind='material').count(), 4)
        self.assertIn('٣٥٠', self.cards.spec)

    def test_seller_submits_and_market_unlocks(self):
        s = _seller()
        c = _login(s)
        page = c.get('/marketplace/seller/prices/').content.decode()
        self.assertIn('كروت شخصية', page)
        self.assertIn('سعّر النهارده وشوف', page)           # المتوسط مقفول
        self.assertIn('value="cairo" selected', page)        # المحافظة متخمّنة من مدينة المتجر
        r = c.post('/marketplace/seller/prices/', {'governorate': 'cairo', f'p_{self.cards.pk}': '٣٥٠',
                                                    f'p_{self.banner.pk}': '120.5'})
        self.assertEqual(r['Location'], '/marketplace/seller/prices/?saved=2')
        q = PriceQuote.objects.get(seller=s, spec=self.cards)
        self.assertEqual((q.price, q.governorate, q.quoted_on), (Decimal('350.00'), 'cairo', pb.today()))
        # تعديل في نفس اليوم ⇒ نفس الصف
        c.post('/marketplace/seller/prices/', {'governorate': 'cairo', f'p_{self.cards.pk}': '400'})
        self.assertEqual(PriceQuote.objects.filter(seller=s, spec=self.cards).count(), 1)
        self.assertEqual(PriceQuote.objects.get(seller=s, spec=self.cards).price, Decimal('400.00'))
        page = c.get('/marketplace/seller/prices/').content.decode()
        self.assertNotIn('سعّر النهارده وشوف', page)
        self.assertIn('متوسط محافظتك', page)

    def test_bad_input_rejected(self):
        c = _login(_seller())
        r = c.post('/marketplace/seller/prices/', {'governorate': 'cairo', f'p_{self.cards.pk}': 'abc'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('رقم مش مظبوط', r.content.decode())
        r = c.post('/marketplace/seller/prices/', {'governorate': 'mars', f'p_{self.cards.pk}': '300'})
        self.assertIn('اختار المحافظة', r.content.decode())
        r = c.post('/marketplace/seller/prices/', {'governorate': 'cairo', f'p_{self.cards.pk}': '-5'})
        self.assertIn('رقم مش مظبوط', r.content.decode())
        self.assertFalse(PriceQuote.objects.exists())

    def test_confirm_same_as_yesterday(self):
        s = _seller()
        PriceQuote.objects.create(spec=self.cards, seller=s, governorate='giza', price=330,
                                  quoted_on=pb.today() - timedelta(days=1))
        c = _login(s)
        self.assertIn('أسعاري زي امبارح', c.get('/marketplace/seller/prices/').content.decode())
        c.post('/marketplace/seller/prices/', {'action': 'confirm'})
        q = PriceQuote.objects.get(seller=s, spec=self.cards, quoted_on=pb.today())
        self.assertEqual((q.price, q.governorate), (Decimal('330.00'), 'giza'))

    def test_whatsapp_link_confirms_only_on_post(self):
        s = _seller()
        PriceQuote.objects.create(spec=self.cards, seller=s, governorate='cairo', price=300,
                                  quoted_on=pb.today() - timedelta(days=1))
        url = pb.reminder_link(pb.Provider('seller', s)).split('mousstec.com')[-1].replace('https://', '')
        url = url[url.index('/marketplace/'):]
        c = HttpClient()
        self.assertEqual(c.get(url).status_code, 200)       # معاينة اللينك مابتأكّدش
        self.assertFalse(PriceQuote.objects.filter(quoted_on=pb.today()).exists())
        r = c.post(url)
        self.assertIn('اتأكّدت أسعارك', r.content.decode())
        self.assertTrue(PriceQuote.objects.filter(seller=s, quoted_on=pb.today()).exists())
        self.assertEqual(c.get('/marketplace/prices/confirm/garbage/').status_code, 410)

    def test_public_index_needs_three_shops_and_ignores_stale_and_outliers(self):
        page = HttpClient().get('/marketplace/prices/').content.decode()
        self.assertIn('لسه بنجمّع أسعار', page)
        sellers = [_seller(f'م{i}') for i in range(5)]
        for s, price in zip(sellers[:2], (300, 400)):
            PriceQuote.objects.create(spec=self.cards, seller=s, governorate='cairo', price=price, quoted_on=pb.today())
        self.assertIsNone(dict(pb.public_board())[self.cards])   # مطبعتين بس ⇒ مفيش نطاق
        PriceQuote.objects.create(spec=self.cards, seller=sellers[2], governorate='cairo', price=500,
                                  quoted_on=pb.today())
        # قديم (أقدم من FRESH_DAYS) ⇒ مايتحسبش
        PriceQuote.objects.create(spec=self.cards, seller=sellers[3], governorate='cairo', price=100,
                                  quoted_on=pb.today() - timedelta(days=pb.FRESH_DAYS))
        st = dict(pb.public_board())[self.cards]
        self.assertEqual((st['n'], st['min'], st['max'], st['avg']), (3, 300, 500, 400))
        # شاذ (٢٠ ضعف) ⇒ بيتعلّم ومايدخلش
        pb.save_prices(pb.Provider('seller', sellers[4]), 'cairo', {self.cards.pk: Decimal('9000')})
        self.assertTrue(PriceQuote.objects.get(seller=sellers[4]).is_outlier)
        self.assertEqual(dict(pb.public_board())[self.cards]['max'], 500)
        page = HttpClient().get('/marketplace/prices/?gov=cairo').content.decode()
        self.assertIn('300 – 500', page)
        self.assertNotIn(sellers[0].store.name, page)              # من غير أسماء
        self.assertIn('القاهرة', page)

    def test_erp_printing_tenant_can_submit(self):
        tenant = Client.objects.get(schema_name='public')
        provider = pb.Provider('merchant', tenant)
        pb.save_prices(provider, 'alex', {self.banner.pk: Decimal('110')})
        q = PriceQuote.objects.get(merchant=tenant)
        self.assertEqual((q.governorate, q.seller_id), ('alex', None))
        self.assertTrue(pb.submitted_today(provider))
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            PriceQuote.objects.create(spec=self.cards, governorate='cairo', price=1, quoted_on=pb.today())

    def test_merchant_page_requires_login(self):
        self.assertEqual(HttpClient().get('/marketplace/merchant/prices/').status_code, 302)
        c = HttpClient()
        c.force_login(User.objects.create(username='pb_owner', is_staff=True, is_superuser=True))
        # على الـ public schema مفيش شركة مطبعة ⇒ رجوع
        self.assertEqual(c.get('/marketplace/merchant/prices/').status_code, 302)

    @override_settings(WHATSAPP_ACCESS_TOKEN='t', WHATSAPP_PHONE_NUMBER_ID='p')
    def test_daily_reminder_skips_who_already_priced(self):
        done, pending = _seller('خلص'), _seller('لسه')
        pb.save_prices(pb.Provider('seller', done), 'cairo', {self.cards.pk: Decimal('300')})
        with mock.patch('clients.services.whatsapp_notify.send_platform_whatsapp', return_value=True) as wa, \
                mock.patch.object(pb, '_providers_to_remind',
                                  return_value=[pb.Provider('seller', done), pb.Provider('seller', pending)]):
            self.assertEqual(pb.send_daily_price_reminders(), 1)
        self.assertEqual(wa.call_args.args[0], pending.phone)
        self.assertIn('/marketplace/prices/confirm/', wa.call_args.kwargs['text'])

    def test_automotive_seller_redirected(self):
        s = _seller()
        CatalogStore.objects.filter(pk=s.store_id).update(sector='automotive')
        self.assertEqual(_login(s).get('/marketplace/seller/prices/')['Location'], '/marketplace/seller/')


    def test_upload_price_list_prefills_suggestions(self):
        """🤖 ملف أسعار ⇒ الأسعار المقترحة بتتملى (مش بتتحفظ) — سعر القطعة بيتحوّل لسعر الـ ١٠٠٠."""
        s = _seller()
        c = _login(s)
        csv_bytes = ('اسم البند,وحدة التسعير,السعر / من\nكارت شخصي وجهين,قطعة,2.2\n'
                     'بانر فليكس خارجي,متر,65\nحاجة تانية,قطعة,5\n').encode('utf-8')
        from django.core.files.uploadedfile import SimpleUploadedFile
        r = c.post('/marketplace/seller/prices/', {'action': 'import',
                                                   'price_file': SimpleUploadedFile('prices.csv', csv_bytes)})
        self.assertEqual(r['Location'], '/marketplace/seller/prices/?suggested=1')
        self.assertFalse(PriceQuote.objects.exists())          # لسه ماتحفظش
        html = c.get(r['Location']).content.decode()
        self.assertIn('المساعد ملا 2 سعر', html)
        self.assertIn(f'name="p_{self.cards.pk}" value="2200"', html)
        self.assertIn(f'name="p_{self.banner.pk}" value="65"', html)
        r = c.post('/marketplace/seller/prices/', {'action': 'import',
                                                   'price_file': SimpleUploadedFile('x.csv', b'a,b\n1,2\n')})
        self.assertEqual(r.status_code, 200)


class GovernorateGuessTests(TestCase):
    def test_guess(self):
        self.assertEqual(pb.guess_governorate('القاهرة - مدينة نصر'), 'cairo')
        self.assertEqual(pb.guess_governorate('الاسكندرية'), 'alex')
        self.assertEqual(pb.guess_governorate('Giza'), 'giza')
        self.assertEqual(pb.guess_governorate(''), '')
