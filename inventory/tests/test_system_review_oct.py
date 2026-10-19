"""🧪 مراجعة أكتوبر الشاملة — تصليحات اتلقت وأنا بجرّب الشاشات بنفسي.

* «خصم على المتبقّي» في نافذة التحصيل على فاتورة عليها ضريبة: كان بيتضاف لخصم
  الفاتورة قبل الضريبة، فـ «خصم ١٣» بيشيل 14.82، والتحصيل اللي بعده بيتقصّ على
  المتبقّي الجديد — الكاشير ياخد الفلوس كاملة والخزنة تتسجّل أقل منها.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import Client, override_settings

from inventory.models import FinancialTransaction, SaleInvoice, ServiceCatalog
from inventory.services.inventory_service import set_branch_stock
from inventory.services.books_audit import books_problems
from .base import ERPTenantTestCase
from .factories import make_branch, make_customer, make_product, make_treasury

D = lambda v: Decimal(str(v))  # noqa: E731


@override_settings(ALLOWED_HOSTS=['*'])
class DiscountOnRemainingWithTaxTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch('الفرع الرئيسي')
        self.cash = make_treasury(self.branch, name='كاش', balance='0.00')
        self.customer = make_customer('أحمد علي', phone='01011112222')
        self.oil = make_product(part_number='REV-OIL', name='فلتر زيت', retail_price='250.00',
                                purchase_price='0', average_cost='0')
        with transaction.atomic():
            set_branch_stock(self.oil, self.branch, 10, unit_cost=D('120'))
        self.owner = User.objects.create_user('rev_owner', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.owner)

    def post_json(self, url, payload):
        return self.c.post(url, data=json.dumps(payload), content_type='application/json', HTTP_HOST=self.host)

    def _delivered_job(self):
        """أمر شغل: قطعة 250 + مصنعية 200 + ضريبة ١٤٪ = 513."""
        r = self.post_json('/system/job-card/save/', {
            'branch_id': self.branch.pk, 'customer_id': self.customer.pk, 'invoice_type': 'maintenance',
            'labor_cost_manual': '200', 'tax_percentage': '14',
            'items': [{'product_id': self.oil.pk, 'qty': 1, 'price': 250}], 'services': [],
        })
        self.assertEqual(r.status_code, 200, r.content)
        job = SaleInvoice.objects.get(pk=r.json()['invoice_id'])
        self.assertEqual(job.total_amount, D('513.00'))
        self.c.post(f'/system/invoices/{job.pk}/deliver/', HTTP_HOST=self.host)
        job.refresh_from_db()
        self.assertEqual(job.status, 'posted')
        return job

    def _cash_in(self, job):
        return sum((t.amount for t in FinancialTransaction.objects.filter(
            sale_invoice=job, transaction_type='in')), D(0))

    def test_discount_cuts_the_remaining_by_exactly_the_typed_amount(self):
        job = self._delivered_job()
        r = self.post_json(f'/system/invoices/{job.pk}/pay/',
                           {'treasury_id': self.cash.pk, 'amount': '400', 'discount': '13'})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()['total'], r.json()['paid'], r.json()['due']), (500.0, 400.0, 100.0))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D('100.00'))
        self.assertEqual(self._cash_in(job), D('400.00'))
        self.assertEqual(books_problems(), [])

    def test_cash_taken_is_the_cash_recorded(self):
        """«حصّل المتبقّي − ١٠ وخصم ١٠» = الدرج فيه المبلغ المكتوب بالظبط."""
        job = self._delivered_job()
        r = self.post_json(f'/system/invoices/{job.pk}/pay/',
                           {'treasury_id': self.cash.pk, 'amount': '503', 'discount': '10'})
        self.assertEqual(r.status_code, 200, r.content)
        job.refresh_from_db()
        self.assertEqual(job.open_due, D('0.00'))
        self.assertEqual(self._cash_in(job), D('503.00'))
        self.cash.refresh_from_db()
        self.assertEqual(self.cash.balance, D('503.00'))
        self.assertEqual(books_problems(), [])

    def test_full_remaining_discount_closes_the_invoice_without_going_negative(self):
        job = self._delivered_job()
        r = self.post_json(f'/system/invoices/{job.pk}/pay/', {'discount': '513'})
        self.assertEqual(r.status_code, 200, r.content)
        job.refresh_from_db()
        self.assertEqual(job.total_amount, D('0.00'))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.balance, D('0.00'))
        self.assertEqual(books_problems(), [])

    def test_invoice_without_tax_is_unchanged(self):
        ServiceCatalog.objects.create(name='فحص', labor_price=D('0'))
        r = self.post_json('/system/lightning-pos/checkout/', {
            'branch_id': self.branch.pk, 'customer_id': self.customer.pk,
            'items': [{'product_id': self.oil.pk, 'qty': 2, 'price': 250}], 'payments': []})
        inv = SaleInvoice.objects.get(pk=r.json()['invoice_id'])
        r = self.post_json(f'/system/invoices/{inv.pk}/pay/',
                           {'treasury_id': self.cash.pk, 'amount': '450', 'discount': '50'})
        self.assertEqual((r.json()['total'], r.json()['paid'], r.json()['due']), (450.0, 450.0, 0.0))
        self.assertEqual(books_problems(), [])


@override_settings(ALLOWED_HOSTS=['*'])
class AdminReportsPageTests(ERPTenantTestCase):
    """🐛 صفحة /secure-portal/reports/: كانت بتقع 500 أول ما يبقى في موظف HR،
    وتقرير المطبعة كان بيقع دايماً ويتحوّل بصمت لتقرير السيارات بأصفار، وأسماء
    العملاء كانت بتتطبع جوه <script> من غير تهريب (stored XSS)."""

    PAYLOAD = '</script><script>window.__xss=1</script>'

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.owner = User.objects.create_user('rep_owner', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.owner)

    def _get(self):
        r = self.c.get('/secure-portal/reports/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_automotive_reports_with_hr_employee_and_hostile_customer_name(self):
        from hr.models import Employee
        Employee.objects.create(user=self.owner, department='workshop', base_salary=D('5000'))
        make_customer(self.PAYLOAD, phone='01099999999')
        html = self._get()
        self.assertIn('var employeesSummary = [{', html)       # كانت 500 (__proxy__)
        self.assertNotIn(self.PAYLOAD, html)
        self.assertIn('\\u003C/script\\u003E\\u003Cscript\\u003Ewindow.__xss=1', html)

    def test_printing_reports_render_the_printing_numbers(self):
        from printing.models import PrintBranch, PrintCustomer, PrintMaterial
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        branch = PrintBranch.objects.create(name='الفرع الرئيسي')
        PrintMaterial.objects.create(name='ورق A4', category='paper', unit='رزمة', quantity=0,
                                     cost_per_unit=D('180'), min_stock=5, branch=branch)
        PrintCustomer.objects.create(name=self.PAYLOAD, phone='01000000001')
        html = self._get()
        self.assertIn('\\u003C/script\\u003E\\u003Cscript\\u003Ewindow.__xss=1', html)  # من عملاء المطبعة
        self.assertNotIn(self.PAYLOAD, html)


@override_settings(ALLOWED_HOSTS=['*'])
class StaffCannotOpenManagementScreensTests(ERPTenantTestCase):
    """🐛 شاشات إدارة كانت login بس: إعدادات الرد الآلي (توكنات Meta)، استوديو التسويق
    (إطلاق حملات بتصرف من حساب الإعلانات)، تصدير محادثات العملاء، وكشف حساب المورد
    بالرقم (رغم إن صفحة المورد مقفولة على الكاشير)."""

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        from .factories import make_employee, make_vendor
        self.branch = make_branch('الفرع الرئيسي')
        self.cashier, _ = make_employee('rev_cashier', role='cashier', branch=self.branch)
        self.manager, _ = make_employee('rev_manager', role='manager', branch=self.branch)
        self.vendor = make_vendor('مورد')
        self.customer = make_customer('عميل', phone='01012121212')

    def _client(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_cashier_is_refused_and_manager_allowed(self):
        cashier, manager = self._client(self.cashier), self._client(self.manager)
        for url in ('/omnichannel/settings/', '/social-studio/settings/', '/social-studio/studio/',
                    f'/system/statement/vendor/{self.vendor.pk}/print/',
                    f'/system/api/v1/statement/vendor/{self.vendor.pk}/'):
            self.assertEqual(cashier.get(url, HTTP_HOST=self.host).status_code, 403, url)
        for url in ('/omnichannel/settings/', '/social-studio/settings/',
                    f'/system/statement/vendor/{self.vendor.pk}/print/'):
            self.assertEqual(manager.get(url, HTTP_HOST=self.host).status_code, 200, url)
        # الكاشير لسه يطبع كشف حساب العميل (زي صفحة العميل)
        r = cashier.get(f'/system/statement/customer/{self.customer.pk}/print/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)

    def test_cashier_cannot_rewrite_the_meta_settings(self):
        from omnichannel.models import TenantChannelConfig
        config, _ = TenantChannelConfig.objects.get_or_create(tenant=self.tenant)
        before = config.ai_enabled if hasattr(config, 'ai_enabled') else None
        r = self._client(self.cashier).post('/omnichannel/settings/', {'business_name': 'hijack'},
                                           HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 403)
        config.refresh_from_db()
        if before is not None:
            self.assertEqual(config.ai_enabled, before)
