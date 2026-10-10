"""
📦 Grouped shipment tests — shared costs spread across several purchase invoices.

Ten invoices from different vendors come in one container; shipping/customs
(landed) and travel (expense) are entered ONCE on the shipment. The system
splits each cost across the invoices by value, re-posts every invoice, and:

* each product's average cost = supplier price + its value-share of landed costs;
* travel never touches product cost;
* the treasury is debited exactly the cost total; the ledger still balances;
* re-saving with new amounts replaces the old shares (no double counting);
* an invoice with stock already sold can't join (nothing changes);
* deleting the shipment restores the plain supplier costs.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import (
    PurchaseInvoiceExtraCost, PurchaseShipment, Inventory, Product,
)
from inventory.services.shipment_service import allocate
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_vendor, make_treasury, make_product, make_inventory,
    make_purchase_invoice,
)
from .test_landed_cost import ledger_is_balanced

D = lambda v: Decimal(str(v))  # noqa: E731


class AllocateTests(ERPTenantTestCase):
    def test_allocate_sums_exactly_and_never_negative(self):
        shares = allocate(D('100.00'), [D('1'), D('1'), D('1')])
        self.assertEqual(sum(shares), D('100.00'))
        self.assertTrue(all(s >= 0 for s in shares))
        self.assertEqual(allocate(D('50'), [D('0'), D('0')]), [D('0.00'), D('0.00')])


@override_settings(ALLOWED_HOSTS=['*'])
class GroupedShipmentTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.treasury = make_treasury(self.branch, balance='50000.00')
        self.p1 = make_product(part_number='SH-1', name='موتور e90', average_cost='0', purchase_price='0')
        self.p2 = make_product(part_number='SH-2', name='جيربوكس', average_cost='0', purchase_price='0')
        make_inventory(self.p1, self.branch, quantity=0)
        make_inventory(self.p2, self.branch, quantity=0)
        # فاتورتين من موردين مختلفين: 30,000 و 10,000 (المجموع 40,000)
        self.inv1 = make_purchase_invoice(make_vendor(name='مورد 1'), self.branch,
                                          items=[(self.p1, 2, '15000.00')], status='draft')
        self.inv2 = make_purchase_invoice(make_vendor(name='مورد 2', phone='0111'), self.branch,
                                          items=[(self.p2, 1, '10000.00')], status='draft')
        for inv in (self.inv1, self.inv2):
            inv.status = 'posted'
            inv.save()
            inv.refresh_from_db()
        self.boss = User.objects.create_user('ship_boss', password='x', is_staff=True, is_superuser=True)

    def _save(self, **body):
        c = Client()
        c.force_login(self.boss)
        data = {'name': 'شحنة الإمارات', 'branch_id': self.branch.pk,
                'invoice_ids': [self.inv1.pk, self.inv2.pk],
                'costs': [{'kind': 'shipping', 'amount': '3000', 'treasury_id': self.treasury.pk},
                          {'kind': 'customs', 'amount': '1000'},
                          {'kind': 'travel', 'amount': '800', 'treasury_id': self.treasury.pk}]}
        data.update(body)
        return c.post('/system/purchases/shipments/save/', data=json.dumps(data),
                      content_type='application/json', HTTP_ACCEPT='application/json',
                      HTTP_HOST=self.host)

    def test_costs_spread_by_value_across_invoices(self):
        r = self._save()
        self.assertEqual(r.status_code, 200, r.content)
        # landed = 4000 على 40,000 = 10% → inv1 3000، inv2 1000
        self.p1.refresh_from_db()
        self.p2.refresh_from_db()
        self.assertEqual(self.p1.average_cost, D('16500.00'))   # 15000 + 10%
        self.assertEqual(self.p2.average_cost, D('11000.00'))   # 10000 + 10%
        self.assertEqual(self.p1.purchase_price, D('15000.00'))  # سعر المورد الخام
        self.assertEqual(Inventory.objects.get(product=self.p1, branch=self.branch).quantity, 2)
        # الخزنة اتخصم منها الشحن + السفر بس (الجمارك آجل)
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('50000.00') - D('3800.00'))
        shares = PurchaseInvoiceExtraCost.objects.filter(shipment__isnull=False)
        self.assertEqual(sum(s.amount for s in shares), D('4800.00'))
        self.assertTrue(ledger_is_balanced())

        # إعادة الحفظ بمبالغ جديدة بتستبدل الأنصبة القديمة (مفيش ازدواج)
        sh = PurchaseShipment.objects.get()
        r = self._save(id=sh.pk, costs=[{'kind': 'shipping', 'amount': '2000',
                                          'treasury_id': self.treasury.pk}])
        self.assertEqual(r.status_code, 200, r.content)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.average_cost, D('15750.00'))   # 15000 + 5%
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('48000.00'))
        self.assertTrue(ledger_is_balanced())

        # حذف الشحنة بيرجّع التكلفة لسعر المورد
        c = Client()
        c.force_login(self.boss)
        c.post(f'/system/purchases/shipments/{sh.pk}/delete/', HTTP_HOST=self.host)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.average_cost, D('15000.00'))
        self.assertFalse(PurchaseShipment.objects.exists())
        self.inv1.refresh_from_db()
        self.assertIsNone(self.inv1.shipment_id)
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('50000.00'))

    def test_invoice_with_sold_stock_cannot_join(self):
        Inventory.objects.filter(product=self.p2, branch=self.branch).update(quantity=0)  # اتباع
        r = self._save()
        self.assertEqual(r.status_code, 409)
        self.assertFalse(PurchaseShipment.objects.exists())
        self.assertFalse(PurchaseInvoiceExtraCost.objects.exists())
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.average_cost, D('15000.00'))

    def test_invoice_in_shipment_cannot_be_deleted_alone(self):
        self.assertEqual(self._save().status_code, 200)
        c = Client()
        c.force_login(self.boss)
        r = c.post(f'/system/purchases/{self.inv1.pk}/delete/', HTTP_HOST=self.host)
        self.assertIn('err=shipment', r['Location'])
        self.inv1.refresh_from_db()
        self.assertTrue(self.inv1.is_applied)

    def test_pick_invoices_from_purchase_list(self):
        c = Client()
        c.force_login(self.boss)
        page = c.get('/system/purchases/', HTTP_HOST=self.host)
        self.assertContains(page, f'class="sel-inv" value="{self.inv1.pk}"')
        self.assertContains(page, f'class="sel-inv" value="{self.inv2.pk}"')
        form = c.get(f'/system/purchases/shipments/new/?invoices={self.inv1.pk},{self.inv2.pk},x',
                     HTTP_HOST=self.host)
        self.assertEqual(form.status_code, 200)
        self.assertContains(form, f'const PRESELECT = [{self.inv1.pk}, {self.inv2.pk}];')
        # فاتورة دخلت شحنة مبقاش ليها checkbox في القائمة
        self.assertEqual(self._save().status_code, 200)
        page = c.get('/system/purchases/', HTTP_HOST=self.host)
        self.assertNotContains(page, f'class="sel-inv" value="{self.inv1.pk}"')
