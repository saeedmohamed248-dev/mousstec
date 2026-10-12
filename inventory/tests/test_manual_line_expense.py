"""
✍️ Set (or lower) the expense of one or two lines by hand; the rest of the
invoice's expenses spread over the other lines automatically.

On «إعادة توزيع التكلفة» each line has «مصاريف القطعة» (empty = automatic).
A typed value is kept on the line, survives editing the invoice, and is
dropped back to automatic only if the invoice's expenses no longer cover it.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, SimpleTestCase, override_settings

from inventory.models import Product, PurchaseInvoice
from inventory.services.landed_allocation import MARGIN, VALUE, allocate, manual_problem
from .base import ERPTenantTestCase
from .factories import make_branch, make_product, make_vendor
from .test_landed_cost import ledger_is_balanced

D = lambda v: Decimal(str(v))  # noqa: E731
LINES = [(1, '4000', '5000'), (1, '4000', '4500'), (2, '425', '400')]


class ManualAllocationTests(SimpleTestCase):
    def test_fixed_line_takes_its_amount_and_the_rest_spreads(self):
        shares, used = allocate(LINES, '300', MARGIN, {0: '50'})
        self.assertEqual((used, shares), (MARGIN, [D('50.00'), D('250.00'), D('0.00')]))
        shares, used = allocate(LINES, '300', VALUE, {0: '50'})
        self.assertEqual(shares, [D('50.00'), D('206.19'), D('43.81')])
        shares, _ = allocate(LINES, '300', VALUE, {2: '0'})          # الباكته من غير مصاريف
        self.assertEqual(shares, [D('150.00'), D('150.00'), D('0.00')])

    def test_inconsistent_manual_values(self):
        self.assertIn('أكتر من إجمالي', manual_problem(LINES, '300', {0: '301'}))
        self.assertIn('لازم يساوي', manual_problem(LINES, '300', {0: '100', 1: '100', 2: '10'}))
        self.assertIsNone(manual_problem(LINES, '300', {0: '100', 1: '100', 2: '50'}))
        self.assertIn('بالسالب', manual_problem(LINES, '300', {0: '-1'}))
        # مش متّسق ⇒ التوزيع بيتجاهله
        self.assertEqual(allocate(LINES, '300', MARGIN, {0: '301'})[0], [D('200.00'), D('100.00'), D('0.00')])


@override_settings(ALLOWED_HOSTS=['*'])
class ManualLineExpenseTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.vendor = make_vendor(name='FIXIT UAE')
        self.e60 = make_product(part_number='ML-1', name='اسكتال e60', retail_price='5000',
                                average_cost='0', purchase_price='0')
        self.r56 = make_product(part_number='ML-2', name='اسكتال r56', retail_price='4500',
                                average_cost='0', purchase_price='0')
        self.pad = make_product(part_number='ML-3', name='باكته', retail_price='400',
                                average_cost='0', purchase_price='0')
        self.c = Client()
        self.c.force_login(User.objects.create_user('ml_boss', password='x', is_staff=True, is_superuser=True))

    def _save(self, items=None, **extra):
        body = {'vendor_id': self.vendor.pk, 'branch_id': self.branch.pk, 'treasury_id': None, 'paid_amount': 0,
                'cost_allocation': 'margin',
                'items': items or [{'product_id': self.e60.pk, 'qty': 1, 'cost': 4000},
                                   {'product_id': self.r56.pk, 'qty': 1, 'cost': 4000},
                                   {'product_id': self.pad.pk, 'qty': 2, 'cost': 425}],
                'extra_costs': [{'kind': 'shipping', 'behavior': 'landed', 'amount': 300}]}
        body.update(extra)
        r = self.c.post('/system/purchases/save/', json.dumps(body), content_type='application/json',
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        return PurchaseInvoice.objects.get(pk=r.json()['invoice_id'])

    def avgs(self):
        return tuple(Product.objects.get(pk=p.pk).average_cost for p in (self.e60, self.r56, self.pad))

    def recost(self, inv, **extras):
        items = list(inv.items.order_by('id'))
        data = {'method': 'margin'}
        for it, key in zip(items, ('e60', 'r56', 'pad')):
            data[f'extra_{it.pk}'] = extras.get(key, '')
        return self.c.post(f'/system/purchases/{inv.pk}/recost/', data, HTTP_HOST=self.host)

    def test_lower_one_line_by_hand(self):
        inv = self._save()
        self.assertEqual(self.avgs(), (D('4200.00'), D('4100.00'), D('425.00')))
        r = self.recost(inv, e60='50')                      # e60: 200 ⇒ 50 بإيدي
        self.assertIn('ok=recosted', r['Location'])
        self.assertEqual(self.avgs(), (D('4050.00'), D('4250.00'), D('425.00')))   # الباقي 250 على r56
        e60_line = inv.items.get(product=self.e60)
        self.assertEqual((e60_line.manual_extra_unit, e60_line.landed_unit_cost), (D('50.00'), D('4050.00')))
        self.assertIsNone(inv.items.get(product=self.r56).manual_extra_unit)
        self.assertTrue(ledger_is_balanced())
        page = self.c.get(f'/system/purchases/{inv.pk}/recost/', HTTP_HOST=self.host).content.decode()
        self.assertIn(f'name="extra_{e60_line.pk}" type="number" min="0" step="0.01" value="50.00"', page)

        # التعديل بيحافظ على اليدوي (صفحة التعديل بتبعته تاني)
        edit = self.c.get(f'/system/purchases/{inv.pk}/edit/', HTTP_HOST=self.host).content.decode()
        self.assertIn('"extra_unit": 50.0', edit)
        self._save(invoice_id=inv.pk, items=[
            {'product_id': self.e60.pk, 'qty': 1, 'cost': 4000, 'extra_unit': 50},
            {'product_id': self.r56.pk, 'qty': 1, 'cost': 4000, 'extra_unit': None},
            {'product_id': self.pad.pk, 'qty': 2, 'cost': 425}])
        self.assertEqual(self.avgs(), (D('4050.00'), D('4250.00'), D('425.00')))

        # المصاريف قلّت عن اليدوي ⇒ يرجع تلقائي
        self._save(invoice_id=inv.pk, items=[
            {'product_id': self.e60.pk, 'qty': 1, 'cost': 4000, 'extra_unit': 50},
            {'product_id': self.r56.pk, 'qty': 1, 'cost': 4000},
            {'product_id': self.pad.pk, 'qty': 2, 'cost': 425}],
            extra_costs=[{'kind': 'shipping', 'behavior': 'landed', 'amount': 30}])
        self.assertIsNone(inv.items.get(product=self.e60).manual_extra_unit)
        self.assertEqual(self.avgs(), (D('4020.00'), D('4010.00'), D('425.00')))

    def test_two_lines_by_hand_and_back_to_auto(self):
        inv = self._save()
        self.recost(inv, e60='100', pad='25')               # 100 + 2×25 = 150، الباقي 150 على r56
        self.assertEqual(self.avgs(), (D('4100.00'), D('4150.00'), D('450.00')))
        self.recost(inv)                                    # الكل تلقائي تاني
        self.assertEqual(self.avgs(), (D('4200.00'), D('4100.00'), D('425.00')))
        self.assertFalse(inv.items.filter(manual_extra_unit__isnull=False).exists())
        self.assertTrue(ledger_is_balanced())

    def test_manual_more_than_the_expenses_is_refused(self):
        inv = self._save()
        r = self.recost(inv, e60='400')
        self.assertEqual(r.status_code, 200)
        self.assertIn('أكتر من إجمالي المصاريف', r.content.decode())
        self.assertEqual(self.avgs(), (D('4200.00'), D('4100.00'), D('425.00')))
        self.assertFalse(inv.items.filter(manual_extra_unit__isnull=False).exists())
