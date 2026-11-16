"""
🏬 أصناف الشركات المعتمدة لازم تظهر في سوق قطع الغيار العام ويتطلبوا.

🐛 الشركة (زي FixIt) تطلب نشر أصنافها والإدارة توافق، والأصناف كانت بتتحط في
``GlobalB2BMarketplace`` اللي مالوش أي صفحة عامة — صاحب المنصة يوافق ومايلاقيش حاجة
في «سوق السيارات». وكمان الصنف اللي رصيده صفر وقت الموافقة كان بيتشال من غير ولا كلمة
ومايرجعش لما المخزون يرجع.
"""
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import Client as HttpClient
from django_tenants.utils import schema_context

from clients.models import GlobalB2BMarketplace, MarketplaceCustomer
from clients.services import b2b_listing_review as svc
from clients.services import parts_stock_orders as stock_svc
from inventory.models import B2BListingRequest, Branch, Product, SaleInvoice
from .base import ERPTenantTestCase
from .factories import make_branch, make_inventory, make_product


class _PublicDomainMixin:
    """صفحات السوق على public — بنربط testserver بيها."""
    _provisioned_domain = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django_tenants.utils import get_tenant_domain_model, get_tenant_model
        Tenant, Domain = get_tenant_model(), get_tenant_domain_model()
        connection.set_schema_to_public()
        public = Tenant.objects.filter(schema_name='public').first()
        if public is None:
            public = Tenant(schema_name='public', name='Public', owner_name='T', phone='000')
            public.auto_create_schema = False
            public.save(verbosity=0)
        if not Domain.objects.filter(domain='testserver').exists():
            cls._provisioned_domain = Domain.objects.create(tenant=public, domain='testserver', is_primary=False)
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        if cls._provisioned_domain is not None:
            connection.set_schema_to_public()
            try:
                cls._provisioned_domain.delete()
            except Exception:  # noqa: BLE001
                pass
        super().tearDownClass()


