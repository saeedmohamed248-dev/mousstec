"""👥 صفحة العملاء والآجل بتتفلتر بالفرع المختار من المبدّل.

قبل كده آجل فرع (مثلاً مصر) كان بيظهر وانت مختار فرع تاني (الإمارات)، لأن الرصيد
على مستوى الشركة. دلوقتي: فرع نشط = عملاء الفرع ومتبقّي فواتيره بس.
"""
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings

from inventory.models import Customer, SaleInvoice
from inventory.tests.base import ERPTenantTestCase

from .factories import make_branch, make_customer

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['*'])
class CustomersBranchScopeTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        type(self.tenant).objects.filter(pk=self.tenant.pk).update(max_branches=50)
        self.tenant.max_branches = 50
        self.host = self.domain.domain
        self.egypt = make_branch(name='مصر')
        self.uae = make_branch(name='الامارات')
        self.ali = make_customer(name='علاء')
        self.omar = make_customer(name='عمر', phone='01200000000')
        self._due(self.ali, self.egypt, '1500')
        self._due(self.omar, self.uae, '700')
        self._due(self.omar, self.egypt, '300')
        Customer.objects.filter(pk=self.ali.pk).update(balance=D('1500'))
        Customer.objects.filter(pk=self.omar.pk).update(balance=D('1000'))
        self.c = Client()
        self.c.force_login(User.objects.create_user('cb_boss', password='x', is_staff=True, is_superuser=True))

    def _due(self, customer, branch, amount):
        inv = SaleInvoice.objects.create(customer=customer, branch=branch, status='posted')
        SaleInvoice.objects.filter(pk=inv.pk).update(total_amount=D(amount), paid_amount=D('0'))

    def _rows(self, set_branch):
        r = self.c.get(f'/system/customers/?set_branch={set_branch}', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200)
        return {c.name: c.balance for c in r.context['page'].object_list}, r.context['total_debt']

    def test_branch_shows_only_its_customers_and_dues(self):
        rows, total = self._rows(self.uae.pk)
        self.assertEqual(rows, {'عمر': D('700')})          # علاء آجله في مصر بس
        self.assertEqual(total, D('700'))
        rows, total = self._rows(self.egypt.pk)
        self.assertEqual(rows, {'علاء': D('1500'), 'عمر': D('300')})
        self.assertEqual(total, D('1800'))

    def test_all_branches_keeps_company_balance(self):
        rows, total = self._rows(0)
        self.assertEqual(rows, {'علاء': D('1500'), 'عمر': D('1000')})
        self.assertEqual(total, D('2500'))
