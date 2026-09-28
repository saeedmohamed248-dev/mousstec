"""
💰 Printing money cycles — treasury, order totals, material stock, quotations.

Covers the fixes where the numbers used to drift from their source:
  * editing / bulk-deleting a treasury transaction left the balance wrong
  * the order total didn't follow its jobs' prices
  * materials were never deducted from stock nor counted as job cost
  * converting a quote made an empty order and lost discount / tax
  * the customer statement counted cancelled orders and ignored refunds
  * money reports / quotation endpoints were open (no permission / no CSRF)
"""
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, override_settings

from inventory.tests.base import ERPTenantTestCase
from printing.models import (
    PriceQuotation, PrintCustomer, PrintJob, PrintJobMaterial, PrintMaterial,
    PrintOrder, PrintTransaction, PrintTreasury, QuotationLine,
)
from printing.views.finance import convert_quotation

User = get_user_model()
D = Decimal


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingMoneyCyclesTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.customer = PrintCustomer.objects.create(name='عميل تجربة', phone='01011112222')
        self.treasury = PrintTreasury.objects.create(name='الخزنة الرئيسية')

    def _order(self, **kw):
        kw.setdefault('status', 'confirmed')
        return PrintOrder.objects.create(customer=self.customer, **kw)

    def _bal(self, treasury=None):
        return PrintTreasury.objects.get(pk=(treasury or self.treasury).pk).balance

    # ── treasury ────────────────────────────────────────────────────────
    def test_editing_transaction_moves_balance(self):
        other = PrintTreasury.objects.create(name='خزنة الفرع')
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('500'))
        out = PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('100'))
        self.assertEqual(self._bal(), D('400'))

        out.amount = D('150')
        out.save()
        self.assertEqual(self._bal(), D('350'))

        # Type flip: 150 out → 150 in.
        out.transaction_type = 'in'
        out.save()
        self.assertEqual(self._bal(), D('650'))

        # Moving it to another treasury takes it off the first.
        out.treasury = other
        out.save()
        self.assertEqual(self._bal(), D('500'))
        self.assertEqual(self._bal(other), D('150'))

    def test_bulk_delete_reverses_balance(self):
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('300'))
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('50'))
        self.assertEqual(self._bal(), D('250'))
        PrintTransaction.objects.filter(treasury=self.treasury).delete()   # admin "delete selected"
        self.assertEqual(self._bal(), D('0'))

    def test_overdraw_refused(self):
        with self.assertRaises(ValueError):
            PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('1'))
        self.assertEqual(self._bal(), D('0'))

    def test_admin_form_reports_overdraw_as_form_error(self):
        from printing.admin import PrintTransactionForm
        form = PrintTransactionForm(data={
            'treasury': self.treasury.pk, 'transaction_type': 'out', 'amount': '10',
            'description': 'x', 'date_0': '2026-01-01', 'date_1': '10:00', 'date': '2026-01-01 10:00',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('لا يكفي', str(form.errors))

    # ── order total follows its jobs ──────────────────────────────────
    def test_order_total_follows_jobs(self):
        order = self._order()
        j1 = PrintJob.objects.create(order=order, description='كروت', quantity=5, copies=2, unit_price=D('10'))
        PrintJob.objects.create(order=order, description='بنر', quantity=1, unit_price=D('40'))
        order.refresh_from_db()
        self.assertEqual(order.total_amount, D('140'))
        j1.delete()
        order.refresh_from_db()
        self.assertEqual(order.total_amount, D('40'))

    def test_order_paid_amount_from_payments_and_refunds(self):
        order = self._order(total_amount=D('200'))
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('150'), order=order)
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('30'), order=order)
        order.refresh_from_db()
        self.assertEqual(order.paid_amount, D('120'))
        self.assertEqual(order.remaining, D('80'))

    def test_delivered_sets_date(self):
        order = self._order()
        order.status = 'delivered'
        order.save(update_fields=['status'])
        order.refresh_from_db()
        self.assertIsNotNone(order.date_delivered)

    # ── material consumption ──────────────────────────────────────────
    def test_material_usage_moves_stock_and_cost(self):
        paper = PrintMaterial.objects.create(name='ورق كوشيه', quantity=D('100'), cost_per_unit=D('2'))
        order = self._order()
        job = PrintJob.objects.create(order=order, description='فلاير', quantity=1, unit_price=D('100'))

        use = PrintJobMaterial.objects.create(job=job, material=paper, quantity=D('10'))
        paper.refresh_from_db()
        self.assertEqual(paper.quantity, D('90'))
        self.assertEqual(use.unit_cost, D('2'))
        self.assertEqual(job.material_cost, D('20.00'))
        self.assertEqual(job.calculated_cost, D('20.00'))

        use.quantity = D('15')
        use.save()
        paper.refresh_from_db()
        self.assertEqual(paper.quantity, D('85'))

        with self.assertRaises(ValidationError):
            PrintJobMaterial.objects.create(job=job, material=paper, quantity=D('1000'))
        paper.refresh_from_db()
        self.assertEqual(paper.quantity, D('85'))

        # Completed job: its frozen cost follows later material changes.
        job.is_complete = True
        job.save()
        job.refresh_from_db()
        self.assertEqual(job.actual_cost, D('30.00'))
        use.delete()
        paper.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(paper.quantity, D('100'))
        self.assertEqual(job.actual_cost, D('0.00'))
        self.assertEqual(job.actual_profit, D('100.00'))

    # ── quotation → order ────────────────────────────────────────────
    def _quote(self, **kw):
        q = PriceQuotation.objects.create(title='عرض كروت', customer=self.customer, **kw)
        QuotationLine.objects.create(quotation=q, description='كروت شخصية', quantity=D('2'), unit_price=D('50'))
        QuotationLine.objects.create(quotation=q, description='فينيل', quantity=D('1.5'), unit_price=D('20'))
        q.recalc_totals()
        q.refresh_from_db()
        return q

    def test_convert_quote_carries_lines_discount_and_tax(self):
        q = self._quote(discount=D('10'), tax_percent=D('14'))
        self.assertEqual(q.total, D('136.80'))
        q.status = 'accepted'
        q.save(update_fields=['status'])

        order = convert_quotation(q)
        order.refresh_from_db()
        self.assertEqual(order.jobs.count(), 2)
        self.assertEqual(order.total_amount, D('130'))
        self.assertEqual(order.discount, D('10'))
        self.assertEqual(order.tax_amount, D('16.80'))
        self.assertEqual(order.net_total, q.total)       # what the customer owes
        self.assertEqual(order.revenue, D('120'))        # tax isn't profit
        fractional = order.jobs.get(description__startswith='فينيل')
        self.assertEqual((fractional.quantity, fractional.total_price), (1, D('30.00')))

        q.refresh_from_db()
        self.assertEqual((q.status, q.converted_order_id), ('converted', order.pk))
        with self.assertRaises(ValidationError):
            convert_quotation(q)

    def test_convert_requires_accepted_quote(self):
        q = self._quote()
        with self.assertRaises(ValidationError):
            convert_quotation(q)
        self.assertFalse(PrintOrder.objects.exists())

    def test_quote_line_delete_recalcs_total(self):
        q = self._quote()
        q.lines.get(description='فينيل').delete()
        q.refresh_from_db()
        self.assertEqual(q.total, D('100'))

    # ── HTTP: statement / reports / quotation endpoints ──────────────
    def _login(self, staff=True, csrf=False):
        user = User.objects.create_user(username=f'u{User.objects.count()}', password='x', is_staff=staff)
        c = Client(enforce_csrf_checks=csrf)
        c.force_login(user)
        return c

    def test_statement_skips_cancelled_and_shows_refunds(self):
        live = self._order(total_amount=D('200'))
        self._order(total_amount=D('999'), status='cancelled')
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('150'), order=live)
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('20'), order=live)
        r = self._login().get(f'/printing/customer/{self.customer.pk}/statement/?from=bad-date', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['total_invoiced'], D('220'))   # 200 invoice + 20 refund
        self.assertEqual(r.context['total_paid'], D('150'))
        self.assertEqual(r.context['final_balance'], D('70'))

    def test_money_pages_need_permission(self):
        c = self._login(staff=False)
        order = self._order()
        for url in ('/printing/reports/profit-loss/',
                    f'/printing/customer/{self.customer.pk}/statement/',
                    f'/printing/order/{order.pk}/profit/'):
            self.assertEqual(c.get(url, HTTP_HOST=self.host).status_code, 403, url)

    def test_profit_page_uses_material_cost(self):
        paper = PrintMaterial.objects.create(name='بنر', quantity=D('10'), cost_per_unit=D('5'))
        order = self._order()
        job = PrintJob.objects.create(order=order, description='بنر', quantity=1, unit_price=D('100'))
        PrintJobMaterial.objects.create(job=job, material=paper, quantity=D('4'))
        r = self._login().get(f'/printing/order/{order.pk}/profit/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['total_cost'], D('20.00'))
        self.assertEqual(r.context['gross_profit'], D('80.00'))

    def test_quotation_create_needs_csrf_and_valid_lines(self):
        body = {'title': 'عرض تجربة', 'customer_id': self.customer.pk,
                'lines': [{'description': 'كروت', 'quantity': 2, 'unit_price': 10}]}
        r = self._login(csrf=True).post('/printing/quotation/create/', json.dumps(body),
                                        content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 403)

        c = self._login()
        bad = dict(body, lines=[{'description': 'كروت', 'quantity': -2, 'unit_price': 10}])
        r = c.post('/printing/quotation/create/', json.dumps(bad), content_type='application/json',
                   HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(PriceQuotation.objects.exists())

        r = c.post('/printing/quotation/create/', json.dumps(body), content_type='application/json',
                   HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        self.assertEqual(data['total'], '20.00')
        self.assertTrue(data['whatsapp_url'].startswith('https://wa.me/201011112222?text='))
