"""
🧪 Final hands-on review — bugs found by running the whole cycle in a browser.

* Partial return with chosen lines returned EVERY other line too.
* After a return that came off the customer's account, the original invoice
  still showed its full «remaining» (and let it be collected again).
* Sales report «paid» added cash refunds; customers report ignored returns;
  the sales report's Excel export crashed on a translated label.
* Job cards stayed «in progress» forever: no way to close them in the app,
  no return, missing from the printed statement. Parts-only → posted now.
* POS: a name typed without a phone was silently dropped; a credit sale with
  no phone put the debt on the shared walk-in customer.
* Vendors page hid every vendor with a zero balance (new vendors vanished).
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import Customer, Inventory, SaleInvoice
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_inventory, make_product, make_sale_invoice,
    make_treasury, make_vendor,
)


@override_settings(ALLOWED_HOSTS=['*'])
class FinalReviewTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.till = make_treasury(self.branch, balance='5000.00')
        self.customer = make_customer(name='عميل آجل', phone='01099990000')
        self.pads = make_product(part_number='FR-PAD', retail_price='1400.00', average_cost='900.00')
        self.filt = make_product(part_number='FR-FLT', retail_price='250.00', average_cost='150.00')
        make_inventory(self.pads, self.branch, quantity=10)
        make_inventory(self.filt, self.branch, quantity=10)
        self.boss = User.objects.create_user('fr_boss', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.boss)

    def _stock(self, p):
        return Inventory.objects.get(product=p, branch=self.branch).quantity

    def _credit_sale(self):
        """1400 + 250 = 1650، مدفوع 650 والباقي 1000 آجل."""
        inv = make_sale_invoice(self.customer, self.branch, treasury=self.till,
                                items=[(self.pads, 1, '1400.00'), (self.filt, 1, '250.00')])
        SaleInvoice.objects.filter(pk=inv.pk).update(paid_amount=Decimal('650'))
        inv.refresh_from_db()
        inv.status = 'posted'
        inv.save()
        inv.refresh_from_db()
        return inv

    def _post(self, url, data):
        return self.c.post(url, json.dumps(data), content_type='application/json',
                           HTTP_ACCEPT='application/json', HTTP_HOST=self.host)

    # ------------------------------------------------------------------ returns
    def test_partial_return_returns_only_the_chosen_line(self):
        inv = self._credit_sale()
        flt_line = inv.items.get(product=self.filt)
        r = self.c.post(f'/system/invoices/{inv.pk}/return/', {f'qty_{flt_line.pk}': '1'},
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        ret = SaleInvoice.objects.get(original_invoice=inv)
        self.assertEqual(list(ret.items.values_list('product__part_number', flat=True)), ['FR-FLT'])
        self.assertEqual(ret.total_amount, Decimal('250.00'))
        self.assertEqual(self._stock(self.filt), 10)    # رجع
        self.assertEqual(self._stock(self.pads), 9)     # لسه مباع

    def test_credit_return_closes_the_original_remaining(self):
        inv = self._credit_sale()
        self.assertEqual(Customer.objects.get(pk=self.customer.pk).balance, Decimal('1000.00'))
        pads_line = inv.items.get(product=self.pads)
        self.c.post(f'/system/invoices/{inv.pk}/return/', {f'qty_{pads_line.pk}': '1'}, HTTP_HOST=self.host)
        ret = SaleInvoice.objects.get(original_invoice=inv)
        # 1400 مرتجع: بيسدّد الآجل (1000) الأول، والكاش بيترد عن الزيادة بس (400)
        # — مابنديش كاش لعميل لسه عليه فلوس على نفس الفاتورة.
        self.assertEqual(ret.paid_amount, Decimal('400.00'))
        inv.refresh_from_db()
        self.assertEqual(inv.returned_credit, Decimal('1000.00'))
        self.assertEqual(inv.open_due, Decimal('0.00'))
        self.assertEqual(Customer.objects.get(pk=self.customer.pk).balance, Decimal('0.00'))

        page = self.c.get('/system/invoices/?pay=due', HTTP_HOST=self.host)
        self.assertEqual(list(page.context['page'].object_list), [])
        # التحصيل مايعديش المتبقّي الفعلي (صفر) — كان بيقبل 1000
        r = self._post(f'/system/invoices/{inv.pk}/pay/', {'amount': '1000', 'treasury_id': self.till.pk})
        self.assertEqual(r.status_code, 200, r.content)
        inv.refresh_from_db()
        self.assertEqual(inv.paid_amount, Decimal('650.00'))
        self.assertEqual(inv.open_due, Decimal('0.00'))
        self.assertEqual(Customer.objects.get(pk=self.customer.pk).balance, Decimal('0.00'))

    def test_reports_net_out_returns(self):
        inv = self._credit_sale()
        pads_line = inv.items.get(product=self.pads)
        self.c.post(f'/system/invoices/{inv.pk}/return/', {f'qty_{pads_line.pk}': '1'}, HTTP_HOST=self.host)
        r = self.c.get('/system/reports/sales/?period=all', HTTP_HOST=self.host)
        # صافي 250، اتدفع 650 واترد 400 (المرتجع سدّد الآجل الأول) → المحصّل
        # صافي 250 ومفيش آجل (مش بالسالب)
        self.assertEqual(r.context['net_sales'], Decimal('250.00'))
        self.assertEqual(r.context['paid'], Decimal('250.00'))
        self.assertEqual(r.context['due'], Decimal('0.00'))
        r = self.c.get('/system/reports/customers/?period=all', HTTP_HOST=self.host)
        row = [x for x in r.context['rows'] if x['id'] == self.customer.pk][0]
        self.assertEqual(row['total'], Decimal('250.00'))
        self.assertEqual(row['due'], Decimal('0.00'))
        # تصدير Excel لتقرير المبيعات كان بيقع 500 («Cannot convert 'بيع قطع غيار' to Excel»)
        r = self.c.get('/system/reports/sales/?period=all&export=xlsx', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertIn('spreadsheetml', r['Content-Type'])

    # ---------------------------------------------------------------- job cards
    def test_parts_only_job_card_is_posted_and_job_can_be_delivered(self):
        base = {'branch_id': self.branch.pk, 'customer_id': self.customer.pk,
                'items': [{'product_id': self.filt.pk, 'qty': 1, 'price': 250}],
                'treasury_id': self.till.pk, 'paid_amount': 250}
        r = self._post('/system/job-card/save/', dict(base, invoice_type='sale'))
        self.assertEqual(r.status_code, 200, r.content)
        parts = SaleInvoice.objects.get(pk=r.json()['invoice_id'])
        self.assertEqual(parts.status, 'posted')
        self.assertTrue(parts.is_applied)

        r = self._post('/system/job-card/save/', dict(base, invoice_type='maintenance', paid_amount=100))
        job = SaleInvoice.objects.get(pk=r.json()['invoice_id'])
        self.assertEqual(job.status, 'in_progress')
        self.assertEqual(self._stock(self.filt), 8)
        # كشف الحساب المطبوع فيه أمر الشغل المفتوح (عليه 150 آجل)
        st = self.c.get(f'/system/statement/customer/{self.customer.pk}/print/', HTTP_HOST=self.host)
        self.assertIn(job.pk, [i.pk for i in st.context['invoices']])

        r = self.c.post(f'/system/invoices/{job.pk}/deliver/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        self.assertIn(f'delivered={job.pk}', r['Location'])
        job.refresh_from_db()
        self.assertEqual(job.status, 'posted')
        self.assertEqual(self._stock(self.filt), 8)                       # مفيش خصم تاني
        self.assertEqual(Customer.objects.get(pk=self.customer.pk).balance, Decimal('150.00'))
        self.assertEqual(self.c.get(f'/system/invoices/{job.pk}/return/', HTTP_HOST=self.host).status_code, 200)

    def test_deliver_pays_the_technician_once(self):
        from inventory.models import ServiceCatalog
        from .factories import make_employee
        _u, tech = make_employee(username='fr_tech', role='tech')
        svc = ServiceCatalog.objects.create(name='ضبط زوايا', labor_price=Decimal('150'),
                                            estimated_hours=Decimal('1'), tech_commission_percent=Decimal('10'))
        r = self._post('/system/job-card/save/', {'branch_id': self.branch.pk, 'customer_id': self.customer.pk,
                                                  'services': [{'service_id': svc.pk}]})
        si = SaleInvoice.objects.get(pk=r.json()['invoice_id'])
        si.service_items.update(technician=tech)          # اتعيّن في الورشة
        self.c.post(f'/system/invoices/{si.pk}/deliver/', HTTP_HOST=self.host)
        tech.refresh_from_db()
        self.assertEqual(tech.commission_balance, Decimal('15.00'))
        r = self.c.post(f'/system/invoices/{si.pk}/deliver/', HTTP_HOST=self.host)
        self.assertIn('err=cannot_deliver', r['Location'])  # مرة واحدة بس
        tech.refresh_from_db()
        self.assertEqual(tech.commission_balance, Decimal('15.00'))

    # ---------------------------------------------------------------------- POS
    def test_pos_name_without_phone_and_walk_in_credit(self):
        item = [{'product_id': self.filt.pk, 'qty': 1, 'price': 250}]
        paid = {'treasury_id': self.till.pk, 'paid_amount': 250}
        r = self._post('/system/lightning-pos/checkout/',
                       dict(paid, branch_id=self.branch.pk, items=item, customer_name='عميل آجل'))
        self.assertEqual(SaleInvoice.objects.get(pk=r.json()['invoice_id']).customer_id, self.customer.pk)

        r = self._post('/system/lightning-pos/checkout/',
                       dict(paid, branch_id=self.branch.pk, items=item, customer_name='زبون جديد'))
        inv = SaleInvoice.objects.get(pk=r.json()['invoice_id'])
        self.assertIn('زبون جديد', inv.notes)

        before = SaleInvoice.objects.count()
        r = self._post('/system/lightning-pos/checkout/',
                       {'branch_id': self.branch.pk, 'items': item, 'customer_name': ''})
        self.assertEqual(r.status_code, 400)
        self.assertIn('رقم موبايل', r.json()['error'])
        self.assertEqual(SaleInvoice.objects.count(), before)
        self.assertEqual(self._stock(self.filt), 8)

    # ------------------------------------------------------------------ vendors
    def test_vendors_page_lists_new_vendor(self):
        make_vendor(name='مورد لسه جديد', phone='0123')
        r = self.c.get('/system/vendors/', HTTP_HOST=self.host)
        self.assertContains(r, 'مورد لسه جديد')
