"""
🧾 شاشة «طلب جديد» للمطابع: «+ طلب جديد» بيفتح الفاتورة السريعة، والفاتورة السريعة فيها
الضريبة نسبة + موعد التسليم + الملفات، والشاشة التفصيلية من غير خانات فاضية وبنسبة ضريبة.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from inventory.tests.base import ERPTenantTestCase
from printing.models import PrintBranch, PrintCustomer, PrintOrder, PrintTreasury

User = get_user_model()
D = Decimal
ADD = '/secure-portal/printing/printorder/add/'


@override_settings(ALLOWED_HOSTS=['*'])
class OrderFormUXTests(ERPTenantTestCase):
    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.host = self.domain.domain
        self.treasury = PrintTreasury.objects.create(name='الخزنة')
        self.user = User.objects.create_superuser('form_boss', 'f@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.user)

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def quick(self, **data):
        payload = {'customer_name': 'شركة الندى', 'customer_phone': '01011112222', 'customer_id': '',
                   'line_desc': ['كروت'], 'line_qty': ['1'], 'line_price': ['1000'], 'discount': '100',
                   'delivered': '1', 'paid': '0', 'treasury': str(self.treasury.pk), 'notes': ''}
        payload.update(data)
        return self.c.post(reverse('printing:quick_invoice'), payload, HTTP_HOST=self.host)

    # ── «+ طلب جديد» ⇒ الفاتورة السريعة ───────────────────────────────────
    def test_add_opens_quick_invoice_and_full_form_still_reachable(self):
        r = self.c.get(ADD, HTTP_HOST=self.host)
        self.assertRedirects(r, reverse('printing:quick_invoice'), fetch_redirect_response=False)
        cust = PrintCustomer.objects.create(name='منى', phone='01000000001')
        r = self.c.get(f'{ADD}?customer={cust.pk}', HTTP_HOST=self.host)
        self.assertEqual(r['Location'], reverse('printing:quick_invoice') + f'?customer={cust.pk}')
        self.assertEqual(self.c.get(f'{ADD}?full=1', HTTP_HOST=self.host).status_code, 200)
        self.assertContains(self.c.get(reverse('printing:quick_invoice'), HTTP_HOST=self.host), f'{ADD}?full=1')

    def test_full_form_hides_empty_fields_and_preselects_the_only_branch(self):
        branch = PrintBranch.objects.create(name='الفرع الرئيسي')
        r = self.c.get(f'{ADD}?full=1', HTTP_HOST=self.host)
        form = r.context['adminform'].form
        for name in ('order_number', 'date_delivered', 'order_actions', 'paid_amount'):
            self.assertNotIn(name, str(r.context['adminform'].fieldsets), name)
        self.assertFalse(form.fields['total_amount'].required)
        self.assertEqual(form.initial.get('branch'), branch.pk)
        self.assertIn('tax_rate', form.fields)

    def test_full_form_tax_rate_becomes_the_tax_amount(self):
        cust = PrintCustomer.objects.create(name='منى', phone='01000000001')
        data = {'customer': cust.pk, 'status': 'draft', 'total_amount': '', 'discount': '100', 'tax_amount': '0',
                'tax_rate': '14', 'notes': ''}
        for prefix in ('jobs', 'printtransaction_set'):
            data.update({f'{prefix}-TOTAL_FORMS': '0', f'{prefix}-INITIAL_FORMS': '0',
                         f'{prefix}-MIN_NUM_FORMS': '0', f'{prefix}-MAX_NUM_FORMS': '1000'})
        data.update({'jobs-TOTAL_FORMS': '1', 'jobs-0-description': 'كروت', 'jobs-0-quantity': '1',
                     'jobs-0-copies': '1', 'jobs-0-unit_price': '1000'})
        r = self.c.post(f'{ADD}?full=1', data, HTTP_HOST=self.host)
        self.assertIn(r.status_code, (302,), getattr(r, 'context', None) and r.context['adminform'].form.errors)
        order = PrintOrder.objects.get()
        self.assertEqual((order.total_amount, order.tax_amount, order.net_total), (D('1000.00'), D('126.00'), D('1026.00')))

    # ── الفاتورة السريعة: ضريبة ٪ + موعد تسليم + ملفات ──────────────────
    def test_quick_invoice_tax_rate_on_net_after_discount(self):
        self.quick(tax_rate='14', paid='1026')
        order = PrintOrder.objects.get()
        self.assertEqual((order.tax_amount, order.net_total, order.remaining), (D('126.00'), D('1026.00'), D('0.00')))

    def test_quick_invoice_paid_above_net_with_tax_is_rejected(self):
        r = self.quick(tax_rate='14', paid='1027')
        self.assertContains(r, 'المدفوع أكبر من قيمة الفاتورة')
        self.assertFalse(PrintOrder.objects.exists())

    def test_quick_invoice_due_date_for_work_in_progress(self):
        day = timezone.localdate() + timedelta(days=3)
        self.quick(delivered='0', due_date=day.isoformat())
        order = PrintOrder.objects.get()
        self.assertEqual(timezone.localtime(order.date_due).date(), day)
        r = self.quick(delivered='0', due_date=(timezone.localdate() - timedelta(days=1)).isoformat())
        self.assertContains(r, 'موعد التسليم مينفعش يبقى فات')

    def test_quick_invoice_uploads_design_files(self):
        files = [SimpleUploadedFile(f'design{i}.pdf', b'%PDF-1.4 x', content_type='application/pdf') for i in range(2)]
        self.quick(files=files)
        order = PrintOrder.objects.get()
        self.assertTrue(order.project_file.name.endswith('.pdf'))
        self.assertTrue(order.project_file_2.name)
        self.assertFalse(order.project_file_3)
        r = self.quick(files=[SimpleUploadedFile(f'f{i}.pdf', b'x') for i in range(4)])
        self.assertContains(r, 'أقصى عدد ملفات')
