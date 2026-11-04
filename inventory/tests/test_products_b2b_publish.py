"""
🌐 صفحة القطع — زرار «نشر في سوق التجار» (B2B).

* الأصناف المحددة بيتقدّم لها طلب نشر (B2BListingRequest) قيد المراجعة، وبتتعلّم «منشورة».
* صنف ملوش رصيد أو عليه طلب لسه قيد المراجعة → بيتساب.
* الكاشير مايقدرش ينشر، والباقة اللي مافيهاش «سوق B2B» بترجع رسالة ترقية.
* 🐛 الموافقة على طلب من أكشن الأدمن (من غير ما الصنف يتعلّم منشور) كانت بتشيله من السوق.
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import B2BListingRequest, Product
from inventory.services.inventory_service import InventoryService
from .base import ERPTenantTestCase
from .factories import make_branch, make_employee, make_inventory, make_product

URL = '/system/products/b2b-publish/'
IN_PLAN = patch('clients.services.entitlements.EntitlementService.has', return_value=True)


@override_settings(ALLOWED_HOSTS=['*'])
class ProductB2BPublishTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.ok = make_product(part_number='BB-1', name='صنف متاح', retail_price='100',
                               purchase_price='60')
        Product.objects.filter(pk=self.ok.pk).update(b2b_wholesale_price=Decimal('80'))
        make_inventory(self.ok, self.branch, quantity=4)
        self.empty = make_product(part_number='BB-2', name='صنف خلصان', retail_price='50')
        make_inventory(self.empty, self.branch, quantity=0)
        self.pending = make_product(part_number='BB-3', name='صنف عليه طلب', retail_price='70')
        make_inventory(self.pending, self.branch, quantity=2)
        B2BListingRequest.objects.create(product=self.pending, requested_price=Decimal('70'))
        self.boss = User.objects.create_user('bb_boss', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.boss)

    def _post(self, **data):
        return self.c.post(URL, data, HTTP_HOST=self.host)

    def test_button_shows_on_products_page(self):
        page = self.c.get('/system/products/', HTTP_HOST=self.host)
        self.assertContains(page, URL)
        self.assertContains(page, 'b2bPublish()')

    def test_publish_selected_creates_pending_requests(self):
        with IN_PLAN:
            r = self._post(ids=f'{self.ok.pk},{self.empty.pk},{self.pending.pk}')
        self.assertEqual(r.status_code, 302)
        loc = r['Location']
        self.assertIn('b2b=sent', loc)
        self.assertIn('b2b_sent=1', loc)
        self.assertIn('b2b_pending=1', loc)
        self.assertIn('b2b_nostock=1', loc)
        req = B2BListingRequest.objects.get(product=self.ok)
        self.assertEqual(req.status, 'pending')
        self.assertEqual(req.requested_price, Decimal('100'))  # سعر البيع مش الجملة (80)
        self.assertEqual(req.requested_by, self.boss)
        self.assertTrue(Product.objects.get(pk=self.ok.pk).is_b2b_published)
        self.assertFalse(B2BListingRequest.objects.filter(product=self.empty).exists())
        self.assertEqual(B2BListingRequest.objects.filter(product=self.pending).count(), 1)
        # الضغط تاني مايكرّرش الطلب
        with IN_PLAN:
            self._post(ids=str(self.ok.pk))
        self.assertEqual(B2BListingRequest.objects.filter(product=self.ok).count(), 1)

    def test_publish_all_results_of_filter(self):
        with IN_PLAN:
            r = self._post(all='1', q='BB-1')
        self.assertIn('b2b_sent=1', r['Location'])
        self.assertTrue(B2BListingRequest.objects.filter(product=self.ok).exists())

    def test_plan_without_market_is_refused(self):
        with patch('clients.services.entitlements.EntitlementService.has', return_value=False):
            r = self._post(ids=str(self.ok.pk))
        self.assertIn('b2b=plan', r['Location'])
        self.assertFalse(B2BListingRequest.objects.filter(product=self.ok).exists())

    def test_cashier_cannot_publish(self):
        cashier, _p = make_employee(username='bb_cash', role='cashier', branch=self.branch)
        c = Client()
        c.force_login(cashier)
        with IN_PLAN:
            r = c.post(URL, {'ids': str(self.ok.pk)}, HTTP_HOST=self.host)
        self.assertIn('b2b=perm', r['Location'])
        self.assertFalse(B2BListingRequest.objects.filter(product=self.ok).exists())

    def test_approval_marks_product_published_before_sync(self):
        req = B2BListingRequest.objects.get(product=self.pending)
        self.assertFalse(Product.objects.get(pk=self.pending.pk).is_b2b_published)
        seen = {}
        with patch.object(InventoryService, 'sync_product_to_b2b',
                          side_effect=lambda p, override_price=None: seen.update(pub=p.is_b2b_published)):
            InventoryService.approve_b2b_listing(req, Decimal('65'), self.boss)
        self.assertTrue(seen['pub'])
        self.assertTrue(Product.objects.get(pk=self.pending.pk).is_b2b_published)
        self.assertEqual(B2BListingRequest.objects.filter(product=self.pending).count(), 1)


    def test_market_price_is_retail_with_wholesale_fallback(self):
        """🐛 السوق كان بينزل الصنف بسعر الجملة — المطلوب سعر البيع، ولو صفر الجملة."""
        self.assertEqual(InventoryService.b2b_market_price(self.ok), Decimal('100'))
        Product.objects.filter(pk=self.ok.pk).update(retail_price=Decimal('0'))
        self.assertEqual(InventoryService.b2b_market_price(Product.objects.get(pk=self.ok.pk)), Decimal('80'))
