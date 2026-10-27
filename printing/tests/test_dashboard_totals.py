"""
🧾 لوحة المطبعة وتقرير العميل — المتبقّي على العميل = الإجمالي − الخصم + الضريبة − المدفوع.

* الإيراد والمديونية في لوحة التحكم كانوا بيعدّوا الطلبات الملغية، والإيراد
  ماكانش بيشيل الخصم، والمديونية ماكانتش فيها الضريبة.
* تقرير العميل وأعمار الديون كانوا بيحسبوا المتبقّي من غير الضريبة.
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, override_settings

from inventory.admin import dashboard
from inventory.tests.base import ERPTenantTestCase
from printing.models import PrintCustomer, PrintOrder

User = get_user_model()
D = Decimal


@override_settings(ALLOWED_HOSTS=['*'])
class PrintDashboardTotalsTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.customer = PrintCustomer.objects.create(name='عميل مطبعة', phone='01011113333')
        # 1000 − 100 خصم + 126 ضريبة = 1026 مستحق، اتدفع 500 → متبقّي 526
        PrintOrder.objects.create(customer=self.customer, status='confirmed', total_amount=D('1000'),
                                  discount=D('100'), tax_amount=D('126'), paid_amount=D('500'))
        PrintOrder.objects.create(customer=self.customer, status='cancelled', total_amount=D('9000'))
        self.user = User.objects.create_superuser('print_boss', 'b@x.com', 'x')

    def _request(self, **params):
        request = RequestFactory().get('/secure-portal/reports/', params)
        request.user = self.user
        return request

    def test_dashboard_revenue_and_debt(self):
        with mock.patch.object(dashboard, 'original_index', side_effect=lambda r, ctx: ctx):
            ctx = dashboard._printing_dashboard(self._request(), {})
        self.assertEqual(ctx['stats']['total_sales_today'], '900')   # من غير الملغي، بعد الخصم
        self.assertEqual(ctx['stats']['total_debt'], '526')          # شامل الضريبة

    def test_customer_detail_and_debt_aging_include_tax(self):
        response = dashboard._build_reports_response_printing(self._request(customer=self.customer.pk))
        detail = json.loads(response.context_data['customer_detail'])
        self.assertEqual(detail['balance'], 526.0)
        self.assertEqual([o['due'] for o in detail['invoices']], [526.0])
        aging = json.loads(response.context_data['debt_aging'])
        self.assertEqual(aging[0]['total_debt'], 526.0)
