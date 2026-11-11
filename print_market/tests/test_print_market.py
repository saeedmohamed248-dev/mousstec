"""
🖨️ سوق المطابع — اختبارات الدورة كاملة:
* حارس التواصل بيمنع موبايل/إيميل/لينك/واتساب (وبيعدّ المحاولات) ومابيمنعش الكلام العادي.
* تسجيل/دخول بالإيميل، ومطبعة مجانية تنزّل منتج يظهر في السوق.
* طلب ← عرض ← رفض ← عرض جديد ← قبول ← دفع (سعر + رسوم تحويل بس) ← تأكيد Mouss Tec ← بروفة فيديو
  ← تعديل ← بروفة ٢ ← موافقة ← تسليم ← استلام.
* الصلاحيات: عميل تاني مايشوفش الطلب ولا ملفاته، والمطبعة ماتشوفش إيصال الدفع.
* الفيديو بيتخدم بـ Range (للموبايل).
"""
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from clients.tests.test_printing_customer_flow import _TenantDomainMixin
from print_market.contact_guard import find_contact
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from print_market.models import (MarketAccount, MarketNotification, MarketOrder, MarketPayment, MarketProduct,
                                 MarketProof, MarketReview, transfer_fee)

PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15'
       b'\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')
PW = 'Strong-Pass-2026'


def _png(name='r.png'):
    return SimpleUploadedFile(name, PNG, content_type='image/png')


class ContactGuardTests(TestCase):
    def test_blocks_contacts(self):
        for text in ('كلمني 01012345678', 'رقمي ٠١٠ ١٢٣ ٤٥ ٦٧٨', '+20 112 345 6789', 'a.b@gmail.com',
                     'ابعت على wa.me/2010', 'شوف www.shop.com', 'كلمني واتس', 'تابعني @myprint_shop',
                     'حوّلي فودافون كاش', 'mysite.net', '0 1 0 1 2 3 4 5 6 7 8'):
            self.assertTrue(find_contact(text), text)

    def test_allows_normal_text(self):
        for text in ('١٠٠٠ كارت مقاس 9x5.5 ورق 350 جرام', 'محتاج 500 فلاير و 2000 ستيكر',
                     'التسليم يوم 15/12 الساعة 3', 'السعر 1500 جنيه', ''):
            self.assertEqual(find_contact(text), '', text)


