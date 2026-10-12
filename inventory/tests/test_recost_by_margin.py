"""
🔁 Purchase expenses spread by each item's profit, not one fixed percentage.

Landed expenses (customs/shipping/…) used to be spread by purchase value, so
every item got the same uplift (a 49 % shipment made a 425-cost part with a
600 sell price look like a loss). Now an invoice can spread them by margin —
(sell − purchase) × qty: an item earning 1,000 a piece carries twice what an
item earning 500 carries, and an item that earns nothing carries nothing.
Posted invoices can be re-spread from «إعادة توزيع التكلفة» (sell prices
editable there) without touching quantities, the supplier or the ledger.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, SimpleTestCase, override_settings

from inventory.models import AccountingEntry, Inventory, Product, PurchaseInvoice, Vendor
from inventory.services.landed_allocation import MARGIN, VALUE, allocate, landed_units
from .base import ERPTenantTestCase
from .factories import make_branch, make_product, make_vendor
from .test_landed_cost import ledger_is_balanced

D = lambda v: Decimal(str(v))  # noqa: E731


class AllocationTests(SimpleTestCase):
    LINES = [(1, '4000', '5000'),   # بيكسب 1000
             (1, '4000', '4500'),   # بيكسب 500
             (2, '425', '400')]     # بيخسر — مابيشيلش مصاريف

    def test_margin_follows_each_items_profit(self):
        shares, used = allocate(self.LINES, '300', MARGIN)
        self.assertEqual((used, shares), (MARGIN, [D('200.00'), D('100.00'), D('0.00')]))
        units, _ = landed_units(self.LINES, '300', MARGIN)
        self.assertEqual(units, [D('4200.00'), D('4100.00'), D('425.00')])

    def test_value_is_one_fixed_percentage(self):
        shares, used = allocate(self.LINES, '885', VALUE)
        self.assertEqual(used, VALUE)
        self.assertEqual(shares, [D('400.00'), D('400.00'), D('85.00')])   # 10 % على كل صنف

    def test_no_profit_anywhere_falls_back_to_value_and_totals_are_exact(self):
        shares, used = allocate([(3, '100', '0'), (7, '33.33', '20')], '100', MARGIN)
        self.assertEqual(used, VALUE)
        self.assertEqual(sum(shares), D('100'))
        shares, _ = allocate([(3, '10', '20'), (7, '10', '11'), (1, '5', '9')], '1000', MARGIN)
        self.assertEqual(sum(shares), D('1000'))


@override_settings(ALLOWED_HOSTS=['*'])
class RecostByMarginTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.vendor = make_vendor(name='FIXIT UAE')
        self.e60 = make_product(part_number='RM-1', name='اسكتال e60', retail_price='5000',
                                average_cost='0', purchase_price='0')
        self.r56 = make_product(part_number='RM-2', name='اسكتال r56', retail_price='4500',
                                average_cost='0', purchase_price='0')
        self.pad = make_product(part_number='RM-3', name='باكته', retail_price='400',
                                average_cost='0', purchase_price='0')
        self.c = Client()
        self.c.force_login(User.objects.create_user('rm_boss', password='x', is_staff=True, is_superuser=True))

    def _save(self, **extra):
        body = {'vendor_id': self.vendor.pk, 'branch_id': self.branch.pk, 'treasury_id': None, 'paid_amount': 0,
                'items': [{'product_id': self.e60.pk, 'qty': 1, 'cost': 4000},
                          {'product_id': self.r56.pk, 'qty': 1, 'cost': 4000},
                          {'product_id': self.pad.pk, 'qty': 2, 'cost': 425}],
                'extra_costs': [{'kind': 'shipping', 'behavior': 'landed', 'amount': 300}]}
        body.update(extra)
        r = self.c.post('/system/purchases/save/', json.dumps(body), content_type='application/json',
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        return PurchaseInvoice.objects.get(pk=r.json()['invoice_id'])

    def avg(self, p):
        return Product.objects.get(pk=p.pk).average_cost

    def stock_value(self):
        return sum((Product.objects.get(pk=p.pk).average_cost
                    * sum(Inventory.objects.filter(product=p).values_list('quantity', flat=True)))
                   for p in (self.e60, self.r56, self.pad))

    def test_posting_by_margin(self):
        inv = self._save(cost_allocation='margin')
        self.assertEqual(inv.cost_allocation, 'margin')
        self.assertEqual((self.avg(self.e60), self.avg(self.r56), self.avg(self.pad)),
                         (D('4200.00'), D('4100.00'), D('425.00')))
        self.assertEqual([it.landed_unit_cost for it in inv.items.order_by('id')],
                         [D('4200.00'), D('4100.00'), D('425.00')])
        self.assertTrue(ledger_is_balanced())

    def test_recost_a_posted_invoice_from_value_to_margin(self):
        inv = self._save()
        self.assertEqual((self.avg(self.e60), self.avg(self.pad)), (D('4135.59'), D('439.41')))
        value_before = self.stock_value()
        entries_before = AccountingEntry.objects.count()

        page = self.c.get(f'/system/purchases/{inv.pk}/recost/', HTTP_HOST=self.host).content.decode()
        self.assertIn('حسب ربح كل صنف', page)
        self.assertIn('name="sell_%d"' % self.e60.pk, page)

        r = self.c.post(f'/system/purchases/{inv.pk}/recost/',
                        {'method': 'margin', f'sell_{self.e60.pk}': '5000', f'sell_{self.r56.pk}': '4500',
                         f'sell_{self.pad.pk}': '400'}, HTTP_HOST=self.host)
        self.assertIn('ok=recosted', r['Location'])
        self.assertEqual((self.avg(self.e60), self.avg(self.r56), self.avg(self.pad)),
                         (D('4200.00'), D('4100.00'), D('425.00')))
        inv.refresh_from_db()
        self.assertEqual(inv.cost_allocation, 'margin')
        # الكميات والمورد والقيود زي ما هي — بس التوزيع اتغيّر
        self.assertEqual(sum(Inventory.objects.filter(product=self.pad).values_list('quantity', flat=True)), 2)
        self.assertEqual(Vendor.objects.get(pk=self.vendor.pk).balance, D('8850.00'))
        self.assertEqual(AccountingEntry.objects.count(), entries_before)
        self.assertLess(abs(self.stock_value() - value_before), D('0.05'))
        self.assertTrue(ledger_is_balanced())

        # تعديل الفاتورة بعد كده بيعكسها بالتكلفة الجديدة ويفضل «حسب الربح»
        self._save(invoice_id=inv.pk)
        inv.refresh_from_db()
        self.assertEqual(inv.cost_allocation, 'margin')
        self.assertEqual((self.avg(self.e60), self.avg(self.pad)), (D('4200.00'), D('425.00')))

    def test_sell_price_edited_on_the_page_drives_the_split(self):
        inv = self._save()
        other = make_product(part_number='RM-9', name='صنف تاني', retail_price='100')
        r = self.c.post(f'/system/purchases/{inv.pk}/recost/',
                        {'method': 'margin', f'sell_{self.r56.pk}': '5000', f'sell_{self.pad.pk}': '425',
                         f'sell_{other.pk}': '1', 'sell_999999': '5'}, HTTP_HOST=self.host)
        self.assertIn('ok=recosted', r['Location'])
        self.assertEqual(Product.objects.get(pk=self.r56.pk).retail_price, D('5000.00'))
        self.assertEqual(Product.objects.get(pk=other.pk).retail_price, D('100.00'))  # مش من الفاتورة
        # e60 و r56 بيكسبوا 1000 الاتنين ⇒ 150 / 150، والباكته مابتكسبش ⇒ 0
        self.assertEqual((self.avg(self.e60), self.avg(self.r56), self.avg(self.pad)),
                         (D('4150.00'), D('4150.00'), D('425.00')))

    def test_refused_when_items_were_already_sold(self):
        inv = self._save()
        Inventory.objects.filter(product=self.pad).update(quantity=1)
        r = self.c.post(f'/system/purchases/{inv.pk}/recost/', {'method': 'margin'}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertIn('اتباع من أصناف الفاتورة دي', r.content.decode())
        self.assertEqual(self.avg(self.e60), D('4135.59'))
        inv.refresh_from_db()
        self.assertEqual(inv.cost_allocation, 'value')

    def test_purchase_page_offers_the_choice(self):
        page = self.c.get('/system/purchases/new/', HTTP_HOST=self.host).content.decode()
        self.assertIn('data-alloc="margin"', page)
        inv = self._save(cost_allocation='margin')
        page = self.c.get(f'/system/purchases/{inv.pk}/edit/', HTTP_HOST=self.host).content.decode()
        self.assertIn('"cost_allocation": "margin"', page)
        lst = self.c.get('/system/purchases/', HTTP_HOST=self.host).content.decode()
        self.assertIn(f'/system/purchases/{inv.pk}/recost/', lst)
