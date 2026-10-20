"""
📒 صفحات المطبعة المحاسبية والأدمن — من غير ما حاجة توقع، وبالأرقام الصح.

* قائمة الدخل بالاستحقاق: العربون ورأس المال مش إيراد، والإيراد = اللي اتسلّم.
* ميزان المراجعة / الميزانية / اليومية + كشف حساب المورد + كشف العميل بالرصيد المقدّم.
* فاتورة شراء من الأدمن: «استلم دلوقتي» ← المخزون يزيد والخزنة تنقص لوحدها.
* كل صفحات الأدمن الجديدة بتفتح.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone

from inventory.tests.base import ERPTenantTestCase
from printing.models import (
    MaterialPurchase, PrintCustomer, PrintJob, PrintJobMaterial, PrintMaterial, PrintOrder,
    PrintProof, PrintStockAdjustment, PrintSupplier, PrintTransaction, PrintTreasury,
)

User = get_user_model()
D = Decimal


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingAccountingPagesTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.host = self.domain.domain
        self.treasury = PrintTreasury.objects.create(name='الخزنة', balance=D('5000'))
        self.customer = PrintCustomer.objects.create(name='عميل الصفحات', phone='01012345678')
        self.supplier = PrintSupplier.objects.create(name='مورد الحبر', phone='01055554444')
        self.ink = PrintMaterial.objects.create(name='حبر أسود', unit='عبوة', quantity=D('2'), cost_per_unit=D('100'))
        self.boss = User.objects.create_superuser('pboss', 'p@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.boss)

    def get(self, url, **kw):
        return self.c.get(url, HTTP_HOST=self.host, **kw)

    def test_profit_loss_is_accrual(self):
        order = PrintOrder.objects.create(customer=self.customer, status='confirmed')
        job = PrintJob.objects.create(order=order, description='بنر', quantity=1, unit_price=D('1000'),
                                      proof_waived=True)
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('400'), order=order)
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('3000'),
                                        category='capital', description='إيداع المالك')
        r = self.get('/printing/reports/profit-loss/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['total_income'], D('0'))      # عربون + رأس مال = مش إيراد
        self.assertEqual(r.context['cash_in'], D('3400'))

        PrintJobMaterial.objects.create(job=job, material=self.ink, quantity=D('1'))
        order.status = 'delivered'
        order.save()
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('250'),
                                        description='فاتورة كهرباء')
        r = self.get('/printing/reports/profit-loss/')
        self.assertEqual(r.context['total_income'], D('1000.00'))
        self.assertEqual(r.context['cogs'], D('100.00'))
        self.assertEqual(r.context['gross_profit'], D('900.00'))
        self.assertEqual(r.context['net_profit'], D('650.00'))
        self.assertEqual(r.context['orders_count'], 1)
        self.assertContains(r, 'كهرباء ومياه ومرافق')
        # المساعد الذكي بيقول نفس الربح (مش فلوس الخزنة اللي فيها رأس مال وعربون)
        from printing.views.copilot import _query_business_data
        self.assertIn('صافي الربح (استحقاق): 650.00', _query_business_data('كام الربح الشهر ده')['context'])

    def test_books_pages_render_and_balance(self):
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('100'),
                                        description='إيجار')
        pages = {tab: self.get(f'/printing/accounting/?tab={tab}') for tab in ('trial', 'sheet', 'journal')}
        for tab, r in pages.items():
            self.assertEqual(r.status_code, 200, tab)
        # كل تبويب بيحسب بياناته هو بس
        self.assertTrue(pages['trial'].context['trial']['is_balanced'])
        self.assertTrue(pages['sheet'].context['sheet']['is_balanced'])
        self.assertTrue(pages['journal'].context['entries'])

    def test_statements(self):
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('300'),
                                        customer=self.customer)
        r = self.get(f'/printing/customer/{self.customer.pk}/statement/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['final_balance'], D('-300'))
        self.assertEqual(r.context['summary']['available_credit'], D('300'))
        self.assertContains(r, 'رصيد مقدّم')

        p = MaterialPurchase.objects.create(supplier=self.supplier, payment_terms='credit')
        p.lines.create(material=self.ink, quantity=D('3'), unit_cost=D('120'))
        p.receive()
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('100'), purchase=p)
        r = self.get(f'/printing/supplier/{self.supplier.pk}/statement/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['summary']['balance'], D('260.00'))
        self.assertEqual(r.context['events'][-1]['balance'], D('260.00'))

    def test_books_need_permission(self):
        clerk = User.objects.create_user('clerk', password='x')
        c = Client()
        c.force_login(clerk)
        for url in ('/printing/accounting/', f'/printing/supplier/{self.supplier.pk}/statement/'):
            self.assertEqual(c.get(url, HTTP_HOST=self.host).status_code, 403, url)

    def test_admin_purchase_receive_now_moves_stock_and_cash(self):
        data = {
            'supplier': self.supplier.pk, 'supplier_invoice_no': 'INV-77', 'date': timezone.localdate().isoformat(),
            'payment_terms': 'cash', 'treasury': self.treasury.pk, 'discount': '0', 'tax_amount': '0',
            'notes': '', 'receive_now': 'on',
            'lines-TOTAL_FORMS': '1', 'lines-INITIAL_FORMS': '0', 'lines-MIN_NUM_FORMS': '0', 'lines-MAX_NUM_FORMS': '1000',
            'lines-0-material': self.ink.pk, 'lines-0-quantity': '8', 'lines-0-unit_cost': '125',
            'payments-TOTAL_FORMS': '0', 'payments-INITIAL_FORMS': '0', 'payments-MIN_NUM_FORMS': '0', 'payments-MAX_NUM_FORMS': '1000',
        }
        r = self.c.post('/secure-portal/printing/materialpurchase/add/', data, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context.get('errors'))
        p = MaterialPurchase.objects.get()
        self.ink.refresh_from_db()
        self.treasury.refresh_from_db()
        self.assertEqual((p.status, p.paid_amount), ('received', D('1000.00')))
        self.assertEqual(self.ink.quantity, D('10'))
        self.assertEqual(self.ink.cost_per_unit, D('120'))          # (200 + 1000) / 10
        self.assertEqual(self.treasury.balance, D('4000'))
        r = self.get(f'/secure-portal/printing/materialpurchase/{p.pk}/change/')
        self.assertEqual(r.status_code, 200)

    def test_admin_stock_count_adjustment(self):
        data = {'material': self.ink.pk, 'counted_quantity': '1', 'quantity_change': '', 'reason': 'count', 'notes': 'جرد'}
        r = self.c.post('/secure-portal/printing/printstockadjustment/add/', data, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        self.ink.refresh_from_db()
        self.assertEqual(self.ink.quantity, D('1'))
        self.assertEqual(PrintStockAdjustment.objects.get().quantity_change, D('-1'))

    def test_salary_advance_is_an_asset_recovered_from_payroll(self):
        """🐛 المطبعة: السلفة أصل وقت صرفها، والقسط المخصوم من المرتب بيقفلها (المرتب كامل مصروف)."""
        from django.db.models import Sum
        from hr.models import Employee, HRSettings
        from hr.services.advance_service import AdvanceService
        from hr.services.payroll_service import PayrollService
        from inventory.models import AccountingEntry

        def acct(code):
            agg = AccountingEntry.objects.filter(account__code=code).aggregate(d=Sum('debit'), c=Sum('credit'))
            return (agg['d'] or D('0')) - (agg['c'] or D('0'))

        HRSettings.objects.get_or_create(pk=1)
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('10000'),
                                        category='capital')
        worker = User.objects.create_user('printer_emp', password='x', first_name='سيد', last_name='علي')
        emp = Employee.objects.create(user=worker, department='printing', base_salary=D('6000.00'))
        adv = AdvanceService.request_advance(emp, D('1500'), installments_count=3)
        AdvanceService.approve_advance(adv.pk, None, by_user=self.boss)
        AdvanceService.disburse_advance(adv.pk, self.treasury.pk, by_user=self.boss)
        self.assertEqual(PrintTransaction.objects.get(description__startswith='صرف سلفة').category, 'advance')
        self.assertEqual(acct('1150'), D('1500.00'))

        adv.refresh_from_db()
        first_due = adv.installments.order_by('due_month').first().due_month
        run = PayrollService.generate_payroll(first_due.month, first_due.year, created_by=self.boss)
        self.assertEqual(run.entries.get(employee=emp).net_salary, D('5500.00'))
        PayrollService.approve_payroll(run.pk, approved_by=self.boss)
        PayrollService.disburse_payroll(run.pk)
        self.assertEqual(acct('1150'), D('1000.00'))             # قسط اتسدّد من المرتب
        self.assertEqual(acct('5100'), D('6000.00'))             # المرتب كامل مصروف
        self.treasury.refresh_from_db()
        self.assertEqual(self.treasury.balance, D('15000') - D('1500') - D('5500'))

    def test_admin_pages_open(self):
        order = PrintOrder.objects.create(customer=self.customer, status='confirmed')
        job = PrintJob.objects.create(order=order, description='كروت', quantity=1, unit_price=D('50'))
        from django.core.files.uploadedfile import SimpleUploadedFile
        proof = PrintProof.objects.create(job=job, file=SimpleUploadedFile('p.png', b'x', content_type='image/png'))
        urls = [
            '/secure-portal/printing/printsupplier/', '/secure-portal/printing/printsupplier/add/',
            f'/secure-portal/printing/printsupplier/{self.supplier.pk}/change/',
            '/secure-portal/printing/materialpurchase/', '/secure-portal/printing/materialpurchase/add/',
            '/secure-portal/printing/printstockadjustment/', '/secure-portal/printing/printstockadjustment/add/',
            '/secure-portal/printing/printproof/', f'/secure-portal/printing/printproof/{proof.pk}/change/',
            '/secure-portal/printing/printjob/', f'/secure-portal/printing/printjob/{job.pk}/change/',
            '/secure-portal/printing/printorder/', f'/secure-portal/printing/printorder/{order.pk}/change/',
            '/secure-portal/printing/printcustomer/', f'/secure-portal/printing/printcustomer/{self.customer.pk}/change/',
            '/secure-portal/printing/printmaterial/', f'/secure-portal/printing/printmaterial/{self.ink.pk}/change/',
            '/secure-portal/printing/printtransaction/',
        ]
        for url in urls:
            self.assertEqual(self.get(url).status_code, 200, url)