@override_settings(ALLOWED_HOSTS=['*'])
class PrintMarketFlowTests(_TenantDomainMixin, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp()
        cls._media_override = override_settings(MEDIA_ROOT=cls._media)
        cls._media_override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._media_override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        cache.clear()
        self.shop_c, self.cust_c, self.other_c = Client(), Client(), Client()
        self._signup(self.shop_c, 'shop', 'مطبعة النور', 'shop@x.com', 'القاهرة')
        self._signup(self.cust_c, 'customer', 'أحمد', 'cust@x.com')
        self._signup(self.other_c, 'customer', 'غريب', 'other@x.com')
        self.shop = MarketAccount.objects.get(email='shop@x.com')
        self.cust = MarketAccount.objects.get(email='cust@x.com')

    def _signup(self, c, role, name, email, city=''):
        r = c.post(reverse('print_market:signup'), {'role': role, 'name': name, 'email': email,
                                                    'password': PW, 'city': city})
        self.assertEqual(r.status_code, 302, r.content[:500])

    def _u(self, name, order=None, *args):
        if order is None:
            return reverse(f'print_market:{name}', args=args)
        return reverse(f'print_market:{name}', args=[order.code, *args])

    # ── الحسابات ──
    def test_signup_login_logout(self):
        self.assertEqual(self.shop.role, 'shop')
        self.assertTrue(self.shop.check_password(PW))
        c = Client()
        r = c.post(reverse('print_market:login'), {'email': 'SHOP@x.com', 'password': 'wrong'})
        self.assertContains(r, 'غلط')
        r = c.post(reverse('print_market:login'), {'email': 'SHOP@x.com', 'password': PW})
        self.assertRedirects(r, reverse('print_market:shop_dashboard'), fetch_redirect_response=False)
        self.assertEqual(c.get(reverse('print_market:shop_dashboard')).status_code, 200)
        c.post(reverse('print_market:logout'))
        r = c.get(reverse('print_market:shop_dashboard'))
        self.assertEqual(r.status_code, 302)
        # الخروج بيطلّع من الأجهزة التانية كمان
        self.assertEqual(self.shop_c.get(reverse('print_market:shop_dashboard')).status_code, 302)

    def test_duplicate_email_and_shop_needs_city(self):
        r = Client().post(reverse('print_market:signup'), {'role': 'customer', 'name': 'x', 'email': 'cust@x.com',
                                                           'password': PW})
        self.assertEqual(r.status_code, 200)
        r = Client().post(reverse('print_market:signup'), {'role': 'shop', 'name': 'x', 'email': 'n@x.com',
                                                           'password': PW})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(MarketAccount.objects.filter(email='n@x.com').exists())

    def test_customer_cannot_open_shop_pages(self):
        r = self.cust_c.get(reverse('print_market:shop_dashboard'))
        self.assertEqual(r.status_code, 302)

    def test_password_reset(self):
        from django.core import mail
        self.client.post(reverse('print_market:password_forgot'), {'email': 'cust@x.com'})
        self.assertEqual(len(mail.outbox), 1)
        link = [w for w in mail.outbox[0].body.split() if '/password/reset/' in w][0]
        path = link.split('testserver', 1)[-1]
        r = self.client.post(path, {'password': 'Another-Pass-77'})
        self.assertEqual(r.status_code, 302)
        self.cust.refresh_from_db()
        self.assertTrue(self.cust.check_password('Another-Pass-77'))
        # اللينك بيبطل بعد الاستخدام
        r = self.client.post(path, {'password': 'Third-Pass-88'})
        self.assertRedirects(r, reverse('print_market:password_forgot'), fetch_redirect_response=False)

    # ── المنتجات ──
    def test_product_listing_and_contact_block(self):
        r = self.shop_c.post(reverse('print_market:product_new'), {
            'title': 'كروت شخصية', 'category': 'cards', 'price_from': '250', 'unit': '١٠٠٠ كارت',
            'description': 'ورق 350 جرام', 'is_active': 'on', 'image': _png('p.png')})
        self.assertEqual(r.status_code, 302)
        p = MarketProduct.objects.get(shop=self.shop)
        self.assertContains(Client().get(reverse('print_market:home') + '?q=كروت'), 'كروت شخصية')
        self.assertContains(Client().get(reverse('print_market:product', args=[p.pk])), 'اطلب عرض سعر')
        r = self.shop_c.post(reverse('print_market:product_edit', args=[p.pk]), {
            'title': 'كروت', 'category': 'cards', 'description': 'كلمني 01012345678', 'is_active': 'on'})
        self.assertEqual(r.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.description, 'ورق 350 جرام')
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.blocked_attempts, 1)

    # ── الدورة كاملة ──
    def _new_order(self):
        r = self.cust_c.post(reverse('print_market:order_new', args=[self.shop.pk]), {
            'title': 'فلايرز', 'details': '1000 فلاير A5 وشين', 'quantity': '1000'})
        self.assertEqual(r.status_code, 302)
        return MarketOrder.objects.get(customer=self.cust)

    def test_full_flow(self):
        order = self._new_order()
        self.assertEqual(order.status, 'requested')
        # عرض ← رفض ← عرض جديد ← قبول
        self.shop_c.post(self._u('offer_submit', order), {'price': '1000', 'delivery_days': '3'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'offered')
        self.cust_c.post(self._u('offer_respond', order, order.pending_offer.pk), {'action': 'reject'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'requested')
        self.shop_c.post(self._u('offer_submit', order), {'price': '900', 'delivery_days': '2', 'note': 'ورق كوشيه'})
        order.refresh_from_db()
        offer = order.pending_offer
        self.assertEqual(offer.price, Decimal('900.00'))
        self.cust_c.post(self._u('offer_respond', order, offer.pk), {'action': 'accept'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'awaiting_payment')
        page = self.cust_c.get(self._u('order_detail', order))
        self.assertRegex(page.content.decode(), r'900[.,]90')  # 900 + 0.1% = 0.9 رسوم إنستاباي

        # الدفع: سعر العرض + رسوم التحويل بس
        self.cust_c.post(self._u('payment_submit', order), {'method': 'instapay', 'reference': 'TX123',
                                                            'receipt': _png()})
        order.refresh_from_db()
        self.assertEqual(order.status, 'payment_review')
        pay = MarketPayment.objects.get(order=order)
        self.assertEqual((pay.amount, pay.fee, pay.total), (Decimal('900'), Decimal('0.90'), Decimal('900.90')))

        # المطبعة ماتشوفش الإيصال، والعميل يشوفه، وصاحب المنصة يشوفه
        self.assertEqual(self.shop_c.get(self._u('file', None, 'receipt', pay.pk)).status_code, 404)
        self.assertEqual(self.cust_c.get(self._u('file', None, 'receipt', pay.pk)).status_code, 200)
        admin = User.objects.create_superuser('owner', 'o@x.com', 'x')
        pay.review(admin, confirm=True)
        order.refresh_from_db()
        self.assertEqual(order.status, 'paid')
        with self.assertRaises(ValidationError):
            pay.review(admin, confirm=True)   # مايتأكّدش مرتين

        # مايتلغيش بعد الدفع
        self.cust_c.post(self._u('order_cancel', order))
        order.refresh_from_db()
        self.assertEqual(order.status, 'paid')

        # بروفة فيديو ← تعديل ← بروفة ٢ ← موافقة
        video = SimpleUploadedFile('proof.mp4', b'\x00' * 5000, content_type='video/mp4')
        self.shop_c.post(self._u('proof_submit', order), {'file': video, 'note': 'شوف الحركة'})
        p1 = MarketProof.objects.get(order=order)
        self.assertEqual((p1.version, p1.kind), (1, 'video'))
        self.assertContains(self.cust_c.get(self._u('order_detail', order)), '<video')
        # Range للفيديو
        r = self.cust_c.get(self._u('file', None, 'proof', p1.pk), HTTP_RANGE='bytes=0-99')
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r['Content-Range'], 'bytes 0-99/5000')
        self.assertEqual(len(b''.join(r.streaming_content)), 100)
        # التسليم ممنوع قبل موافقة البروفة
        self.shop_c.post(self._u('order_delivered', order))
        order.refresh_from_db()
        self.assertEqual(order.status, 'paid')
        # طلب تعديل لازم يبقى فيه تعليق
        self.cust_c.post(self._u('proof_respond', order, p1.pk), {'action': 'reject', 'comment': ''})
        p1.refresh_from_db()
        self.assertEqual(p1.status, 'pending')
        self.cust_c.post(self._u('proof_respond', order, p1.pk), {'action': 'reject', 'comment': 'كبّر اللوجو'})
        p1.refresh_from_db()
        self.assertEqual(p1.status, 'changes_requested')
        self.shop_c.post(self._u('proof_submit', order), {'file': _png('v2.png')})
        p2 = MarketProof.objects.get(order=order, version=2)
        self.cust_c.post(self._u('proof_respond', order, p2.pk), {'action': 'approve'})
        p2.refresh_from_db()
        self.assertEqual(p2.status, 'approved')
        my = self.cust_c.get(reverse('print_market:my_orders'))
        self.assertContains(my, 'العميل وافق')

        # تسليم ← استلام
        self.shop_c.post(self._u('order_delivered', order))
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        self.cust_c.post(self._u('order_received', order))
        order.refresh_from_db()
        self.assertEqual(order.status, 'completed')

    def test_strangers_and_contact_in_messages(self):
        order = self._new_order()
        self.assertEqual(self.other_c.get(self._u('order_detail', order)).status_code, 404)
        self.assertEqual(Client().get(self._u('order_detail', order)).status_code, 302)
        # عميل تاني مايقدرش يقدّم عرض أو يرد
        self.other_c.post(self._u('offer_submit', order), {'price': '1', 'delivery_days': '1'})
        self.assertFalse(order.offers.exists())
        # رسالة عادية بتتبعت، ورسالة فيها رقم بتتمنع
        self.cust_c.post(self._u('message_send', order), {'body': 'محتاجهم يوم الخميس'})
        self.cust_c.post(self._u('message_send', order), {'body': 'كلمني على 0101 234 5678'})
        self.assertEqual(list(order.messages.values_list('body', flat=True)), ['محتاجهم يوم الخميس'])
        self.cust.refresh_from_db()
        self.assertEqual(self.cust.blocked_attempts, 1)

    def test_proof_not_allowed_before_offer_accepted(self):
        order = self._new_order()
        self.shop_c.post(self._u('proof_submit', order), {'file': _png()})
        self.assertFalse(order.proofs.exists())

    def test_payment_rejected_returns_to_awaiting(self):
        order = self._new_order()
        self.shop_c.post(self._u('offer_submit', order), {'price': '50', 'delivery_days': '1'})
        order.refresh_from_db()
        self.cust_c.post(self._u('offer_respond', order, order.pending_offer.pk), {'action': 'accept'})
        self.cust_c.post(self._u('payment_submit', order), {'method': 'vodafone_cash', 'reference': 'x',
                                                            'receipt': _png()})
        pay = MarketPayment.objects.get(order=order)
        self.assertEqual(pay.fee, Decimal('1.00'))   # ١٪ بحد أدنى جنيه
        pay.review(None, confirm=False, note='مالقيناش التحويل')
        order.refresh_from_db()
        self.assertEqual(order.status, 'awaiting_payment')

    def test_bad_numbers_dont_crash(self):
        """🐛 NaN / Infinity في السعر كانوا بيطلّعوا 500."""
        order = self._new_order()
        for bad in ('NaN', 'Infinity', '-inf', 'sNaN', '1e30'):
            r = self.shop_c.post(self._u('offer_submit', order), {'price': bad, 'delivery_days': '2'})
            self.assertEqual(r.status_code, 302, bad)
            r = self.shop_c.post(self._u('product_new'), {'title': 'كروت', 'price_from': bad})
            self.assertEqual(r.status_code, 200, bad)
        self.assertFalse(order.offers.exists())
        self.assertFalse(MarketProduct.objects.filter(title='كروت').exists())

    def test_home_lists_shops(self):
        r = self.client.get(self._u('home'))
        self.assertContains(r, 'مطبعة النور')
        self.assertContains(r, self._u('shop', None, self.shop.pk))

    # ── تسليم ← تأكيد تلقائي / مشكلة ← تقييم ──
    def _delivered_order(self):
        order = self._new_order()
        self.shop_c.post(self._u('offer_submit', order), {'price': '500', 'delivery_days': '2'})
        order.refresh_from_db()
        self.cust_c.post(self._u('offer_respond', order, order.pending_offer.pk), {'action': 'accept'})
        self.cust_c.post(self._u('payment_submit', order), {'method': 'instapay', 'reference': 'T',
                                                            'receipt': _png()})
        MarketPayment.objects.get(order=order).review(None, confirm=True)
        self.shop_c.post(self._u('order_delivered', order))
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        self.assertIsNotNone(order.delivered_at)
        return order

    @override_settings(PRINT_MARKET_AUTO_CONFIRM_DAYS=3)
    def test_auto_confirm_after_days(self):
        order = self._delivered_order()
        self.assertContains(self.cust_c.get(self._u('order_detail', order)), 'هيتأكّد تلقائي')
        # لسه بدري — مايتأكّدش
        MarketOrder.objects.filter(pk=order.pk).update(delivered_at=timezone.now() - timedelta(days=2))
        self.shop_c.get(self._u('shop_dashboard'))
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        # عدّى ٣ أيام — بيتأكّد ويظهر لصاحب المنصة في التحويلات
        MarketOrder.objects.filter(pk=order.pk).update(delivered_at=timezone.now() - timedelta(days=3, minutes=1))
        from print_market.tasks import auto_complete_orders
        self.assertEqual(auto_complete_orders(), 1)
        order.refresh_from_db()
        self.assertEqual((order.status, order.auto_completed), ('completed', True))
        self.assertTrue(MarketNotification.objects.filter(account=self.shop, title='الطلب اكتمل').exists())
        self.assertEqual(auto_complete_orders(), 0)

    @override_settings(PRINT_MARKET_AUTO_CONFIRM_DAYS=3)
    def test_dispute_stops_auto_confirm_and_payout(self):
        order = self._delivered_order()
        # لازم سبب
        self.cust_c.post(self._u('order_dispute', order), {'reason': ''})
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        # المطبعة ماتقدرش تفتح مشكلة
        self.shop_c.post(self._u('order_dispute', order), {'reason': 'x'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        self.cust_c.post(self._u('order_dispute', order), {'reason': 'الألوان مختلفة عن البروفة'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'disputed')
        # التأكيد التلقائي مايلمسش المشكلة، والعميل مايقدرش يقيّم
        MarketOrder.objects.filter(pk=order.pk).update(delivered_at=timezone.now() - timedelta(days=30))
        self.assertEqual(MarketOrder.auto_complete_due(), [])
        self.cust_c.post(self._u('review_submit', order), {'rating': '1'})
        self.assertFalse(MarketReview.objects.exists())
        # صاحب المنصة بيحكم للعميل ← ملغي ومش هيتحوّل للمطبعة
        order.resolve_dispute(for_shop=False)
        self.assertEqual(order.status, 'cancelled')
        with self.assertRaises(ValidationError):
            order.resolve_dispute(for_shop=True)

    @override_settings(OWNER_NOTIFY_SYNC=True, CATALOG_NOTIFY_EMAIL='owner@mousstec.test',
                       EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_owner_notified_and_paid_but_not_delivered_has_a_way_out(self):
        """🔔 صاحب المنصة بيتبلّغ بالإيصال والمشكلة والتحويل — والعميل اللي دفع والمطبعة ماسلّمتش يقدر يفتح مشكلة."""
        from django.core import mail
        order = self._new_order()
        self.shop_c.post(self._u('offer_submit', order), {'price': '300', 'delivery_days': '2'})
        order.refresh_from_db()
        self.cust_c.post(self._u('offer_respond', order, order.pending_offer.pk), {'action': 'accept'})
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.cust_c.post(self._u('payment_submit', order), {'method': 'instapay', 'reference': 'R9',
                                                                'receipt': _png()})
        owner = [m for m in mail.outbox if 'owner@mousstec.test' in m.to]
        self.assertEqual(len(owner), 1)
        self.assertIn('إيصال دفع', owner[0].subject)
        self.assertIn('/print_market/marketpayment/', owner[0].body)
        MarketPayment.objects.get(order=order).review(None, confirm=True)
        # اتدفع والمطبعة لسه ماسلّمتش ⇒ العميل يقدر يفتح مشكلة (كان مالوش أي مخرج)
        self.assertContains(self.cust_c.get(self._u('order_detail', order)), 'عندي مشكلة في الطلب')
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.cust_c.post(self._u('order_dispute', order), {'reason': 'عدّى أسبوع ومفيش تسليم'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'disputed')
        self.assertTrue([m for m in mail.outbox if 'owner@mousstec.test' in m.to and 'مشكلة' in m.subject])
        # الحكم للمطبعة قبل التسليم ⇒ ترجع تكمّل (مش «مكتمل» من غير تسليم)
        order.resolve_dispute(for_shop=True)
        self.assertEqual(order.status, 'paid')
        # وبعد التسليم والاستلام صاحب المنصة بيتبلّغ يحوّل
        self.shop_c.post(self._u('order_delivered', order))
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.cust_c.post(self._u('order_received', order))
        self.assertTrue([m for m in mail.outbox if 'owner@mousstec.test' in m.to and 'حوّل' in m.subject])

    def test_review_once_after_completed(self):
        order = self._delivered_order()
        self.cust_c.post(self._u('review_submit', order), {'rating': '5'})
        self.assertFalse(MarketReview.objects.exists())   # قبل الاستلام ممنوع
        self.cust_c.post(self._u('order_received', order))
        self.shop_c.post(self._u('review_submit', order), {'rating': '5'})   # المطبعة ماتقيّمش نفسها
        self.cust_c.post(self._u('review_submit', order), {'rating': '9'})
        self.cust_c.post(self._u('review_submit', order), {'rating': '4', 'comment': 'كلمني 01012345678'})
        self.assertFalse(MarketReview.objects.exists())
        self.cust_c.post(self._u('review_submit', order), {'rating': '4', 'comment': 'شغل نضيف وفي الميعاد'})
        self.cust_c.post(self._u('review_submit', order), {'rating': '1'})
        rv = MarketReview.objects.get()
        self.assertEqual((rv.rating, rv.shop_id), (4, self.shop.pk))
        page = self.client.get(self._u('shop', None, self.shop.pk))
        self.assertContains(page, 'شغل نضيف وفي الميعاد')
        self.assertRegex(page.content.decode(), r'<b>4[.,]0</b>')
        self.assertIn('<title>مطبعة النور |', page.content.decode())
        rv.is_visible = False
        rv.save()
        self.assertNotContains(self.client.get(self._u('shop', None, self.shop.pk)), 'شغل نضيف')

    def test_guest_can_order_and_account_is_created(self):
        guest = Client()
        url = reverse('print_market:order_new', args=[self.shop.pk])
        self.assertEqual(guest.get(url).status_code, 200)
        base = {'title': 'كروت', 'details': '500 كارت', 'quantity': '500', 'name': 'منى', 'password': PW}
        # إيميل متسجّل قبل كده ← مفيش طلب
        r = guest.post(url, {**base, 'email': 'cust@x.com'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(MarketOrder.objects.count(), 0)
        r = guest.post(url, {**base, 'email': 'Mona@X.com'})
        self.assertEqual(r.status_code, 302)
        mona = MarketAccount.objects.get(email='mona@x.com')
        self.assertEqual(mona.role, 'customer')
        order = MarketOrder.objects.get(customer=mona)
        # اتعمله دخول
        self.assertEqual(guest.get(self._u('order_detail', order)).status_code, 200)
        # المطبعة ماتطلبش من مطبعة
        self.assertEqual(self.shop_c.get(url).status_code, 302)

    def test_notifications_page_and_bell(self):
        self._new_order()
        n = MarketNotification.objects.get(account=self.shop)
        self.assertEqual(n.title, 'طلب جديد')
        self.assertContains(self.shop_c.get(self._u('shop_dashboard')), 'class="pm-count"')
        self.assertContains(self.shop_c.get(self._u('notifications')), 'طلب جديد')
        n.refresh_from_db()
        self.assertTrue(n.is_read)
        self.assertNotContains(self.shop_c.get(self._u('shop_dashboard')), 'class="pm-count"')
        # العميل مايشوفش إشعارات المطبعة
        self.assertNotContains(self.cust_c.get(self._u('notifications')), 'طلب جديد')

    @override_settings(WHATSAPP_ACCESS_TOKEN='t', WHATSAPP_PHONE_NUMBER_ID='p')
    def test_shop_whatsapp_private_and_notified(self):
        self.shop_c.post(self._u('settings'), {'name': 'مطبعة النور', 'city': 'القاهرة', 'whatsapp': '١٢٣'})
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.whatsapp, '')   # رقم غلط
        self.shop_c.post(self._u('settings'), {'name': 'مطبعة النور', 'city': 'القاهرة',
                                               'whatsapp': '01012345678'})
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.whatsapp, '201012345678')
        with mock.patch('print_market.notify.send_shop_whatsapp_now') as wa, \
                mock.patch('print_market.tasks.send_shop_whatsapp.delay', side_effect=Exception('no broker')), \
                self.captureOnCommitCallbacks(execute=True):
            order = self._new_order()
        wa.assert_called_once()
        self.assertEqual(wa.call_args[0][0], '201012345678')
        # الرقم مابيظهرش للعميل في أي صفحة
        for url in (self._u('shop', None, self.shop.pk), self._u('order_detail', order), self._u('home')):
            self.assertNotContains(self.cust_c.get(url), '1012345678')
        # والعميل مابيوصلوش واتساب
        with mock.patch('print_market.notify.send_shop_whatsapp_now') as wa, \
                self.captureOnCommitCallbacks(execute=True):
            self.shop_c.post(self._u('offer_submit', order), {'price': '10', 'delivery_days': '1'})
        wa.assert_not_called()

    def test_transfer_fee_limits(self):
        self.assertEqual(transfer_fee(Decimal('100000'), 'instapay'), Decimal('20.00'))
        self.assertEqual(transfer_fee(Decimal('100'), 'instapay'), Decimal('0.50'))
        self.assertEqual(transfer_fee(Decimal('5000'), 'vodafone_cash'), Decimal('50.00'))


@override_settings(ALLOWED_HOSTS=['*'])
class PrintMarketIsolationTests(_TenantDomainMixin, TestCase):
    """🔒 سوق المطابع مفصول عن حسابات السيستم:
    * صاحب شركة (superuser جوه سكيمة شركته) مايشوفش ملفات/إيصالات السوق — صاحب المنصة (public) بس.
    * دخول السيستم (Django) مابيدّيش حساب في السوق، ودخول السوق مابيدّيش دخول للسيستم.
    """

    def setUp(self):
        cache.clear()
        self.shop = MarketAccount(role='shop', name='مطبعة', email='iso-shop@x.com', city='القاهرة')
        self.cust = MarketAccount(role='customer', name='عميل', email='iso-cust@x.com')
        for a in (self.shop, self.cust):
            a.set_password(PW)
            a.save()
        self.order = MarketOrder.objects.create(customer=self.cust, shop=self.shop, title='كروت', details='x',
                                                quantity=1, status='awaiting_payment')
        from print_market.models import MarketOffer
        MarketOffer.objects.create(order=self.order, price=Decimal('100'), status='accepted')
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=self.media):
            self.pay = MarketPayment.submit(self.order, self.cust, 'instapay', 'T1', _png())

    def test_company_owner_superuser_cannot_read_market_receipts(self):
        from unittest import mock

        from print_market import views
        from django.test import RequestFactory
        owner = User.objects.create(username='company_owner_iso', is_superuser=True, is_staff=True)
        req = RequestFactory().get('/')
        req.user = owner
        req.COOKIES = {}
        from django.http import Http404
        with override_settings(MEDIA_ROOT=self.media):
            # على دومين شركة (schema مش public) ⇒ ممنوع
            with mock.patch('clients.permissions.connection') as conn:
                conn.schema_name = 'some_company'
                with self.assertRaises(Http404):
                    views.private_file(req, 'receipt', self.pay.pk)
            # صاحب المنصة على public ⇒ مسموح
            self.assertEqual(views.private_file(req, 'receipt', self.pay.pk).status_code, 200)

    def test_django_login_is_not_a_market_login_and_vice_versa(self):
        c = Client()
        c.force_login(User.objects.create(username='staff_iso', email='iso-cust@x.com', is_staff=True))
        # نفس الإيميل في السيستم مايدخلش السوق
        self.assertEqual(c.get(reverse('print_market:my_orders')).status_code, 302)
        m = Client()
        r = m.post(reverse('print_market:login'), {'email': 'iso-cust@x.com', 'password': PW})
        self.assertEqual(r.status_code, 302)
        self.assertIn('pm_session', m.cookies)
        self.assertNotIn('sessionid', m.cookies)   # مفيش جلسة Django
        self.assertEqual(m.get(reverse('print_market:my_orders')).status_code, 200)
        # ومايقدرش يفتح السوبر أدمن ولا أدمن المنصة
        self.assertNotEqual(m.get('/superadmin/live/').status_code, 200)
        self.assertNotEqual(m.get('/secure-portal/').status_code, 200)
