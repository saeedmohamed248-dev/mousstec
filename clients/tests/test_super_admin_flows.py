"""
🔁 دورات لوحة المشرف اللي كانت بتقف في النص — لازم تكمل لآخرها والعميل يعرف.

* طلب الطباعة: عرض سعر ← العميل يوافق ← طباعة ← شحن ← تسليم (كان مفيش ولا زرار).
* إيصال فودافون كاش اتأكد/اترفض ⇒ العميل بيتبلّغ (والسبب لو اترفض).
* باقة التصاميم اتفعّلت/اترفضت ⇒ إشعار.
* قطعة الغيار اتنشرت/اترفضت ⇒ البائع بيتبلّغ.
* حظر / فك حظر عميل من اللوحة نفسها.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import Client, TestCase

from clients.models import (
    CustomerDesign, CustomerNotification, DesignPrintRequest, ManualPaymentReceipt,
    MarketplaceCustomer,
)

_PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
        b'\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')


def _customer(sector='printing'):
    return MarketplaceCustomer.objects.create(
        customer_type='individual', full_name='عميل اختبار', sector=sector, is_verified=True,
        phone=f'+2010{uuid.uuid4().int % 100000000:08d}')


class SuperAdminFlowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # المسارات دي على الـ public schema — testserver لازم يكون دومين ليها
        from django_tenants.utils import get_tenant_domain_model, get_tenant_model
        Tenant, Domain = get_tenant_model(), get_tenant_domain_model()
        cls._pub = Tenant.objects.filter(schema_name='public').first()
        cls._made_pub = cls._pub is None
        if cls._made_pub:
            cls._pub = Tenant(schema_name='public', name='Public', owner_name='o', phone='0')
            cls._pub.auto_create_schema = False
            cls._pub.save(verbosity=0)
        cls._dom = None
        if not Domain.objects.filter(domain='testserver').exists():
            cls._dom = Domain.objects.create(tenant=cls._pub, domain='testserver', is_primary=False)

    @classmethod
    def tearDownClass(cls):
        if cls._dom is not None:
            cls._dom.delete()
        if cls._made_pub:
            cls._pub.delete(force_drop=False)
        super().tearDownClass()

    def setUp(self):
        self.owner = User.objects.create_user('flows_owner@test.com', password='x', is_staff=True, is_superuser=True)
        self.admin = Client()
        self.admin.force_login(self.owner)
        self.customer = _customer()
        self.cust_client = Client()
        self.cust_client.cookies['mp_session'] = str(self.customer.session_token)

    def _notes(self, customer=None):
        return list(CustomerNotification.objects.filter(customer=customer or self.customer).order_by('pk'))

    # ── 🖨️ طلب الطباعة ───────────────────────────────────────────────
    def _print_request(self, customer=None):
        customer = customer or self.customer
        design = CustomerDesign.objects.create(customer=customer, title='لوجو', description='لوجو محل')
        return DesignPrintRequest.objects.create(design=design, customer=customer, product_type='business_card',
                                                 quantity=500)

    def _step(self, pr, action, **data):
        return self.admin.post(f'/superadmin/print-request/{pr.pk}/update/', {'action': action, **data})

    def test_print_request_goes_all_the_way_to_delivery(self):
        pr = self._print_request()
        html = self.admin.get('/superadmin/').content.decode()
        self.assertIn(f'openPrintQuote({pr.pk},', html)

        # خطوة مش في وقتها مرفوضة
        self.assertEqual(self._step(pr, 'shipped').status_code, 409)
        self.assertEqual(self._step(pr, 'quote', price='0').status_code, 400)

        r = self._step(pr, 'quote', price='450', note='التسليم خلال ٣ أيام')
        self.assertTrue(r.json()['ok'], r.content)
        pr.refresh_from_db()
        self.assertEqual((pr.status, pr.quoted_price), ('quoted', Decimal('450.00')))
        self.assertIn('450', self._notes()[-1].body)

        # العميل شايف العرض وزرار الموافقة، ويوافق
        page = self.cust_client.get('/marketplace/design-store/print-orders/').content.decode()
        self.assertIn('respondQuote(', page)
        r = self.cust_client.post(f'/marketplace/design-store/print-orders/{pr.request_code}/respond/',
                                  {'decision': 'accept'})
        self.assertEqual(r.status_code, 200, r.content)
        pr.refresh_from_db()
        self.assertEqual(pr.status, 'accepted')
        # الطلب المقبول بيفضل في اللوحة ومحسوب «مستني الإدارة» (كان بيختفي)
        r = self.admin.get('/superadmin/')
        self.assertIn(pr.pk, [p.pk for p in r.context['pending_print_requests']])
        self.assertEqual(r.context['print_needs_action'], 1)
        self.assertIn(f"printStep({pr.pk}, 'in_production')", r.content.decode())

        for action, status in (('in_production', 'in_production'), ('shipped', 'shipped'), ('delivered', 'delivered')):
            before = len(self._notes())
            self.assertTrue(self._step(pr, action).json()['ok'])
            pr.refresh_from_db()
            self.assertEqual(pr.status, status)
            self.assertEqual(len(self._notes()), before + 1)
        # اتسلّم ⇒ خرج من اللوحة
        self.assertNotIn(pr.pk, [p.pk for p in self.admin.get('/superadmin/').context['pending_print_requests']])

    def test_customer_responds_only_to_own_quoted_order(self):
        pr = self._print_request()
        url = f'/marketplace/design-store/print-orders/{pr.request_code}/respond/'
        self.assertEqual(self.cust_client.post(url, {'decision': 'accept'}).status_code, 409)   # لسه مفيش عرض
        self._step(pr, 'quote', price='100')
        stranger = Client()
        stranger.cookies['mp_session'] = str(_customer().session_token)
        self.assertEqual(stranger.post(url, {'decision': 'accept'}).status_code, 404)
        self.assertEqual(Client().post(url, {'decision': 'accept'}).status_code, 401)
        self.assertEqual(self.cust_client.post(url, {'decision': 'decline'}).status_code, 200)
        pr.refresh_from_db()
        self.assertEqual(pr.status, 'cancelled')

    def test_cancel_notifies_with_reason(self):
        pr = self._print_request()
        self.assertTrue(self._step(pr, 'cancel', note='الخامة مش متوفرة').json()['ok'])
        self.assertIn('الخامة مش متوفرة', self._notes()[-1].body)

    # ── 💵 إيصالات فودافون كاش ───────────────────────────────────────
    def test_receipt_rejection_reaches_the_customer_with_the_reason(self):
        receipt = ManualPaymentReceipt(purchase_type='design', purchase_id=999999, amount=Decimal('150'),
                                       sender_phone='01099998888', txn_reference='TXN-777', customer=self.customer)
        receipt.receipt_image.save('r.png', ContentFile(_PNG), save=True)
        r = self.admin.post(f'/payment/manual/admin/review/{receipt.receipt_code}/',
                            {'action': 'reject', 'notes': 'المبلغ ماوصلش'})
        self.assertTrue(r.json()['ok'], r.content)
        note = self._notes()[-1]
        self.assertEqual(note.level, 'danger')
        self.assertIn('المبلغ ماوصلش', note.body)

    def test_receipt_confirmation_reaches_the_customer(self):
        receipt = ManualPaymentReceipt(purchase_type='design', purchase_id=999998, amount=Decimal('150'),
                                       sender_phone='01099998888', txn_reference='TXN-778', customer=self.customer)
        receipt.receipt_image.save('r.png', ContentFile(_PNG), save=True)
        self.assertTrue(self.admin.post(f'/payment/manual/admin/review/{receipt.receipt_code}/',
                                        {'action': 'confirm'}).json()['ok'])
        self.assertEqual(self._notes()[-1].level, 'success')

    # ── 🎨 باقة التصاميم ─────────────────────────────────────────────
    def test_design_purchase_confirm_and_reject_notify(self):
        from clients.models import DesignPackage, DesignPurchase
        pkg = DesignPackage.objects.create(slug=f'p-{uuid.uuid4().hex[:6]}', name_ar='باقة ١٠', designs_count=10,
                                           price_egp=Decimal('100'), price_per_design=Decimal('10'))
        ok = DesignPurchase.objects.create(customer=self.customer, package=pkg, designs_total=10,
                                           price_paid=Decimal('100'), status='awaiting_confirm')
        bad = DesignPurchase.objects.create(customer=self.customer, package=pkg, designs_total=10,
                                            price_paid=Decimal('100'), status='awaiting_confirm')
        self.admin.post(f'/marketplace/design-store/confirm-payment/{ok.pk}/', {'action': 'confirm'})
        self.assertEqual(self._notes()[-1].level, 'success')
        self.admin.post(f'/marketplace/design-store/confirm-payment/{bad.pk}/', {'action': 'reject'})
        self.assertEqual(self._notes()[-1].level, 'danger')

    # ── 🚗 قطع الغيار ───────────────────────────────────────────────
    def test_part_moderation_tells_the_seller(self):
        from clients.models import PartCarMake, PartListing
        seller = _customer(sector='automotive')
        make, _ = PartCarMake.objects.get_or_create(slug='flows-make', defaults={'name': 'Flows Make'})
        a = PartListing.objects.create(title='فانوس', description='x', car_make=make, price_egp=Decimal('500'),
                                       moderation_status='pending_approval', seller_customer=seller)
        b = PartListing.objects.create(title='مراية', description='x', car_make=make, price_egp=Decimal('300'),
                                       moderation_status='pending_approval', seller_customer=seller)
        self.admin.post(f'/superadmin/parts/moderation/{a.pk}/approve/', HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertIn('اتنشرت', self._notes(seller)[-1].title)
        # موافقة تانية على نفس القطعة مابتبعتش إشعار مكرر
        self.admin.post(f'/superadmin/parts/moderation/{a.pk}/approve/', HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(len(self._notes(seller)), 1)
        self.admin.post(f'/superadmin/parts/moderation/{b.pk}/reject/', data='{"reason": "الصور مش واضحة"}',
                        content_type='application/json')
        self.assertIn('الصور مش واضحة', self._notes(seller)[-1].body)

    # ── 🚫 حظر عميل ─────────────────────────────────────────────────
    def test_block_toggle_from_the_dashboard(self):
        html = self.admin.get('/superadmin/').content.decode()
        self.assertIn(f'toggleCustomerBlock({self.customer.pk},', html)
        r = self.admin.post(f'/superadmin/customer/{self.customer.pk}/block/')
        self.assertTrue(r.json()['is_blocked'])
        # المحظور مابيقدرش يستخدم السوق
        self.assertEqual(self.cust_client.post(
            f'/marketplace/design-store/print-orders/{uuid.uuid4()}/respond/', {'decision': 'accept'}).status_code, 401)
        r = self.admin.post(f'/superadmin/customer/{self.customer.pk}/block/')
        self.assertFalse(r.json()['is_blocked'])

    def test_new_endpoints_are_owner_only(self):
        pr = self._print_request()
        anon = Client()
        self.assertNotEqual(anon.post(f'/superadmin/print-request/{pr.pk}/update/', {'action': 'cancel'}).status_code, 200)
        self.assertNotEqual(anon.post(f'/superadmin/customer/{self.customer.pk}/block/').status_code, 200)
        pr.refresh_from_db()
        self.assertEqual(pr.status, 'pending')
