"""
🖨️ مراجعة شاملة لسيستم المطابع — اتجرّب كأنه مطبعة شغالة من أول العرض لحد التسليم.

* موظفين المطبعة (غير المالك) كانوا بيلفّوا /secure-portal/ ⇄ /auth/redirect/ ومحدش يدخل.
* «طلب طباعة جديد» كان مطلوب فيه رقم الطلب بالإيد، و«عرض سعر جديد» كان مطلوب فيه
  «بواسطة» في قسم مقفول — فالاتنين مابيتحفظوش.
* رابط عرض السعر للعميل كان بيقع (500) لو العرض مالوش منشئ، وكان باسم Mouss Tec مش المطبعة.
* حذف طلب عليه دفعات كان بيسيب الفلوس في الخزنة من غير طلب (تختفي من كشف الحساب)،
  والإلغاء ماكانش بيرجّع المقدّم.
* حالة الطلب ماكانتش بتتحرك مع الإنتاج، و«جاهز للتسليم» ساقط من عدّاد اللوحة.
* صلاحيات الموظف (StaffPermission) كانت بتتجاهل لأي حد staff.
* مفيش فاتورة/إيصال ولا أمر شغل للطلب.
"""
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone

from inventory.tests.base import ERPTenantTestCase
from printing.admin import cancel_order_and_refund
from printing.models import (
    PriceQuotation, PrintCustomer, PrintJob, PrintJobMaterial, PrintMaterial,
    PrintOrder, PrintTransaction, PrintTreasury, QuotationLine, StaffPermission,
)
from printing.views.finance import _can_see_money, convert_quotation

User = get_user_model()
D = Decimal


