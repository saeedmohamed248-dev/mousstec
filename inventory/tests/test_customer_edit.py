"""✏️ تعديل بيانات العميل من البوابة (الاسم/الموبايل/الضريبي/العنوان) وحد الائتمان للإدارة المالية بس."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse

from inventory.models import Customer
from .base import ERPTenantTestCase
from .factories import make_branch, make_customer, make_employee


@override_settings(ALLOWED_HOSTS=['*'])
class CustomerEditTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.cust = make_customer('شركة النيل', phone='01012345678')
        self.boss = User.objects.create_user('ce_boss', password='x', is_staff=True, is_superuser=True)

    def post(self, user, **data):
        c = Client()
        c.force_login(user)
        payload = {'name': 'شركة النيل للتجارة', 'phone': '01012345678', 'tax_id': '123456789',
                   'national_id': '', 'eta_governate': 'القاهرة', 'eta_region_city': 'شبرا',
                   'eta_street': 'شارع الترعة', 'eta_building_number': '7', 'credit_limit': '5000'}
        payload.update(data)
        return c.post(reverse('inventory:customer_edit', args=[self.cust.pk]), payload, HTTP_HOST=self.host)

    def test_manager_edits_details_and_credit_limit(self):
        r = self.post(self.boss)
        self.assertIn('ok=edited', r['Location'])
        self.cust.refresh_from_db()
        self.assertEqual(self.cust.name, 'شركة النيل للتجارة')
        self.assertEqual(self.cust.credit_limit, Decimal('5000.00'))
        self.assertEqual((self.cust.tax_id, self.cust.eta_street), ('123456789', 'شارع الترعة'))
        page = Client()
        page.force_login(self.boss)
        r = page.get(reverse('inventory:customer_detail', args=[self.cust.pk]), HTTP_HOST=self.host)
        self.assertContains(r, 'customer-edit')
        self.assertContains(r, 'حد الائتمان')

    def test_duplicate_phone_and_bad_limit_are_refused(self):
        make_customer('عميل تاني', phone='01099998888')
        r = self.post(self.boss, phone='01099998888')
        self.assertIn('err=msg', r['Location'])
        r = self.post(self.boss, credit_limit='-5')
        self.assertIn('err=msg', r['Location'])
        self.cust.refresh_from_db()
        self.assertEqual((self.cust.name, self.cust.credit_limit), ('شركة النيل', Decimal('0')))

    def test_cashier_cannot_edit_and_walk_in_is_locked(self):
        cashier, _p = make_employee('ce_cashier', role='cashier', branch=self.branch)
        r = self.post(cashier)
        self.assertIn('err=perm', r['Location'])
        walk_in = Customer.objects.create(name='عميل نقدي', phone='0000000000')
        c = Client()
        c.force_login(self.boss)
        r = c.post(reverse('inventory:customer_edit', args=[walk_in.pk]), {'name': 'x', 'phone': '0100'},
                   HTTP_HOST=self.host)
        self.assertIn('err=perm', r['Location'])
        walk_in.refresh_from_db()
        self.assertEqual(walk_in.name, 'عميل نقدي')