class PartsStockPublicTests(_PublicDomainMixin, ERPTenantTestCase):
    def setUp(self):
        cache.clear()
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(
            status='active', is_active=True, is_marketplace_active=True,
            is_fraud_flagged=False, industry='automotive')
        self.tenant.refresh_from_db()
        B2BListingRequest.objects.all().delete()
        self.sku = f'FX-{uuid.uuid4().hex[:6]}'
        self.product = make_product(part_number=self.sku, name=f'طرمبة بنزين {self.sku}',
                                    retail_price='1500', car_model='E90 / E92')
        self.branch = Branch.objects.first() or make_branch(name=f'فرع {self.sku}')
        Product.objects.filter(pk=self.product.pk).update(is_b2b_published=True)
        self.product.refresh_from_db()
        self.req = B2BListingRequest.objects.create(product=self.product, requested_price=Decimal('1500'))
        connection.set_schema_to_public()

    def tearDown(self):
        connection.set_schema_to_public()
        GlobalB2BMarketplace.objects.filter(tenant=self.tenant).delete()
        MarketplaceCustomer.objects.filter(full_name='مشتري السوق').delete()
        connection.set_tenant(self.tenant)

    def _stock(self, qty):
        with schema_context(self.tenant.schema_name):
            make_inventory(self.product, self.branch, quantity=qty)

    def _approve(self, price='1400'):
        info = {}
        svc.decide(self.tenant.schema_name, self.req.pk, 'approve', reviewer_label='سعيد', price=price, info=info)
        return info

    def _row(self):
        return GlobalB2BMarketplace.objects.filter(tenant=self.tenant, part_number=self.sku).first()

    def _buyer(self):
        cust = MarketplaceCustomer.objects.create(
            customer_type='individual', full_name='مشتري السوق',
            phone=f'+2010{uuid.uuid4().int % 10**8:08d}', sector='automotive', city='طنطا',
            is_verified=True)
        c = HttpClient()
        c.cookies['mp_session'] = str(cust.session_token)
        return cust, c

    # ── الظهور ─────────────────────────────────────────────────────────
    def test_approved_company_product_shows_in_public_parts_market(self):
        self._stock(5)
        info = self._approve()
        self.assertEqual(info['stock'], 5)
        row = self._row()
        self.assertIsNotNone(row)
        self.assertEqual(row.wholesale_price, Decimal('1400'))
        self.assertEqual(row.car_model, 'E90 / E92')

        html = HttpClient().get('/marketplace/parts/').content.decode()
        self.assertIn('من مخازن التجار', html)
        self.assertIn(self.product.name, html)
        self.assertIn(f'/marketplace/parts/stock/{row.pk}/', html)

        # البحث برقم القطعة بيلاقيه
        html = HttpClient().get('/marketplace/parts/', {'q': self.sku}).content.decode()
        self.assertIn(self.product.name, html)

        detail = HttpClient().get(f'/marketplace/parts/stock/{row.pk}/')
        self.assertEqual(detail.status_code, 200)
        self.assertIn(self.tenant.name, detail.content.decode())

    def test_hidden_when_company_left_marketplace(self):
        self._stock(5)
        self._approve()
        row = self._row()
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(is_marketplace_active=False)
        html = HttpClient().get('/marketplace/parts/').content.decode()
        self.assertNotIn(self.product.name, html)
        self.assertEqual(HttpClient().get(f'/marketplace/parts/stock/{row.pk}/').status_code, 404)

    # ── رصيد صفر ───────────────────────────────────────────────────────
    def test_zero_stock_approval_is_reported_and_relists_on_restock(self):
        info = self._approve(price='1300')
        self.assertEqual(info['stock'], 0)
        self.assertIsNone(self._row())

        # المخزون رجع ⇒ مزامنة حركة المخزون بترجّعه لوحده بالسعر المعتمد (من غير طلب جديد)
        self._stock(4)
        from clients.tasks import async_sync_b2b_marketplace_product
        res = async_sync_b2b_marketplace_product.apply(args=[self.tenant.schema_name, self.product.pk])
        self.assertEqual(res.result, 'relisted')
        row = self._row()
        self.assertEqual(row.wholesale_price, Decimal('1300'))
        self.assertEqual(row.available_qty, 4)

    def test_restock_never_publishes_a_product_that_was_never_approved(self):
        self._stock(4)
        from clients.tasks import async_sync_b2b_marketplace_product
        res = async_sync_b2b_marketplace_product.apply(args=[self.tenant.schema_name, self.product.pk])
        self.assertEqual(res.result, 'skipped')
        self.assertIsNone(self._row())

    # ── الطلب ─────────────────────────────────────────────────────────
    def test_order_creates_quotation_invoice_in_company_and_notifies(self):
        self._stock(5)
        self._approve()
        row = self._row()
        cust, c = self._buyer()
        sent = []
        with patch.object(stock_svc, '_notify_tenant', side_effect=lambda t, s, b: sent.append((t.pk, s, b))):
            resp = c.post(f'/marketplace/parts/stock/{row.pk}/', {'quantity': '2', 'note': 'BMW 320i 2010'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('طلبك وصل', resp.content.decode())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], self.tenant.pk)

        with schema_context(self.tenant.schema_name):
            inv = SaleInvoice.objects.filter(sales_channel='marketplace').order_by('-pk').first()
            self.assertIsNotNone(inv)
            self.assertEqual(inv.status, 'quotation')
            self.assertIn(f'[سوق Mouss Tec #{row.pk}]', inv.notes)
            self.assertIn('BMW 320i 2010', inv.notes)
            self.assertFalse(inv.customer.is_walk_in)
            self.assertEqual(inv.customer.name, 'مشتري السوق')
            item = inv.items.get()
            self.assertEqual(item.quantity, 2)
            self.assertEqual(item.unit_price, Decimal('1400'))
            # عرض سعر ⇒ المخزون مااتخصمش لسه (بيتخصم لما الشركة تعتمد)
            self.assertEqual(Product.objects.get(pk=self.product.pk).total_inventory_qty, 5)

    def test_order_more_than_live_stock_is_refused(self):
        self._stock(5)
        self._approve()
        row = self._row()
        self._stock(1)   # الرصيد نزل في سيستم الشركة والسوق لسه ماتحدّثش
        _cust, c = self._buyer()
        with patch.object(stock_svc, '_notify_tenant') as notify:
            resp = c.post(f'/marketplace/parts/stock/{row.pk}/', {'quantity': '3'})
        self.assertIn('المتاح عند المحل 1 بس', resp.content.decode())
        notify.assert_not_called()
        with schema_context(self.tenant.schema_name):
            self.assertFalse(SaleInvoice.objects.filter(sales_channel='marketplace').exists())

    def test_anonymous_order_goes_to_login(self):
        self._stock(5)
        self._approve()
        row = self._row()
        resp = HttpClient().post(f'/marketplace/parts/stock/{row.pk}/', {'quantity': '1'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('next=', resp['Location'])
        with schema_context(self.tenant.schema_name):
            self.assertFalse(SaleInvoice.objects.filter(sales_channel='marketplace').exists())
