"""🔐 الدفاتر المحاسبية (ميزان المراجعة / الميزانية / اليومية) للمحاسب والمدير بس.

🐛 _can_view_books كان بيسمح لأي is_staff — وكل موظفين المطبعة is_staff (عشان يدخلوا
الأدمن)، فالاستقبال والإنتاج والمصمم كانوا بيشوفوا كل فلوس الشركة. (اتلقت من زحف
الصفحات بحساب كل دور.)
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings

from inventory.tests.base import ERPTenantTestCase
from printing.views.staff import apply_role

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['*'])
class RoleBooksAccessTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(industry='printing')
        self.host = self.domain.domain

    def _client(self, role):
        u = User.objects.create_user(f'books_{role}', f'{role}@books.x', 'x', is_staff=True)
        apply_role(u, role)
        c = Client()
        c.force_login(u)
        return c

    def test_books_only_for_accountant_and_manager(self):
        from printing.models import PrintMaterial
        mat = PrintMaterial.objects.create(name='ورق', quantity=Decimal('5'), cost_per_unit=Decimal('1'))
        for role, allowed in (('reception', False), ('production', False), ('designer', False),
                              ('accountant', True), ('manager', True)):
            c = self._client(role)
            for url in ('/printing/accounting/', '/printing/accounting/?tab=sheet',
                        f'/printing/material/{mat.pk}/card/'):
                code = c.get(url, HTTP_HOST=self.host).status_code
                self.assertEqual(code, 200 if allowed else 403, f'{role} {url} → {code}')
            html = c.get('/secure-portal/', HTTP_HOST=self.host).content.decode()
            self.assertEqual('/printing/accounting/?tab=aging' in html, allowed, f'{role}: لينك أعمار الديون في القايمة')

    def test_stock_manager_flag_opens_the_stock_card(self):
        from printing.models import PrintMaterial, StaffPermission
        mat = PrintMaterial.objects.create(name='حبر', quantity=Decimal('2'), cost_per_unit=Decimal('3'))
        c = self._client('production')
        StaffPermission.objects.filter(user__username='books_production').update(can_manage_stock=True)
        self.assertEqual(c.get(f'/printing/material/{mat.pk}/card/', HTTP_HOST=self.host).status_code, 200)
        self.assertEqual(c.get('/printing/accounting/', HTTP_HOST=self.host).status_code, 403)

    def test_full_invoice_only_for_sales_and_money_roles(self):
        """الإنتاج والمصمم بياخدوا أمر الشغل (من غير أسعار) — مش الفاتورة بالدفعات."""
        from printing.models import PrintCustomer, PrintJob, PrintOrder
        cu = PrintCustomer.objects.create(name='عميل الفاتورة', phone='01077778888')
        order = PrintOrder.objects.create(customer=cu, status='confirmed')
        PrintJob.objects.create(order=order, description='استيكر سري', quantity=1, unit_price=Decimal('987'))
        url = f'/printing/order/{order.pk}/invoice/'
        for role, full in (('reception', True), ('accountant', True), ('manager', True),
                           ('production', False), ('designer', False)):
            c = self._client(f'{role}')
            r = c.get(url, HTTP_HOST=self.host)
            if full:
                self.assertEqual(r.status_code, 200, role)
                self.assertContains(r, '987')
            else:
                self.assertEqual(r.status_code, 302, role)
                self.assertTrue(r['Location'].endswith('?ticket=1'), role)
                t = c.get(url + '?ticket=1', HTTP_HOST=self.host)
                self.assertEqual(t.status_code, 200, role)
                self.assertContains(t, 'استيكر سري')
                self.assertNotContains(t, '987')
            User.objects.filter(username=f'books_{role}').delete()
        stranger = User.objects.create_user('books_nobody', 'n@books.x', 'x', is_staff=True)
        c = Client()
        c.force_login(stranger)
        self.assertEqual(c.get(url + '?ticket=1', HTTP_HOST=self.host).status_code, 403)
