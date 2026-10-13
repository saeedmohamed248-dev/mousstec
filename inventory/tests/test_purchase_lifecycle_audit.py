"""
🔍 Full purchase → stock → sale lifecycle, checked after every step.

One FIXIT-style flow through the real screens: receive an invoice with landed
and period expenses, add it to a grouped shipment, edit it, re-spread its
expenses by profit, set one line by hand, pay part of it, return a piece to the
supplier, sell, take a sale return, pay the supplier on account.

After each step the books must agree with each other:
* stock rows = the sum of their movements;
* stock value (Σ average cost × qty) = the ledger's inventory account;
* every supplier balance = what its invoices and payments say;
* every treasury balance = opening + its receipts − its payments;
* the ledger balances and no purchase is left half-posted.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import Inventory, PurchaseInvoice, Treasury
from .base import ERPTenantTestCase
from .factories import make_branch, make_product, make_treasury, make_vendor

D = lambda v: Decimal(str(v))  # noqa: E731


def books_problems(opening):
    """قايمة بكل اللي مش متّسق في الدفاتر (فاضية = كله مظبوط) — نفس فحص audit_books."""
    from inventory.services.books_audit import books_problems as audit
    return audit(opening, value_tolerance=D('0.10'))


@override_settings(ALLOWED_HOSTS=['*'])
class PurchaseLifecycleAuditTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.cash = make_treasury(self.branch, balance='100000.00')
        self.opening = {self.cash.pk: D('100000.00')}
        self.vendor = make_vendor(name='FIXIT UAE')
        self.e60 = make_product(part_number='LA-1', name='اسكتال e60', retail_price='7500',
                                b2b_wholesale_price='6000', average_cost='0', purchase_price='0')
        self.r56 = make_product(part_number='LA-2', name='اسكتال r56', retail_price='8500',
                                average_cost='0', purchase_price='0')
        self.pad = make_product(part_number='LA-3', name='باكته', retail_price='600',
                                average_cost='0', purchase_price='0')
        self.c = Client()
        self.c.force_login(User.objects.create_user('la_boss', password='x', is_staff=True, is_superuser=True))

    def ok(self, step):
        self.assertEqual(books_problems(self.opening), [], f"after: {step}")

    def save(self, **extra):
        body = {'vendor_id': self.vendor.pk, 'branch_id': self.branch.pk,
                'treasury_id': self.cash.pk, 'paid_amount': 5000,
                'items': [{'product_id': self.e60.pk, 'qty': 4, 'cost': 4400},
                          {'product_id': self.r56.pk, 'qty': 3, 'cost': 5000},
                          {'product_id': self.pad.pk, 'qty': 10, 'cost': 425}],
                'extra_costs': [{'kind': 'shipping', 'behavior': 'landed', 'amount': 2000},
                                {'kind': 'travel', 'behavior': 'expense', 'amount': 500,
                                 'treasury_id': self.cash.pk}]}
        body.update(extra)
        r = self.c.post('/system/purchases/save/', json.dumps(body), content_type='application/json',
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        return PurchaseInvoice.objects.get(pk=r.json()['invoice_id'])

    def test_whole_cycle_keeps_the_books_together(self):
        inv = self.save()
        self.ok('receive')

        r = self.c.post('/system/purchases/shipments/save/', json.dumps({
            'name': 'شحنة 1', 'branch_id': self.branch.pk, 'invoice_ids': [inv.pk],
            'costs': [{'kind': 'customs', 'amount': '3000', 'treasury_id': self.cash.pk},
                      {'kind': 'food', 'amount': '300'}]}),
            content_type='application/json', HTTP_ACCEPT='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        self.ok('grouped shipment')

        self.save(invoice_id=inv.pk, items=[{'product_id': self.e60.pk, 'qty': 4, 'cost': 4400},
                                            {'product_id': self.r56.pk, 'qty': 4, 'cost': 5000},
                                            {'product_id': self.pad.pk, 'qty': 10, 'cost': 425}])
        self.ok('edit')

        lines = list(inv.items.order_by('id'))
        r = self.c.post(f'/system/purchases/{inv.pk}/recost/', {'method': 'margin'}, HTTP_HOST=self.host)
        self.assertIn('ok=recosted', r['Location'])
        self.ok('recost by margin')
        r = self.c.post(f'/system/purchases/{inv.pk}/recost/',
                        {'method': 'margin', f'extra_{lines[0].pk}': '100'}, HTTP_HOST=self.host)
        self.assertIn('ok=recosted', r['Location'])
        self.ok('manual line expense')

        r = self.c.post(f'/system/purchases/{inv.pk}/pay/',
                        {'amount': '2000', 'treasury_id': self.cash.pk, 'next': '/system/purchases/'},
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        self.ok('partial payment')

        pad_line = inv.items.get(product=self.pad)
        r = self.c.post(f'/system/purchases/{inv.pk}/return/',
                        {f'qty_{pad_line.pk}': '1', 'refund_amount': '0', 'treasury_id': ''},
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302, r.content[:400])
        self.ok('return to supplier')

        r = self.c.post('/system/lightning-pos/checkout/', data=json.dumps({
            'branch_id': self.branch.pk, 'customer_name': 'عميل', 'customer_phone': '01011112222',
            'payments': [{'treasury_id': self.cash.pk, 'amount': 15000}],
            'items': [{'product_id': self.e60.pk, 'qty': 2, 'price': 7500, 'discount': 0}]}),
            content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        self.ok('POS sale')

        from inventory.models import SaleInvoice
        from inventory.services.invoice_service import InvoiceService
        sale = SaleInvoice.objects.filter(is_return=False).latest('id')
        ret = InvoiceService.create_return_invoice(sale)
        ret.status = 'posted'
        ret.save()
        self.ok('sale return')

        r = self.c.post(f'/system/vendors/{self.vendor.pk}/pay/',
                        {'amount': '1000', 'treasury_id': self.cash.pk}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        self.ok('pay supplier on account')

    def test_shipment_resave_and_delete_after_recost(self):
        inv = self.save()
        ship = lambda **b: self.c.post('/system/purchases/shipments/save/', json.dumps(dict({  # noqa: E731
            'name': 'شحنة 1', 'branch_id': self.branch.pk, 'invoice_ids': [inv.pk],
            'costs': [{'kind': 'customs', 'amount': '3000', 'treasury_id': self.cash.pk}]}, **b)),
            content_type='application/json', HTTP_ACCEPT='application/json', HTTP_HOST=self.host)
        self.assertEqual(ship().status_code, 200)
        line = inv.items.order_by('id').first()
        r = self.c.post(f'/system/purchases/{inv.pk}/recost/',
                        {'method': 'margin', f'extra_{line.pk}': '250'}, HTTP_HOST=self.host)
        self.assertIn('ok=recosted', r['Location'])
        self.ok('recost inside a shipment')

        from inventory.models import PurchaseShipment
        sh = PurchaseShipment.objects.get()
        r = ship(id=sh.pk, costs=[{'kind': 'customs', 'amount': '1500', 'treasury_id': self.cash.pk}])
        self.assertEqual(r.status_code, 200, r.content)
        self.ok('shipment re-saved with smaller costs')

        r = self.c.post(f'/system/purchases/shipments/{sh.pk}/delete/', HTTP_HOST=self.host)
        self.assertIn(r.status_code, (200, 302))
        self.ok('shipment deleted')

        r = self.c.post(f'/system/purchases/{inv.pk}/delete/', HTTP_HOST=self.host)
        self.assertIn('ok=deleted', r['Location'])
        self.ok('invoice deleted')
        self.assertEqual(sum(Inventory.objects.values_list('quantity', flat=True)), 0)
        self.assertEqual(Treasury.objects.get(pk=self.cash.pk).balance, D('100000.00'))
        self.assertEqual(type(self.vendor).objects.get(pk=self.vendor.pk).balance, D('0.00'))

    def test_admin_purchase_in_a_second_branch(self):
        from unittest import mock
        with mock.patch('tenancy.signals.quota._current_tenant', return_value=None):
            branch2 = make_branch(name='فرع 2')
            cash2 = make_treasury(branch2, name='خزنة 2', balance='20000.00')
        self.opening[cash2.pk] = D('20000.00')
        inv = self.save(branch_id=branch2.pk, treasury_id=cash2.pk, paid_amount=1000,
                        extra_costs=[{'kind': 'shipping', 'behavior': 'landed', 'amount': 700,
                                      'treasury_id': cash2.pk}])
        self.assertEqual(inv.branch_id, branch2.pk)
        self.assertEqual(Treasury.objects.get(pk=cash2.pk).balance, D('18300.00'))
        self.ok('second branch')
        self.save(invoice_id=inv.pk, branch_id=branch2.pk, treasury_id=cash2.pk, paid_amount=1000,
                  extra_costs=[{'kind': 'shipping', 'behavior': 'landed', 'amount': 700,
                                'treasury_id': cash2.pk}])
        self.ok('second branch edit')
        self.assertEqual(Treasury.objects.get(pk=cash2.pk).balance, D('18300.00'))

    def test_edit_keeps_payments_made_later_from_other_treasuries(self):
        """🐛 التعديل كان بيمسح الدفعات (حتى اللي من «سداد») ويعمل دفعة واحدة من خانة المدفوع."""
        from unittest import mock
        with mock.patch('tenancy.signals.quota._current_tenant', return_value=None):
            branch2 = make_branch(name='فرع 2')
            cash2 = make_treasury(branch2, name='خزنة 2', balance='50000.00')
            cash3 = make_treasury(self.branch, name='خزنة 3', balance='50000.00')
        self.opening.update({cash2.pk: D('50000.00'), cash3.pk: D('50000.00')})
        inv = self.save(paid_amount=0, treasury_id=None, extra_costs=[])        # 36,850 آجل
        self.c.post(f'/system/vendors/{self.vendor.pk}/pay/', {'amount': '10000', 'treasury_id': cash2.pk},
                    HTTP_HOST=self.host)
        self.c.post(f'/system/purchases/{inv.pk}/pay/', {'amount': '6000', 'treasury_id': cash3.pk,
                                                         'next': '/system/purchases/'}, HTTP_HOST=self.host)
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, D('16000.00'))
        self.ok('two payments')

        # تعديل من صفحة التعديل (الخانتين بتوع الدفع مش بيتبعتوا فاضيين)
        self.save(invoice_id=inv.pk, paid_amount=0, treasury_id=None, extra_costs=[])
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, D('16000.00'))
        self.assertEqual(Treasury.objects.get(pk=cash2.pk).balance, D('40000.00'))
        self.assertEqual(Treasury.objects.get(pk=cash3.pk).balance, D('44000.00'))
        self.assertEqual(type(self.vendor).objects.get(pk=self.vendor.pk).balance, D('20850.00'))
        self.assertEqual(inv.payments.count(), 2)
        self.ok('edit after payments')

        # التعديل نزّل الإجمالي تحت المدفوع ⇒ الفرق رصيد لينا عند المورد
        self.save(invoice_id=inv.pk, extra_costs=[],
                  items=[{'product_id': self.pad.pk, 'qty': 10, 'cost': 425}])
        self.assertEqual(type(self.vendor).objects.get(pk=self.vendor.pk).balance, D('-11750.00'))
        self.ok('edit below what was paid')

        # الشحنة المجمّعة بعد الدفع مابتلمسش الدفعات
        r = self.c.post('/system/purchases/shipments/save/', json.dumps({
            'name': 'شحنة', 'branch_id': self.branch.pk, 'invoice_ids': [inv.pk],
            'costs': [{'kind': 'customs', 'amount': '500'}]}),
            content_type='application/json', HTTP_ACCEPT='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Treasury.objects.get(pk=cash2.pk).balance, D('40000.00'))
        self.assertEqual(Treasury.objects.get(pk=cash3.pk).balance, D('44000.00'))
        self.ok('shipment after payments')

    def test_repair_keeps_later_payments(self):
        """فاتورة مكسورة (التعديل القديم) واتدفع عليها بعد كده من «سداد» — الإصلاح مايلمّش الدفعة."""
        from django.db import transaction
        from inventory.views_lightning import _reverse_purchase_posting
        inv = self.save(paid_amount=0, treasury_id=None, extra_costs=[])
        with transaction.atomic():
            _reverse_purchase_posting(inv)
            PurchaseInvoice.objects.filter(pk=inv.pk).update(status='posted', is_applied=True)
        self.vendor.refresh_from_db()
        self.c.post(f'/system/purchases/{inv.pk}/pay/', {'amount': '3000', 'treasury_id': self.cash.pk,
                                                         'next': '/system/purchases/'}, HTTP_HOST=self.host)
        r = self.c.post(f'/system/purchases/{inv.pk}/repair/', HTTP_HOST=self.host)
        self.assertIn('ok=repaired', r['Location'])
        self.assertEqual(Treasury.objects.get(pk=self.cash.pk).balance, D('97000.00'))
        inv.refresh_from_db()
        self.assertEqual((inv.paid_amount, inv.payments.count()), (D('3000.00'), 1))
        self.ok('repair with a later payment')
