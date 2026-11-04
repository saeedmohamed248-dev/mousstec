"""
🌐 طلبات نشر الشركات في سوق B2B لازم توصل لإدارة Mouss Tec.

🐛 الشركة كانت تدوس «نشر في سوق التجار» والطلب يتسجّل في schema الشركة بس —
لوحة السوبر أدمن (public) ماكانتش بتشوفه خالص. دلوقتي
``clients.services.b2b_listing_review`` بيجمع الطلبات من كل الشركات، والموافقة/الرفض
بيتنفّذوا جوه schema الشركة (الصفحة نفسها في clients/tests/test_superadmin_b2b_page.py).
"""
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django_tenants.utils import schema_context

from clients.services import b2b_listing_review as svc
from inventory.models import B2BListingRequest, Product
from inventory.services.inventory_service import InventoryService
from .base import ERPTenantTestCase
from .factories import make_branch, make_inventory, make_product


class B2BListingReviewServiceTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(status='active')
        B2BListingRequest.objects.all().delete()
        self.sku = f'SA-B2B-{uuid.uuid4().hex[:6]}'
        self.product = make_product(part_number=self.sku, name='فلتر زيت للسوق', retail_price='120')
        from inventory.models import Branch
        self.branch = Branch.objects.first() or make_branch(name=f'فرع {self.sku}')
        make_inventory(self.product, self.branch, quantity=3)
        Product.objects.filter(pk=self.product.pk).update(is_b2b_published=True)
        self.req = B2BListingRequest.objects.create(product=self.product, requested_price=Decimal('90'))
        # السوبر أدمن شغّال من public
        connection.set_schema_to_public()

    def tearDown(self):
        connection.set_tenant(self.tenant)

    def _mine(self, status='pending'):
        return [r for r in svc.collect_requests(status=status) if r['schema'] == self.tenant.schema_name]

    def test_pending_request_is_visible_from_public(self):
        rows = self._mine()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], self.req.pk)
        self.assertEqual(rows[0]['part_number'], self.sku)
        self.assertEqual(rows[0]['stock'], 3)
        # الطلب متسجّل بـ90 (جملة قديم) — بيظهر وبيتنشر بسعر البيع الحالي 120
        self.assertEqual(rows[0]['requested_price'], Decimal('120'))
        self.assertGreaterEqual(svc.pending_count(), 1)

    def test_approve_publishes_with_admin_price(self):
        seen = {}
        with patch.object(InventoryService, 'sync_product_to_b2b',
                          side_effect=lambda p, override_price=None: seen.update(
                              price=override_price, pub=p.is_b2b_published, schema=connection.schema_name)):
            svc.decide(self.tenant.schema_name, self.req.pk, 'approve', reviewer_label='سعيد', price='85')
        self.assertEqual(seen, {'price': Decimal('85'), 'pub': True, 'schema': self.tenant.schema_name})
        with schema_context(self.tenant.schema_name):
            req = B2BListingRequest.objects.get(pk=self.req.pk)
            self.assertEqual(req.status, 'approved')
            self.assertEqual(req.approved_price, Decimal('85'))
            self.assertIsNone(req.reviewed_by)          # مستخدم public مش في schema الشركة
            self.assertIn('سعيد', req.review_notes)
        self.assertEqual(self._mine(), [])
        self.assertEqual(len(self._mine('approved')), 1)
        # موافقة تاني على نفس الطلب مرفوضة
        with self.assertRaises(svc.ReviewError):
            svc.decide(self.tenant.schema_name, self.req.pk, 'approve')

    def test_reject_keeps_reason_and_does_not_publish(self):
        with patch.object(InventoryService, 'sync_product_to_b2b') as sync:
            svc.decide(self.tenant.schema_name, self.req.pk, 'reject', notes='الصورة مش واضحة')
        sync.assert_not_called()
        with schema_context(self.tenant.schema_name):
            req = B2BListingRequest.objects.get(pk=self.req.pk)
            self.assertEqual(req.status, 'rejected')
            self.assertIn('الصورة مش واضحة', req.review_notes)

    def test_bad_input_is_refused(self):
        for kwargs in ({'action': 'approve', 'price': '0'}, {'action': 'approve', 'price': 'abc'},
                       {'action': 'delete'}):
            action = kwargs.pop('action')
            with self.assertRaises(svc.ReviewError):
                svc.decide(self.tenant.schema_name, self.req.pk, action, **kwargs)
        with self.assertRaises(svc.ReviewError):
            svc.decide('public', self.req.pk, 'approve')
        with self.assertRaises(svc.ReviewError):
            svc.decide(self.tenant.schema_name, 999999, 'approve')


    def test_approve_many_selected_and_all(self):
        # طلب تاني لصنف تاني — والموافقة الجماعية بسعر البيع لكل واحد
        connection.set_tenant(self.tenant)
        p2 = make_product(part_number=f'{self.sku}-2', name='تيل فرامل للسوق', retail_price='300')
        make_inventory(p2, self.branch, quantity=1)
        r2 = B2BListingRequest.objects.create(product=p2, requested_price=Decimal('250'))
        connection.set_schema_to_public()
        prices = {}
        with patch.object(InventoryService, 'sync_product_to_b2b',
                          side_effect=lambda p, override_price=None: prices.update({p.pk: override_price})):
            res = svc.approve_many([(self.tenant.schema_name, self.req.pk),
                                    (self.tenant.schema_name, self.req.pk)], reviewer_label='سعيد')
            self.assertEqual((res['approved'], res['skipped']), (1, 0))   # المكرر اتحسب مرة
            res = svc.approve_many(None)                                 # «الكل» = اللي فاضل معلّق
        self.assertEqual(res['approved'], 1)
        self.assertEqual(prices, {self.product.pk: Decimal('120'), p2.pk: Decimal('300')})
        self.assertEqual(self._mine(), [])
        # اتوافق عليه قبل كده → بيتساب مش بيوقّع
        res = svc.approve_many([(self.tenant.schema_name, r2.pk)])
        self.assertEqual((res['approved'], res['skipped']), (0, 1))


    def test_reprice_command_moves_published_listings_to_retail(self):
        """الإعلانات اللي اتنشرت بسعر الجملة قبل التصليح بتتعدّل لسعر البيع."""
        from io import StringIO
        from django.core.management import call_command
        from clients.models import GlobalB2BMarketplace
        listing = GlobalB2BMarketplace.objects.create(
            tenant=self.tenant, part_number=self.sku, condition=self.product.condition,
            product_name=self.product.name, brand='', wholesale_price=Decimal('90'), available_qty=3)
        call_command('b2b_reprice_to_retail', '--dry-run', stdout=StringIO())
        listing.refresh_from_db()
        self.assertEqual(listing.wholesale_price, Decimal('90'))       # dry-run مابيحفظش
        out = StringIO()
        call_command('b2b_reprice_to_retail', stdout=out)
        listing.refresh_from_db()
        self.assertEqual(listing.wholesale_price, Decimal('120'))
        self.assertIn('90', out.getvalue())
        listing.delete()
