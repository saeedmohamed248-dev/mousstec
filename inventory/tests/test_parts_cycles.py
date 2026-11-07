"""
📋💳📝 دورات قطع الغيار الجديدة:
* الجرد الفعلي من البوابة (يعدّل الأصناف اللي اختلفت بس + قيد فروقات من غير خزنة).
* حد الائتمان على العميل في الكاشير.
* أمر الشراء: من النواقص ← استلام بفاتورة شراء ← الأمر يتقفل ويترَبط.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse

from inventory.models import (
    Customer, Inventory, InventoryMovement, PurchaseInvoice, PurchaseOrder, SaleInvoice,
)
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_inventory, make_product, make_treasury, make_vendor,
)


@override_settings(ALLOWED_HOSTS=['*'])
class PartsCyclesTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.till = make_treasury(self.branch, balance='5000.00')
        self.oil = make_product(part_number='PC-OIL', name='زيت', retail_price='200.00', average_cost='100.00',
                                min_stock_level=5)
        self.flt = make_product(part_number='PC-FLT', name='فلتر', retail_price='80.00', average_cost='40.00',
                                min_stock_level=2)
        make_inventory(self.oil, self.branch, quantity=10)
        make_inventory(self.flt, self.branch, quantity=10)
        self.boss = User.objects.create_user('pc_boss', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.boss)

    def qty(self, product):
        return Inventory.objects.get(product=product, branch=self.branch).quantity

    def post_json(self, url, data):
        return self.c.post(url, json.dumps(data), content_type='application/json',
                           HTTP_ACCEPT='application/json', HTTP_HOST=self.host)

    # ── 📋 الجرد ─────────────────────────────────────────────────────────
    def test_stock_count_page_lists_branch_stock(self):
        r = self.c.get(reverse('inventory:stock_count'), {'branch': self.branch.pk}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'PC-OIL')

    def test_stock_count_adjusts_only_changed_rows_without_touching_the_till(self):
        r = self.c.post(reverse('inventory:stock_count'), {
            'branch': self.branch.pk,
            f'count_{self.oil.pk}': '7',     # عجز 3
            f'count_{self.flt.pk}': '',      # ماتعدّش
        }, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.qty(self.oil), 7)
        self.assertEqual(self.qty(self.flt), 10)
        res = r.context['result']
        self.assertEqual(res['short_qty'], 3)
        self.assertEqual(res['short_value'], Decimal('300.00'))
        self.assertEqual(InventoryMovement.objects.filter(product=self.oil, reference_type='CycleCount').count(), 1)
        self.till.refresh_from_db()
        self.assertEqual(self.till.balance, Decimal('5000.00'))

    def test_stock_count_rejects_negative_and_text(self):
        r = self.c.post(reverse('inventory:stock_count'), {
            'branch': self.branch.pk, f'count_{self.oil.pk}': '-1', f'count_{self.flt.pk}': 'abc',
        }, HTTP_HOST=self.host)
        self.assertEqual(len(r.context['errors']), 2)
        self.assertEqual(self.qty(self.oil), 10)
        self.assertEqual(self.qty(self.flt), 10)

    def test_cashier_cannot_open_stock_count(self):
        from .factories import make_employee
        cashier, _profile = make_employee("pc_cashier", role="cashier", branch=self.branch)
        c = Client()
        c.force_login(cashier)
        r = c.get(reverse('inventory:stock_count'), HTTP_HOST=self.host)
        self.assertNotEqual(r.status_code, 200)

    # ── 💳 حد الائتمان ───────────────────────────────────────────────────
    def _credit_sale(self, customer, paid):
        return self.post_json('/system/lightning-pos/checkout/', {
            'branch_id': self.branch.pk, 'customer_id': customer.pk,
            'items': [{'product_id': self.oil.pk, 'qty': 1, 'price': 200}],
            'treasury_id': self.till.pk, 'paid_amount': paid})

    def test_credit_sale_over_the_limit_is_refused_and_rolled_back(self):
        cust = make_customer('شركة النور', phone='01055556666', credit_limit=Decimal('150.00'))
        before = SaleInvoice.objects.count()
        r = self._credit_sale(cust, 0)        # آجل 200 > الحد 150
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn('حد الائتمان', r.json()['error'])
        self.assertEqual(SaleInvoice.objects.count(), before)
        self.assertEqual(self.qty(self.oil), 10)
        cust.refresh_from_db()
        self.assertEqual(cust.balance, Decimal('0'))

    def test_credit_sale_within_the_limit_passes(self):
        cust = make_customer('شركة النور', phone='01055556666', credit_limit=Decimal('150.00'))
        r = self._credit_sale(cust, 100)      # آجل 100 ≤ 150
        self.assertEqual(r.status_code, 200, r.content)
        cust.refresh_from_db()
        self.assertEqual(cust.balance, Decimal('100.00'))
        r = self._credit_sale(cust, 100)      # 100 + 100 = 200 > 150
        self.assertEqual(r.status_code, 400)
        cust.refresh_from_db()
        self.assertEqual(cust.balance, Decimal('100.00'))

    def test_zero_limit_means_no_limit(self):
        cust = make_customer('عميل من غير حد', phone='01055557777')
        self.assertEqual(self._credit_sale(cust, 0).status_code, 200)

    # ── 📝 أمر الشراء ────────────────────────────────────────────────────
    def test_po_from_shortages_then_receive_closes_it(self):
        Inventory.objects.filter(product=self.oil, branch=self.branch).update(quantity=1)   # تحت حد 5
        vendor = make_vendor()
        r = self.c.get(reverse('inventory:purchase_order_list'), {'branch': self.branch.pk}, HTTP_HOST=self.host)
        sugg = r.context['suggestions']
        self.assertEqual([s['product'].pk for s in sugg], [self.oil.pk])
        self.assertEqual(sugg[0]['qty'], 9)                                                   # 5×2 − 1

        r = self.c.post(reverse('inventory:purchase_order_create'), {
            'vendor': vendor.pk, 'branch': self.branch.pk,
            f'pick_{self.oil.pk}': 'on', f'qty_{self.oil.pk}': '9', f'cost_{self.oil.pk}': '95',
        }, HTTP_HOST=self.host)
        po = PurchaseOrder.objects.get()
        self.assertRedirects(r, reverse('inventory:purchase_order_detail', args=[po.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(po.status, 'draft')
        self.assertEqual(self.qty(self.oil), 1)                     # الأمر مالوش أثر على المخزون

        self.c.post(reverse('inventory:purchase_order_action', args=[po.pk, 'send']), HTTP_HOST=self.host)
        po.refresh_from_db()
        self.assertEqual(po.status, 'sent')

        r = self.c.get(reverse('inventory:purchase_create'), {'po': po.pk}, HTTP_HOST=self.host)
        self.assertContains(r, 'PC-OIL')
        self.assertIsNotNone(r.context['po_json'])

        r = self.post_json(reverse('inventory:purchase_save'), {
            'po_id': po.pk, 'vendor_id': vendor.pk, 'branch_id': self.branch.pk,
            'treasury_id': None, 'paid_amount': 0, 'cost_allocation': 'value',
            'items': [{'product_id': self.oil.pk, 'qty': 8, 'cost': 95}], 'extra_costs': []})
        self.assertEqual(r.status_code, 200, r.content)
        po.refresh_from_db()
        self.assertEqual(po.status, 'received')
        self.assertEqual(po.purchase_invoice, PurchaseInvoice.objects.get(pk=r.json()['invoice_id']))
        self.assertEqual(self.qty(self.oil), 9)                     # 1 + 8 وصلت فعلاً

        # نفس الأمر مايتستلمش مرتين
        r = self.post_json(reverse('inventory:purchase_save'), {
            'po_id': po.pk, 'vendor_id': vendor.pk, 'branch_id': self.branch.pk,
            'paid_amount': 0, 'cost_allocation': 'value',
            'items': [{'product_id': self.oil.pk, 'qty': 8, 'cost': 95}], 'extra_costs': []})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.qty(self.oil), 9)
        self.assertEqual(PurchaseInvoice.objects.count(), 1)

    def test_cancelled_po_cannot_be_received_or_sent(self):
        vendor = make_vendor()
        po = PurchaseOrder.objects.create(vendor=vendor, branch=self.branch)
        self.c.post(reverse('inventory:purchase_order_action', args=[po.pk, 'cancel']), HTTP_HOST=self.host)
        po.refresh_from_db()
        self.assertEqual(po.status, 'cancelled')
        r = self.c.post(reverse('inventory:purchase_order_action', args=[po.pk, 'send']), HTTP_HOST=self.host)
        self.assertIn('err=state', r['Location'])
        r = self.c.get(reverse('inventory:purchase_create'), {'po': po.pk}, HTTP_HOST=self.host)
        self.assertIsNone(r.context['po_json'])

    def test_po_create_validates_input(self):
        vendor = make_vendor()
        url = reverse('inventory:purchase_order_create')
        r = self.c.post(url, {'branch': self.branch.pk}, HTTP_HOST=self.host)
        self.assertIn('err=vendor', r['Location'])
        r = self.c.post(url, {'vendor': vendor.pk, 'branch': self.branch.pk}, HTTP_HOST=self.host)
        self.assertIn('err=empty', r['Location'])
        r = self.c.post(url, {'vendor': vendor.pk, 'branch': self.branch.pk, f'pick_{self.oil.pk}': 'on',
                              f'qty_{self.oil.pk}': '0', f'cost_{self.oil.pk}': '5'}, HTTP_HOST=self.host)
        self.assertIn('err=number', r['Location'])
        self.assertFalse(PurchaseOrder.objects.exists())
