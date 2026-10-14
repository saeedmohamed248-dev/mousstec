"""
🧪 يوم شغل كامل من الشاشات نفسها — والدفاتر لازم تفضل مظبوطة بعد كل خطوة.

شراء (بمصاريف شحن + دفعة جزئية) ← بيع كاش ← بيع آجل ← أمر شغل وتسليمه ←
تحصيل من العميل ← مرتجع جزئي ← تحصيل على الفاتورة ← سداد المورد وإلغاء دفعة ←
مرتجع مشتريات ← مصروف (وتعديله وحذفه) ← تحويل بين الخزائن ← تحويل مخزون بين
الفروع (خروج/استلام/إلغاء) ← حذف فاتورة ← إلغاء تحصيل ← التقارير.

بعد كل خطوة بنتأكّد إن:
* الدفتر متوازن، والمخزون = حركاته، وقيمته = حساب المخزون (books_problems)؛
* حساب العملاء (١١٠٠) = مجموع أرصدة العملاء، وكل عميل رصيده = المتبقّي على
  فواتيره − التحصيل على الحساب؛
* حساب الموردين (٢١٠٠) = مجموع أرصدة الموردين؛
* حساب النقدية = مجموع أرصدة الخزائن، وحساب تحويلات الخزائن الوسيط = صفر.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.db.models import Q, Sum
from django.test import Client, override_settings

from inventory.models import (
    AccountingEntry, ChartOfAccount, Customer, FinancialTransaction, Inventory,
    PurchaseInvoice, SaleInvoice, ServiceCatalog, StockTransfer, Treasury, Vendor,
)
from inventory.services.books_audit import books_problems
from .base import ERPTenantTestCase
from .factories import make_branch, make_employee, make_product, make_treasury

D = lambda v: Decimal(str(v))  # noqa: E731


def acct(code):
    a = ChartOfAccount.objects.filter(code=code).first()
    if a is None:
        return D('0.00')
    agg = AccountingEntry.objects.filter(account=a).aggregate(d=Sum('debit'), c=Sum('credit'))
    return (agg['d'] or D(0)) - (agg['c'] or D(0))


@override_settings(ALLOWED_HOSTS=['*'])
class BusinessDayEndToEndTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        # فرعين و٣ خزائن وكذا موظف — أكتر من حدود الباقة الافتراضية (0 = بلا حد)
        unlimited = dict(max_treasuries=0, max_branches=0, max_users=0)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(**unlimited)
        for k, v in unlimited.items():
            setattr(self.tenant, k, v)
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.main = make_branch('الفرع الرئيسي')
        self.second = make_branch('فرع مدينة نصر')
        self.cash = make_treasury(self.main, name='كاش', balance='0.00')
        self.bank = make_treasury(self.main, name='بنك', balance='0.00', type='bank')
        self.cash2 = make_treasury(self.second, name='كاش ٢', balance='0.00')
        self.owner = User.objects.create_user('owner_e2e', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.owner)
        self.oil = make_product(part_number='E2E-OIL', name='زيت محرك', retail_price='300.00',
                                purchase_price='0', average_cost='0')
        self.pad = make_product(part_number='E2E-PAD', name='تيل فرامل', retail_price='900.00',
                                purchase_price='0', average_cost='0')
        self.service = ServiceCatalog.objects.create(name='تغيير زيت', labor_price=D('150.00'))

    # ── helpers ───────────────────────────────────────────────────────
    def post(self, url, data=None, **kw):
        return self.c.post(url, data or {}, HTTP_HOST=self.host, **kw)

    def post_json(self, url, payload):
        r = self.c.post(url, data=json.dumps(payload), content_type='application/json',
                        HTTP_HOST=self.host, HTTP_ACCEPT='application/json')
        return r

    def get(self, url):
        return self.c.get(url, HTTP_HOST=self.host)

    def stock(self, product, branch=None):
        row = Inventory.objects.filter(product=product, branch=branch or self.main).first()
        return row.quantity if row else 0

    def assert_books(self, step):
        problems = books_problems()
        self.assertEqual(problems, [], f"[{step}] {problems}")
        customers = Customer.objects.aggregate(s=Sum('balance'))['s'] or D(0)
        self.assertEqual(acct('1100'), customers, f"[{step}] AR ≠ customer balances")
        vendors = Vendor.objects.aggregate(s=Sum('balance'))['s'] or D(0)
        self.assertEqual(-acct('2100'), vendors, f"[{step}] AP ≠ vendor balances")
        treasuries = Treasury.objects.aggregate(s=Sum('balance'))['s'] or D(0)
        self.assertEqual(acct('1001') + acct('1002'), treasuries, f"[{step}] cash ≠ treasuries")
        self.assertEqual(acct('1090'), D(0), f"[{step}] transfer clearing not zero")
        for cust in Customer.objects.all():
            self.assertEqual(cust.balance, self.expected_customer_balance(cust),
                             f"[{step}] customer «{cust.name}» balance ≠ invoices − on account")

    @staticmethod
    def expected_customer_balance(customer):
        expected = D(0)
        for inv in (SaleInvoice.objects.filter(customer=customer, is_return=False, is_applied=True)
                    .prefetch_related('return_invoices')):
            expected += inv.due_amount - inv.returned_credit
        on_acc = FinancialTransaction.objects.filter(customer=customer, sale_invoice__isnull=True)
        agg = on_acc.aggregate(i=Sum('amount', filter=Q(transaction_type='in')),
                               o=Sum('amount', filter=Q(transaction_type='out')))
        return expected - (agg['i'] or D(0)) + (agg['o'] or D(0))

    # ── the day ───────────────────────────────────────────────────────
    def test_full_business_day_keeps_every_book_in_sync(self):
        # 0) رأس مال في الخزنة
        r = self.post(f'/system/treasuries/{self.cash.pk}/movement/',
                      {'kind': 'capital', 'amount': '50000', 'description': 'رأس مال'})
        self.assertIn('ok=moved', r['Location'])
        self.assert_books('capital')

        # 1) مورد جديد + فاتورة شراء بمصاريف شحن ودفعة جزئية
        r = self.post_json('/system/vendors/create/', {'name': 'مورد الزيوت', 'phone': '01222222222'})
        vendor = Vendor.objects.get(pk=r.json()['id'])
        r = self.post_json('/system/purchases/save/', {
            'branch_id': self.main.pk, 'vendor_id': vendor.pk,
            'treasury_id': self.cash.pk, 'paid_amount': '1000',
            'items': [{'product_id': self.oil.pk, 'qty': 20, 'cost': '150'},
                      {'product_id': self.pad.pk, 'qty': 10, 'cost': '500'}],
            'extra_costs': [{'kind': 'shipping', 'amount': '400', 'treasury_id': self.cash.pk}],
        })
        self.assertEqual(r.status_code, 200, r.content)
        purchase = PurchaseInvoice.objects.get(pk=r.json()['invoice_id'])
        self.assertTrue(purchase.is_applied)
        self.assertEqual(self.stock(self.oil), 20)
        vendor.refresh_from_db()
        self.assertEqual(vendor.balance, D('7000.00'))   # 8000 − 1000
        self.assert_books('purchase')

        # 2) بيع كاش لعميل نقدي
        r = self.post_json('/system/lightning-pos/checkout/', {
            'branch_id': self.main.pk,
            'payments': [{'treasury_id': self.cash.pk, 'amount': 600}],
            'items': [{'product_id': self.oil.pk, 'qty': 2, 'price': 300}],
        })
        self.assertEqual(r.status_code, 200, r.content)
        self.assert_books('pos cash')

        # 3) بيع آجل جزئي لعميل مسجّل (دفع مقسّم كاش + بنك)
        r = self.post_json('/system/lightning-pos/checkout/', {
            'branch_id': self.main.pk, 'customer_name': 'أحمد علي', 'customer_phone': '01011112222',
            'payments': [{'treasury_id': self.cash.pk, 'amount': 200},
                         {'treasury_id': self.bank.pk, 'amount': 100}],
            'items': [{'product_id': self.pad.pk, 'qty': 1, 'price': 900}],
        })
        self.assertEqual(r.status_code, 200, r.content)
        credit_sale = SaleInvoice.objects.get(pk=r.json()['invoice_id'])
        customer = credit_sale.customer
        self.assertEqual(credit_sale.due_amount, D('600.00'))
        self.assert_books('pos credit')

        # 4) أمر شغل صيانة (قطعة + خدمة + مصنعية + ضريبة) من غير دفع ← تسليم
        r = self.post_json('/system/job-card/save/', {
            'branch_id': self.main.pk, 'customer_id': customer.pk,
            'vehicle_chassis': 'WBAE2E00000000001', 'vehicle_model': '320i',
            'invoice_type': 'maintenance', 'labor_cost_manual': '100', 'tax_percentage': '14',
            'items': [{'product_id': self.oil.pk, 'qty': 1, 'price': 300}],
            'services': [{'service_id': self.service.pk}],
        })
        self.assertEqual(r.status_code, 200, r.content)
        job = SaleInvoice.objects.get(pk=r.json()['invoice_id'])
        self.assertEqual(job.status, 'in_progress')
        self.assertEqual(job.total_amount, D('627.00'))   # (300+150+100) × 1.14
        self.assert_books('job card')
        r = self.post(f'/system/invoices/{job.pk}/deliver/')
        self.assertIn(f'delivered={job.pk}', r['Location'])
        job.refresh_from_db()
        self.assertEqual(job.status, 'posted')
        self.assert_books('deliver')

        # 5) تحصيل من العميل على الحساب ← بيتوزّع على الأقدم
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D('1227.00'))
        r = self.post(f'/system/customers/{customer.pk}/collect/',
                      {'treasury_id': self.cash.pk, 'amount': '700', 'note': 'دفعة'})
        self.assertIn('ok=collected', r['Location'])
        credit_sale.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(credit_sale.due_amount, D('0.00'))   # الأقدم اتقفل
        self.assertEqual(job.due_amount, D('527.00'))         # والباقي على اللي بعده
        page = self.get(f'/system/customers/{customer.pk}/')
        self.assertEqual([i.pk for i in page.context['open_invoices']], [job.pk])
        self.assert_books('collect')

        # 6) مرتجع جزئي من أمر الشغل (قطعة الزيت) ← بيتخصم من المتبقّي
        item = job.items.get()
        r = self.post(f'/system/invoices/{job.pk}/return/', {f'qty_{item.pk}': '1'})
        self.assertIn(f'returned={job.pk}', r['Location'])
        self.assertEqual(self.stock(self.oil), 18)   # 20 − 2 − 1 + 1
        self.assert_books('sale return')

        # 7) تحصيل المتبقّي على الفاتورة نفسها (مع خصم صغير)
        job.refresh_from_db()
        remaining = job.open_due
        r = self.post_json(f'/system/invoices/{job.pk}/pay/', {
            'treasury_id': self.cash.pk, 'amount': str(remaining - D('10')), 'discount': '10'})
        self.assertEqual(r.status_code, 200, r.content)
        job.refresh_from_db()
        self.assertEqual(job.open_due, D('0.00'))
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D('0.00'))
        self.assert_books('invoice pay')

        # 8) سداد المورد على الحساب ← إلغاء دفعة غلط ← سداد تاني
        r = self.post(f'/system/vendors/{vendor.pk}/pay/', {'treasury_id': self.cash.pk, 'amount': '3000'})
        self.assertIn('ok=paid', r['Location'])
        self.assert_books('vendor pay')
        wrong = FinancialTransaction.objects.filter(vendor=vendor, transaction_type='out').latest('id')
        r = self.post(f'/system/vendors/{vendor.pk}/payments/{wrong.pk}/void/')
        self.assertIn('ok=voided', r['Location'])
        vendor.refresh_from_db()
        purchase.refresh_from_db()
        self.assertEqual(vendor.balance, D('7000.00'))
        self.assertEqual(purchase.paid_amount, D('1000.00'))
        self.assert_books('vendor void')
        r = self.post(f'/system/vendors/{vendor.pk}/pay/', {'treasury_id': self.cash.pk, 'amount': '2000'})
        self.assertIn('ok=paid', r['Location'])
        self.assert_books('vendor pay again')

        # 9) مرتجع مشتريات (٢ تيل) — لسه علينا للمورد، فبيتخصم من مستحقاته (مفيش كاش راجع)
        pad_line = purchase.items.get(product=self.pad)
        r = self.post(f'/system/purchases/{purchase.pk}/return/', {
            f'qty_{pad_line.pk}': '2', 'treasury_id': self.cash.pk, 'refund_amount': '0'})
        self.assertEqual(r.status_code, 302, r.context and r.context.get('error'))
        self.assertEqual(self.stock(self.pad), 7)   # 10 − 1 − 2
        self.assert_books('purchase return')

        # 10) مصروف ← تعديل ← حذف
        r = self.post('/system/expense/create/', {'treasury_id': self.cash.pk, 'amount': '150',
                                                  'description': 'كهربا'})
        self.assertEqual(r.status_code, 200, r.content)
        ft_id = r.json()['transaction_id']
        self.assert_books('expense')
        r = self.post(f'/system/expenses/{ft_id}/edit/', {'treasury_id': self.bank.pk, 'amount': '90',
                                                          'description': 'كهربا'})
        self.assertIn('ok=edited', r['Location'])
        self.assert_books('expense edit')
        new_ft = FinancialTransaction.objects.filter(description='كهربا').latest('id')
        r = self.post(f'/system/expenses/{new_ft.pk}/delete/')
        self.assertIn('ok=deleted', r['Location'])
        self.assert_books('expense delete')

        # 11) تحويل بين خزنتين
        r = self.post(f'/system/treasuries/{self.cash.pk}/transfer/', {'to_id': self.bank.pk, 'amount': '500'})
        self.assertIn('ok=transferred', r['Location'])
        self.assert_books('treasury transfer')

        # 12) تحويل مخزون بين الفروع: خروج ← استلام، وتحويل تاني يتلغي وهو في الطريق
        r = self.post_json('/system/stock-transfers/create/', {
            'from_branch_id': self.main.pk, 'to_branch_id': self.second.pk, 'ship': True,
            'items': [{'product_id': self.oil.pk, 'qty': 3}, {'product_id': self.pad.pk, 'qty': 1}]})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.stock(self.oil), 15)
        self.assertEqual(self.stock(self.oil, self.second), 0)
        self.assert_books('transfer shipped')
        for t in StockTransfer.objects.filter(pk__in=r.json()['ids']):
            rr = self.post(f'/system/stock-transfers/{t.pk}/receive/')
            self.assertIn('ok=receive', rr['Location'])
        self.assertEqual(self.stock(self.oil, self.second), 3)
        self.assertEqual(self.stock(self.pad, self.second), 1)
        self.assert_books('transfer received')
        r = self.post_json('/system/stock-transfers/create/', {
            'from_branch_id': self.main.pk, 'to_branch_id': self.second.pk, 'ship': True,
            'items': [{'product_id': self.oil.pk, 'qty': 5}]})
        tid = r.json()['ids'][0]
        self.assertEqual(self.stock(self.oil), 10)
        r = self.post(f'/system/stock-transfers/{tid}/cancel/')
        self.assertIn('ok=cancel', r['Location'])
        self.assertEqual(self.stock(self.oil), 15)
        self.assert_books('transfer cancelled')
        # بيع من الفرع التاني من البضاعة اللي وصلت
        r = self.post_json('/system/lightning-pos/checkout/', {
            'branch_id': self.second.pk,
            'payments': [{'treasury_id': self.cash2.pk, 'amount': 300}],
            'items': [{'product_id': self.oil.pk, 'qty': 1, 'price': 300}],
        })
        self.assertEqual(r.status_code, 200, r.content)
        self.assert_books('second branch sale')

        # 13) فاتورة اتعملت غلط ← حذفها
        r = self.post_json('/system/lightning-pos/checkout/', {
            'branch_id': self.main.pk, 'customer_id': customer.pk,
            'payments': [{'treasury_id': self.cash.pk, 'amount': 100}],
            'items': [{'product_id': self.oil.pk, 'qty': 1, 'price': 300}],
        })
        wrong_inv = r.json()['invoice_id']
        self.assert_books('wrong invoice')
        r = self.post(f'/system/invoices/{wrong_inv}/delete/')
        self.assertIn(f'deleted={wrong_inv}', r['Location'])
        self.assertEqual(self.stock(self.oil), 15)
        self.assert_books('invoice delete')

        # 14) تحصيل غلط ← إلغاؤه
        r = self.post_json('/system/lightning-pos/checkout/', {
            'branch_id': self.main.pk, 'customer_id': customer.pk,
            'items': [{'product_id': self.oil.pk, 'qty': 1, 'price': 300}],
        })
        self.assertEqual(r.status_code, 200, r.content)
        r = self.post(f'/system/customers/{customer.pk}/collect/', {'treasury_id': self.cash.pk, 'amount': '300'})
        self.assertIn('ok=collected', r['Location'])
        wrong_col = FinancialTransaction.objects.filter(customer=customer, transaction_type='in').latest('id')
        r = self.post(f'/system/customers/{customer.pk}/payments/{wrong_col.pk}/void/')
        self.assertIn('ok=voided', r['Location'])
        customer.refresh_from_db()
        self.assertEqual(customer.balance, D('300.00'))
        self.assert_books('collection void')

        # 15) كل التقارير والشاشات بتفتح
        for url in ['/system/reports/pnl/', '/system/reports/trial-balance/', '/system/reports/balance-sheet/',
                    '/system/reports/sales/', '/system/reports/customers/',
                    f'/system/reports/customers/{customer.pk}/?period=all', '/system/reports/products/',
                    '/system/reports/services/', '/system/reports/reorder/', '/system/invoices/',
                    '/system/purchases/', '/system/expenses/', '/system/transactions/',
                    f'/system/customers/{customer.pk}/', f'/system/vendors/{vendor.pk}/',
                    f'/system/treasuries/{self.cash.pk}/', '/system/stock-transfers/',
                    '/system/stock-transfers/?status=completed', '/system/products/',
                    f'/system/invoice/{job.pk}/print/a4/', f'/system/invoice/{job.pk}/print/thermal/',
                    '/system/dashboard/', '/system/head-office/']:
            resp = self.get(url)
            self.assertEqual(resp.status_code, 200, f"{url} → {resp.status_code}")

        # قائمة الدخل (الدفتر) = تقرير الأرباح
        self.assertEqual(acct('1100'), D('300.00'))

    # ── collection allocation / void edge cases ───────────────────────
    def _credit_sale(self, customer_phone, amount):
        Inventory.objects.update_or_create(product=self.oil, branch=self.main, defaults={'quantity': 50})
        from inventory.models import InventoryMovement
        InventoryMovement.objects.filter(product=self.oil).delete()
        r = self.post_json('/system/lightning-pos/checkout/', {
            'branch_id': self.main.pk, 'customer_name': 'عميل', 'customer_phone': customer_phone,
            'items': [{'product_id': self.oil.pk, 'qty': 1, 'price': amount}],
        })
        self.assertEqual(r.status_code, 200, r.content)
        return SaleInvoice.objects.get(pk=r.json()['invoice_id'])

    def test_collection_spreads_oldest_first_and_rejects_more_than_the_debt(self):
        first = self._credit_sale('01033334444', 300)
        second = self._credit_sale('01033334444', 500)
        customer = first.customer
        r = self.post(f'/system/customers/{customer.pk}/collect/', {'treasury_id': self.cash.pk, 'amount': '900'})
        self.assertIn('err=over', r['Location'])
        r = self.post(f'/system/customers/{customer.pk}/collect/', {'treasury_id': self.cash.pk, 'amount': '400'})
        self.assertIn('ok=collected', r['Location'])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.paid_amount, second.paid_amount), (D('300.00'), D('100.00')))
        self.assertEqual(FinancialTransaction.objects.filter(customer=customer, sale_invoice__isnull=True).count(), 0)
        # التحصيل على الفاتورة بعد كده مايقدرش يتعدّى اللي فاضل عليها
        r = self.post_json(f'/system/invoices/{second.pk}/pay/', {'treasury_id': self.cash.pk, 'amount': '1000'})
        second.refresh_from_db()
        customer.refresh_from_db()
        self.assertEqual(second.paid_amount, D('500.00'))
        self.assertEqual(customer.balance, D('0.00'))

    def test_void_refuses_when_the_money_already_left_the_till(self):
        inv = self._credit_sale('01055556666', 400)
        self.post(f'/system/customers/{inv.customer.pk}/collect/', {'treasury_id': self.cash.pk, 'amount': '400'})
        col = FinancialTransaction.objects.filter(customer=inv.customer, transaction_type='in').latest('id')
        Treasury.objects.filter(pk=self.cash.pk).update(balance=D('100'))   # الفلوس اتصرفت
        r = self.post(f'/system/customers/{inv.customer.pk}/payments/{col.pk}/void/')
        self.assertIn('err=msg', r['Location'])
        self.assertTrue(FinancialTransaction.objects.filter(pk=col.pk).exists())

    def test_cashier_cannot_void_payments(self):
        inv = self._credit_sale('01077778888', 200)
        self.post(f'/system/customers/{inv.customer.pk}/collect/', {'treasury_id': self.cash.pk, 'amount': '200'})
        col = FinancialTransaction.objects.filter(customer=inv.customer, transaction_type='in').latest('id')
        cashier, _ = make_employee('cashier_e2e', role='cashier', branch=self.main)
        c = Client()
        c.force_login(cashier)
        r = c.post(f'/system/customers/{inv.customer.pk}/payments/{col.pk}/void/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 403)
        self.assertTrue(FinancialTransaction.objects.filter(pk=col.pk).exists())

    # ── viewer role is read-only ──────────────────────────────────────
    def test_viewer_role_cannot_write_anything(self):
        Inventory.objects.create(product=self.oil, branch=self.main, quantity=5)
        viewer, _ = make_employee('auditor_e2e', role='viewer', branch=self.main)
        c = Client()
        c.force_login(viewer)
        r = c.get('/system/products/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        r = c.post('/system/lightning-pos/checkout/', data=json.dumps({
            'branch_id': self.main.pk, 'payments': [{'treasury_id': self.cash.pk, 'amount': 300}],
            'items': [{'product_id': self.oil.pk, 'qty': 1, 'price': 300}]}),
            content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 403)
        r = c.post('/system/expense/create/', {'treasury_id': self.cash.pk, 'amount': '10'}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(SaleInvoice.objects.exists())
        self.assertFalse(FinancialTransaction.objects.exists())

    # ── stock transfers: branch permissions ───────────────────────────
    def test_transfer_receive_belongs_to_the_destination_branch(self):
        Inventory.objects.create(product=self.oil, branch=self.main, quantity=5)
        keeper_main, _ = make_employee('keeper_main', role='stock', branch=self.main)
        keeper_second, _ = make_employee('keeper_second', role='stock', branch=self.second)
        a, b = Client(), Client()
        a.force_login(keeper_main)
        b.force_login(keeper_second)
        r = a.post('/system/stock-transfers/create/', data=json.dumps({
            'to_branch_id': self.second.pk, 'ship': True, 'items': [{'product_id': self.oil.pk, 'qty': 9}]}),
            content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 409)   # رصيد مايكفيش ← ولا حاجة اتحرّكت
        self.assertFalse(StockTransfer.objects.exists())
        r = a.post('/system/stock-transfers/create/', data=json.dumps({
            'to_branch_id': self.second.pk, 'ship': True, 'items': [{'product_id': self.oil.pk, 'qty': 2}]}),
            content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        tid = r.json()['ids'][0]
        r = a.post(f'/system/stock-transfers/{tid}/receive/', HTTP_HOST=self.host)
        self.assertIn('err=perm', r['Location'])         # المصدر مايستلمش
        r = b.post(f'/system/stock-transfers/{tid}/cancel/', HTTP_HOST=self.host)
        self.assertIn('err=perm', r['Location'])         # والوصول مايلغيش
        r = b.post(f'/system/stock-transfers/{tid}/receive/', HTTP_HOST=self.host)
        self.assertIn('ok=receive', r['Location'])
        self.assertEqual(self.stock(self.oil), 3)
        self.assertEqual(self.stock(self.oil, self.second), 2)
        r = b.get('/system/stock-transfers/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context['rows']), 1)

    # ── WhatsApp share ────────────────────────────────────────────────
    def test_whatsapp_share_uses_digits_and_carries_the_feedback_link(self):
        inv = self._credit_sale('01099990000', 300)
        r = self.get(f'/system/invoice/{inv.pk}/share/whatsapp/')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r['Location'].startswith('https://wa.me/201099990000?text='), r['Location'])
        from urllib.parse import unquote
        text = unquote(r['Location'])
        self.assertIn('/feedback/', text)
        walk_in = self.post_json('/system/lightning-pos/checkout/', {
            'branch_id': self.main.pk, 'payments': [{'treasury_id': self.cash.pk, 'amount': 300}],
            'items': [{'product_id': self.oil.pk, 'qty': 1, 'price': 300}]}).json()['invoice_id']
        r = self.get(f'/system/invoice/{walk_in}/share/whatsapp/')
        self.assertEqual(r.status_code, 403)

    # ── settlement discount / small-debt write-off / manual balances ──
    def test_settlement_discount_is_saved_and_survives_a_recompute(self):
        inv = self._credit_sale('01044440000', 300)
        r = self.post_json(f'/system/invoices/{inv.pk}/pay/',
                           {'treasury_id': self.cash.pk, 'amount': '100', 'discount': '20'})
        self.assertEqual(r.status_code, 200, r.content)
        inv.refresh_from_db()
        self.assertEqual((inv.discount, inv.total_amount), (D('20.00'), D('280.00')))
        inv.update_total()          # أي إعادة حساب (مرتجع/كور/مراجعة) مارجعتش الإجمالي القديم
        inv.refresh_from_db()
        self.assertEqual(inv.total_amount, D('280.00'))
        self.assertEqual(acct('1100'), inv.customer.__class__.objects.get(pk=inv.customer_id).balance)

    def test_small_debt_write_off_moves_the_invoice_and_the_ledger(self):
        from inventory.services.treasury_service import TreasuryService
        inv = self._credit_sale('01066660000', 300)
        self.post_json(f'/system/invoices/{inv.pk}/pay/', {'treasury_id': self.cash.pk, 'amount': '285'})
        customer = Customer.objects.get(pk=inv.customer_id)
        self.assertEqual(customer.balance, D('15.00'))
        self.assertEqual(TreasuryService.reconcile_small_debts(Customer.objects.filter(pk=customer.pk)), 1)
        customer.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(customer.balance, D('0.00'))
        self.assertEqual(inv.open_due, D('0.00'))
        self.assertEqual(acct('1100'), D('0.00'))

    def test_manual_customer_and_vendor_balances_are_posted_to_the_ledger(self):
        from django.contrib.admin.sites import site
        from django.test import RequestFactory
        customer = Customer.objects.create(name='رصيد افتتاحي', phone='01088880000')
        vendor = Vendor.objects.create(name='مورد قديم')
        req = RequestFactory().post('/')
        req.user = self.owner
        customer.balance = D('1200')
        site._registry[Customer].save_model(req, customer, None, True)
        vendor.balance = D('5000')
        site._registry[Vendor].save_model(req, vendor, None, True)
        self.assertEqual(acct('1100'), D('1200.00'))
        self.assertEqual(-acct('2100'), D('5000.00'))
        customer.balance = D('1000')                    # تصحيح بالنقص
        site._registry[Customer].save_model(req, customer, None, True)
        self.assertEqual(acct('1100'), D('1000.00'))
        self.assertEqual(books_problems(), [])
        # استيراد Excel بنفس القاعدة
        from inventory.admin.customers import CustomerResource
        import tablib
        data = tablib.Dataset(headers=['id', 'name', 'phone', 'balance'])
        data.append([customer.pk, customer.name, customer.phone, '1500'])
        result = CustomerResource().import_data(data, dry_run=False)
        self.assertFalse(result.has_errors(), [e.error for r in result.row_errors() for e in r[1]])
        self.assertEqual(acct('1100'), D('1500.00'))