def _mgmt(prefix, total=0):
    return {f'{prefix}-TOTAL_FORMS': str(total), f'{prefix}-INITIAL_FORMS': '0',
            f'{prefix}-MIN_NUM_FORMS': '0', f'{prefix}-MAX_NUM_FORMS': '1000'}


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingReviewTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self._as_printing_tenant()   # شركة مطبعة (مش سيارات)
        self.boss = User.objects.create_superuser('print_owner', 'o@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.boss)
        self.customer = PrintCustomer.objects.create(name='مكتبة النور', phone='01099990000')
        self.treasury = PrintTreasury.objects.create(name='الخزنة')

    def _as_printing_tenant(self):
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')

    def _paid(self, order, amount, kind='in'):
        return PrintTransaction.objects.create(treasury=self.treasury, transaction_type=kind,
                                               amount=D(amount), order=order, description='دفعة')

    # ───────────────────────────────────────────── forms that wouldn't save
    def test_new_order_from_admin_gets_its_number(self):
        data = {'customer': self.customer.pk, 'status': 'confirmed', 'total_amount': '0',
                'discount': '0', 'tax_amount': '0', **_mgmt('jobs', 1), **_mgmt('printtransaction_set'),
                'jobs-0-description': 'ستيكر', 'jobs-0-quantity': '100', 'jobs-0-copies': '1',
                'jobs-0-unit_price': '1.5', 'jobs-0-paper_size': 'a4'}
        r = self.c.post('/secure-portal/printing/printorder/add/', data, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302, r.content[:3000])
        order = PrintOrder.objects.get(customer=self.customer)
        self.assertTrue(order.order_number.startswith('PO-'))
        self.assertEqual(order.total_amount, D('150.00'))

    def test_new_quotation_from_admin_saves_with_its_creator(self):
        data = {'customer_name': 'أستوديو الفن', 'customer_phone': '01277776666', 'title': 'فلايرات',
                'discount': '0', 'tax_percent': '0', 'status': 'draft', **_mgmt('lines', 1),
                'lines-0-description': 'فلاير', 'lines-0-quantity': '1000', 'lines-0-unit_price': '0.8',
                'lines-0-sort_order': '0'}
        r = self.c.post('/secure-portal/printing/pricequotation/add/', data, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302, r.content[:3000])
        quote = PriceQuotation.objects.get(title='فلايرات')
        self.assertEqual((quote.created_by, quote.total), (self.boss, D('800.00')))

    def test_discount_bigger_than_total_is_refused(self):
        order = PrintOrder.objects.create(customer=self.customer, total_amount=D('100'))
        data = {'customer': self.customer.pk, 'status': 'confirmed', 'total_amount': '100',
                'discount': '150', 'tax_amount': '0', **_mgmt('jobs'), **_mgmt('printtransaction_set')}
        r = self.c.post(f'/secure-portal/printing/printorder/{order.pk}/change/', data, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.discount, D('0'))

    # ───────────────────────────────────────────── quotation for the customer
    def test_public_quotation_without_creator_and_staff_preview(self):
        quote = PriceQuotation.objects.create(customer_name='زبون', title='كروت')
        QuotationLine.objects.create(quotation=quote, description='كروت', quantity=1, unit_price=D('300'))
        url = f'/printing/quotation/view/{quote.share_token}/'

        self.c.get(url, HTTP_HOST=self.host)               # الموظف بيعاين
        quote.refresh_from_db()
        self.assertEqual(quote.status, 'draft')

        r = Client().get(url, HTTP_HOST=self.host)        # العميل فتح الرابط
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.tenant.name)
        quote.refresh_from_db()
        self.assertEqual(quote.status, 'sent')

        r = Client().get(url, HTTP_HOST=self.host, HTTP_ACCEPT_LANGUAGE='en')
        self.assertContains(r, 'Accept quotation')

    def test_converting_a_quote_registers_its_customer(self):
        quote = PriceQuotation.objects.create(customer_name='أستوديو الفن', customer_phone='01277776666',
                                              title='مطبوعات', status='accepted')
        QuotationLine.objects.create(quotation=quote, description='فلاير', quantity=500, unit_price=D('1'))
        r = self.c.post(f'/secure-portal/printing/pricequotation/{quote.pk}/convert/', HTTP_HOST=self.host)
        quote.refresh_from_db()
        self.assertEqual(quote.status, 'converted')
        self.assertRedirects(r, f'/secure-portal/printing/printorder/{quote.converted_order_id}/change/',
                             fetch_redirect_response=False)
        self.assertEqual((quote.customer.name, quote.customer.phone), ('أستوديو الفن', '01277776666'))

        # عرض تاني لنفس التليفون مايكرّرش العميل
        again = PriceQuotation.objects.create(customer_name='الفن', customer_phone='01277776666',
                                              title='تاني', status='accepted')
        QuotationLine.objects.create(quotation=again, description='x', quantity=1, unit_price=D('10'))
        convert_quotation(again)
        self.assertEqual(PrintCustomer.objects.filter(phone='01277776666').count(), 1)

    # ───────────────────────────────────────────── production moves the order
    def test_order_status_follows_its_jobs_forward_only(self):
        order = PrintOrder.objects.create(customer=self.customer, status='confirmed')
        j1 = PrintJob.objects.create(order=order, description='كروت', quantity=1, unit_price=D('100'))
        j2 = PrintJob.objects.create(order=order, description='بنر', quantity=1, unit_price=D('200'))
        paper = PrintMaterial.objects.create(name='ورق', quantity=D('10'), cost_per_unit=D('5'))

        PrintJobMaterial.objects.create(job=j1, material=paper, quantity=D('2'))
        order.refresh_from_db()
        self.assertEqual(order.status, 'in_progress')

        j1.is_complete = True
        j1.save()
        j2.is_complete = True
        j2.save()
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')

        PrintOrder.objects.filter(pk=order.pk).update(status='delivered')
        j2.save()
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')   # مابيرجعش لورا

    # ───────────────────────────────────────────── money on delete / cancel
    def test_order_with_payments_cannot_be_deleted(self):
        paid = PrintOrder.objects.create(customer=self.customer, total_amount=D('200'))
        self._paid(paid, '50')
        self.c.post(f'/secure-portal/printing/printorder/{paid.pk}/delete/', {'post': 'yes'}, HTTP_HOST=self.host)
        self.assertTrue(PrintOrder.objects.filter(pk=paid.pk).exists())

        empty = PrintOrder.objects.create(customer=self.customer, total_amount=D('200'))
        self.c.post(f'/secure-portal/printing/printorder/{empty.pk}/delete/', {'post': 'yes'}, HTTP_HOST=self.host)
        self.assertFalse(PrintOrder.objects.filter(pk=empty.pk).exists())

    def test_cancel_and_refund_pays_back_from_the_same_treasury(self):
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='in', amount=D('1000'))
        order = PrintOrder.objects.create(customer=self.customer, status='confirmed', total_amount=D('600'))
        self._paid(order, '300')
        self._paid(order, '50', kind='out')
        self.c.post('/secure-portal/printing/printorder/',
                    {'action': 'cancel_and_refund', '_selected_action': [order.pk]}, HTTP_HOST=self.host)
        order.refresh_from_db()
        self.assertEqual((order.status, order.paid_amount), ('cancelled', D('0')))
        self.assertEqual(PrintTreasury.objects.get(pk=self.treasury.pk).balance, D('1000'))

    def test_refund_bigger_than_the_treasury_changes_nothing(self):
        order = PrintOrder.objects.create(customer=self.customer, status='confirmed', total_amount=D('600'))
        self._paid(order, '300')
        PrintTransaction.objects.create(treasury=self.treasury, transaction_type='out', amount=D('300'))
        with self.assertRaises(ValueError):
            cancel_order_and_refund(order)
        order.refresh_from_db()
        self.assertEqual((order.status, order.paid_amount), ('confirmed', D('300')))

    # ───────────────────────────────────────────── documents
    def test_invoice_and_job_ticket(self):
        order = PrintOrder.objects.create(customer=self.customer, status='ready', discount=D('10'))
        PrintJob.objects.create(order=order, description='كروت بزنس', quantity=500, unit_price=D('2'))
        self._paid(order, '400')
        r = self.c.get(f'/printing/order/{order.pk}/invoice/', HTTP_HOST=self.host)
        self.assertContains(r, order.order_number)
        self.assertContains(r, '990.00')    # 1000 − 10
        self.assertContains(r, '590.00')    # المتبقي
        r = self.c.get(f'/printing/order/{order.pk}/invoice/?ticket=1', HTTP_HOST=self.host)
        self.assertContains(r, 'كروت بزنس')
        self.assertNotContains(r, '990.00')   # أمر الشغل من غير أسعار
        page = self.c.get(f'/secure-portal/printing/printorder/{order.pk}/change/', HTTP_HOST=self.host)
        self.assertContains(page, 'https://wa.me/201099990000')

    # ───────────────────────────────────────────── employees
    def test_printing_employee_reaches_the_dashboard(self):
        staff = User.objects.create_user('print_cashier', password='x', is_staff=True)
        c = Client()
        c.force_login(staff)
        r = c.get('/secure-portal/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context['print_show_money'])          # staff من غير صلاحيات متضبطة

        StaffPermission.objects.create(user=staff, can_view_treasury=False, can_view_profits=False)
        r = c.get('/secure-portal/', HTTP_HOST=self.host)
        self.assertFalse(r.context['print_show_money'])         # المالك قفلها عليه

    def test_staff_permission_flags_are_respected(self):
        staff = User.objects.create_user('print_sales', password='x', is_staff=True)
        self.assertTrue(_can_see_money(staff, 'can_view_profits'))
        StaffPermission.objects.create(user=staff, can_view_profits=False, can_view_reports=True)
        staff = User.objects.get(pk=staff.pk)
        self.assertFalse(_can_see_money(staff, 'can_view_profits'))
        self.assertTrue(_can_see_money(staff, 'can_view_reports'))
        self.assertTrue(_can_see_money(self.boss, 'can_view_profits'))

    def test_dashboard_counts_ready_and_late_orders(self):
        PrintOrder.objects.create(customer=self.customer, status='ready')
        PrintOrder.objects.create(customer=self.customer, status='in_progress',
                                  date_due=timezone.now() - timedelta(days=1))
        PrintOrder.objects.create(customer=self.customer, status='delivered',
                                  date_due=timezone.now() - timedelta(days=1))
        from inventory.admin import dashboard
        from django.test import RequestFactory
        request = RequestFactory().get('/secure-portal/')
        request.user = self.boss
        with mock.patch.object(dashboard, 'original_index', side_effect=lambda r, ctx: ctx):
            ctx = dashboard._printing_dashboard(request, {})
        self.assertEqual((ctx['print_pending'], ctx['delayed_orders_count']), (2, 1))
        labels = json.loads(ctx['chart_labels'])
        self.assertEqual(len(set(labels)), 6)    # ستة شهور ورا بعض من غير تكرار

    def test_printing_dashboard_route_goes_to_the_printing_home(self):
        r = self.c.get('/printing/dashboard/', HTTP_HOST=self.host)
        self.assertRedirects(r, '/secure-portal/', fetch_redirect_response=False)


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingStaffTests(ERPTenantTestCase):
    """👥 لينك «الموظفون والصلاحيات» كان 404 للمطابع — دلوقتي صفحة بأدوار جاهزة."""

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.boss = User.objects.create_superuser('print_owner2', 'o2@x.com', 'x')
        self.c = Client()
        self.c.force_login(self.boss)

    def test_sidebar_link_lands_on_the_printing_staff_page(self):
        r = self.c.get('/staff/', HTTP_HOST=self.host)
        self.assertRedirects(r, '/printing/staff/', fetch_redirect_response=False)
        self.assertEqual(self.c.get('/printing/staff/', HTTP_HOST=self.host).status_code, 200)

    def test_owner_adds_a_reception_employee_who_can_work_but_not_see_money(self):
        r = self.c.post('/printing/staff/add/', {'full_name': 'منى علي', 'email': 'Mona@Shop.com',
                                                'role': 'reception'}, HTTP_HOST=self.host)
        self.assertContains(r, '/account/set-password/?token=')
        mona = User.objects.get(email='mona@shop.com')
        self.assertTrue(mona.is_staff and not mona.is_superuser and not mona.has_usable_password())
        self.assertTrue(mona.has_perm('printing.add_printorder'))
        self.assertFalse(mona.has_perm('printing.change_printtreasury'))
        self.assertFalse(_can_see_money(mona, 'can_view_treasury'))

        mona.set_password('x')
        mona.save()
        c = Client()
        c.force_login(mona)
        r = c.get('/secure-portal/', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context['print_show_money'])
        self.assertEqual(c.get('/secure-portal/printing/printorder/add/', HTTP_HOST=self.host).status_code, 200)
        self.assertEqual(c.get('/printing/staff/', HTTP_HOST=self.host).status_code, 403)

    def test_owner_changes_role_and_flags_and_can_stop_an_employee(self):
        self.c.post('/printing/staff/add/', {'full_name': 'كريم', 'email': 'karim@shop.com', 'role': 'production'},
                    HTTP_HOST=self.host)
        karim = User.objects.get(email='karim@shop.com')
        self.c.post(f'/printing/staff/{karim.pk}/', {'role': 'accountant'}, HTTP_HOST=self.host)
        karim = User.objects.get(pk=karim.pk)
        self.assertTrue(_can_see_money(karim, 'can_view_treasury'))
        self.assertTrue(karim.has_perm('printing.add_printtransaction'))

        self.c.post(f'/printing/staff/{karim.pk}/', {'role': 'accountant', 'can_view_reports': 'on'},
                    HTTP_HOST=self.host)
        karim = User.objects.get(pk=karim.pk)
        self.assertFalse(_can_see_money(karim, 'can_view_treasury'))   # اتشالت
        self.assertTrue(_can_see_money(karim, 'can_view_reports'))

        self.c.post(f'/printing/staff/{karim.pk}/', {'action': 'toggle_active'}, HTTP_HOST=self.host)
        self.assertFalse(User.objects.get(pk=karim.pk).is_active)

    def test_duplicate_email_is_refused(self):
        self.c.post('/printing/staff/add/', {'full_name': 'أ', 'email': 'a@shop.com', 'role': 'designer'},
                    HTTP_HOST=self.host)
        r = self.c.post('/printing/staff/add/', {'full_name': 'ب', 'email': 'A@shop.com', 'role': 'designer'},
                        HTTP_HOST=self.host)
        self.assertContains(r, 'نفس الإيميل')
        self.assertEqual(User.objects.filter(email__iexact='a@shop.com').count(), 1)

    def test_edit_page_renders(self):
        self.c.post('/printing/staff/add/', {'full_name': 'سارة', 'email': 'sara@shop.com', 'role': 'designer'},
                    HTTP_HOST=self.host)
        sara = User.objects.get(email='sara@shop.com')
        r = self.c.get(f'/printing/staff/{sara.pk}/', HTTP_HOST=self.host)
        self.assertContains(r, 'can_use_ai_studio')


@override_settings(ALLOWED_HOSTS=['*'])
class PrintingCostAndPaymentsTests(ERPTenantTestCase):
    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.customer = PrintCustomer.objects.create(name='عميل', phone='01011110000')

    def test_report_cost_counts_jobs_still_in_production(self):
        """الطلب اللي في الإنتاج كان ربحه في التقرير 100% (تكلفة المكتمل بس)."""
        from inventory.admin.dashboard import _print_jobs_cost
        from printing.models import MachineProfile
        machine = MachineProfile.objects.create(name='ديجيتال', power_consumption_kwh=D('2'),
                                                electricity_rate_per_kwh=D('2.5'), hourly_labor_cost=D('45'))
        order = PrintOrder.objects.create(customer=self.customer, status='in_progress')
        job = PrintJob.objects.create(order=order, description='بنر', quantity=1, unit_price=D('500'),
                                      machine=machine, actual_time_hours=D('2'))
        paper = PrintMaterial.objects.create(name='فليكس', quantity=D('10'), cost_per_unit=D('30'))
        PrintJobMaterial.objects.create(job=job, material=paper, quantity=D('3'))
        jobs = PrintJob.objects.filter(order=order)
        self.assertEqual(_print_jobs_cost(jobs), float(order.total_cost))          # 100 + 90 = 190
        self.assertEqual(_print_jobs_cost(jobs), 190.0)
        job.is_complete = True
        job.save()
        self.assertEqual(_print_jobs_cost(jobs), 190.0)                             # اللقطة نفس الرقم
        machine.refresh_from_db()
        self.assertEqual(machine.total_print_hours, D('2.00'))                      # ساعات الماكينة اتزوّدت

    def test_quick_income_links_to_its_order(self):
        """التحصيل السريع من لوحة المصمم ماكانش بيتربط بالطلب ⇒ العميل يفضل «عليه»."""
        boss = User.objects.create_superuser('print_owner3', 'o3@x.com', 'x')
        c = Client()
        c.force_login(boss)
        treasury = PrintTreasury.objects.create(name='الخزنة')
        order = PrintOrder.objects.create(customer=self.customer, status='ready', total_amount=D('400'))
        r = c.post('/hr/api/treasury/quick-transaction/', json.dumps({
            'treasury_id': treasury.pk, 'amount': 400, 'description': 'تحصيل', 'transaction_type': 'in',
            'order_number': order.order_number}), content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        order.refresh_from_db()
        self.assertEqual(order.paid_amount, D('400'))
        r = c.post('/hr/api/treasury/quick-transaction/', json.dumps({
            'treasury_id': treasury.pk, 'amount': 5, 'description': 'x', 'transaction_type': 'in',
            'order_number': 'PO-000000-999'}), content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 404)
