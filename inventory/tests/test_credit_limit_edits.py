"""
💳 حد الائتمان في تعديل الفاتورة بعد إنشائها:
* «تعديل الدفعات» (/system/invoices/<pk>/edit/) لو شال دفعة فزوّد الآجل فوق الحد ⇒ يترفض.
* «مراجعة كارت الصيانة» لو رجّع سطر مستبعد فزوّد الآجل فوق الحد ⇒ يترفض.
وفي الحالتين مفيش ولا حاجة بتتغيّر (الدفعات/السطور/رصيد العميل).
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import SaleInvoice, ServiceCatalog
from .base import ERPTenantTestCase
from .factories import make_branch, make_customer, make_inventory, make_product, make_treasury

D = Decimal


@override_settings(ALLOWED_HOSTS=['*'])
class CreditLimitOnEditsTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.till = make_treasury(self.branch, balance='5000.00')
        self.part = make_product(part_number='CL-PART', retail_price='200.00', average_cost='100.00')
        make_inventory(self.part, self.branch, 20)
        self.cust = make_customer('شركة الحد', phone='01044445555', credit_limit=D('150.00'))
        boss = User.objects.create_user('cl_boss', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(boss)

    def post_json(self, url, data):
        return self.c.post(url, json.dumps(data), content_type='application/json',
                           HTTP_ACCEPT='application/json', HTTP_HOST=self.host)

    def test_removing_a_payment_over_the_limit_is_refused(self):
        r = self.post_json('/system/lightning-pos/checkout/', {
            'branch_id': self.branch.pk, 'customer_id': self.cust.pk,
            'items': [{'product_id': self.part.pk, 'qty': 1, 'price': 200}],
            'treasury_id': self.till.pk, 'paid_amount': 200})
        self.assertEqual(r.status_code, 200, r.content)
        inv = SaleInvoice.objects.get(pk=r.json()['invoice_id'])

        # الدفعات تبقى 0 ⇒ آجل 200 > الحد 150
        r = self.post_json(f'/system/invoices/{inv.pk}/edit/', {'payments': []})
        self.assertEqual(r.status_code, 400)
        self.assertIn('حد الائتمان', r.json()['error'])
        inv.refresh_from_db()
        self.cust.refresh_from_db()
        self.till.refresh_from_db()
        self.assertEqual(inv.paid_amount, D('200.00'))
        self.assertEqual(self.cust.balance, D('0.00'))
        self.assertEqual(self.till.balance, D('5200.00'))

        # دفعة 100 ⇒ آجل 100 ≤ 150 مسموح
        r = self.post_json(f'/system/invoices/{inv.pk}/edit/',
                           {'payments': [{'treasury_id': self.till.pk, 'amount': '100'}]})
        self.assertEqual(r.status_code, 200, r.content)
        self.cust.refresh_from_db()
        self.assertEqual(self.cust.balance, D('100.00'))

    def test_review_re_billing_a_line_over_the_limit_is_refused(self):
        svc = ServiceCatalog.objects.create(name='فحص', labor_price=D('300'), estimated_hours=D('1'))
        self.cust.credit_limit = D('250.00')
        self.cust.save(update_fields=['credit_limit'])
        r = self.post_json('/system/job-card/save/', {
            'branch_id': self.branch.pk, 'customer_id': self.cust.pk,
            'items': [{'product_id': self.part.pk, 'qty': 1, 'price': 200}],
            'services': [{'service_id': svc.pk}],
            'treasury_id': self.till.pk, 'paid_amount': 300})        # 500 − 300 = آجل 200 ≤ 250
        self.assertEqual(r.status_code, 200, r.content)
        si = SaleInvoice.objects.get(pk=r.json()['invoice_id'])
        item = si.items.get()
        svc_item = si.service_items.get()
        url = f'/system/invoice/{si.pk}/review/'

        # استبعاد الخدمة (بيقلل الآجل) مسموح دايماً
        r = self.c.post(url, {f'item_{item.pk}_billable': 'on'}, HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        self.cust.refresh_from_db()
        balance_after_unbill = self.cust.balance
        # الحد يتقفل على المديونية الحالية بالظبط ⇒ أي زيادة تعدّيه
        self.cust.credit_limit = max(balance_after_unbill, D('0.01'))
        self.cust.save(update_fields=['credit_limit'])

        # رجوعها (+300) يعدّي الحد ⇒ يترفض ومفيش حاجة تتغيّر
        r = self.c.post(url, {f'item_{item.pk}_billable': 'on', f'svc_{svc_item.pk}_billable': 'on'},
                        HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        page = self.c.get(r['Location'], HTTP_HOST=self.host)
        self.assertContains(page, 'حد الائتمان')
        self.assertContains(page, 'messages-banner--error')
        svc_item.refresh_from_db()
        self.assertFalse(svc_item.is_billable)
        self.cust.refresh_from_db()
        self.assertEqual(self.cust.balance, balance_after_unbill)
        si.refresh_from_db()
        self.assertEqual(si.total_amount, D('200.00'))
