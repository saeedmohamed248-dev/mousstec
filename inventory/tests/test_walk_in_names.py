"""
🧪 اسم العميل النقدي — فاتورة اتسجّلت باسم (من غير رقم موبايل) كانت بتظهر في
قايمة الفواتير والطباعة «عميل نقدي (Walk-in)» كأنها من غير اسم.

* الاسم محفوظ أول ملاحظات الفاتورة («العميل: …») — القايمة والطباعة بيعرضوه،
  والبحث بالاسم بيلاقيها.
* فواتير قبل ١١ أكتوبر الاسم فيها ضاع خالص — زرار «اكتب الاسم» في القايمة
  بيكتبه عليها (للعرض بس، من غير أي أثر على الخزائن أو الأرصدة).
* المرتجع على فاتورة عميل نقدي بيعرض اسم الفاتورة الأصلية.
"""
import json

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, override_settings

from inventory.models import Customer, SaleInvoice
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_inventory, make_product, make_sale_invoice,
    make_treasury,
)


def walk_in():
    cust, _ = Customer.objects.get_or_create(
        phone=Customer.normalize_phone('0000000000'), defaults={'name': 'عميل نقدي (Walk-in)'})
    return cust


@override_settings(ALLOWED_HOSTS=['*'])
class WalkInNameTests(ERPTenantTestCase):
    def setUp(self):
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.till = make_treasury(self.branch, balance='5000.00')
        self.filt = make_product(part_number='WN-FLT', retail_price='250.00', average_cost='150.00')
        make_inventory(self.filt, self.branch, quantity=10)
        self.boss = User.objects.create_user('wn_boss', password='x', is_staff=True, is_superuser=True)
        self.c = Client()
        self.c.force_login(self.boss)

    def _post(self, url, data):
        return self.c.post(url, json.dumps(data), content_type='application/json',
                           HTTP_ACCEPT='application/json', HTTP_HOST=self.host)

    def _get(self, url):
        return self.c.get(url, HTTP_HOST=self.host)

    def _pos_sale(self, name):
        r = self._post('/system/lightning-pos/checkout/', {
            'branch_id': self.branch.pk, 'customer_name': name,
            'items': [{'product_id': self.filt.pk, 'qty': 1, 'price': 250}],
            'treasury_id': self.till.pk, 'paid_amount': 250})
        self.assertEqual(r.status_code, 200, r.content)
        return SaleInvoice.objects.get(pk=r.json()['invoice_id'])

    # ------------------------------------------------------------------ model
    def test_name_is_read_from_the_notes_of_a_walk_in_invoice_only(self):
        inv = make_sale_invoice(walk_in(), self.branch)
        inv.notes = 'العميل: محمد سعيد — صوت في العفشة'
        self.assertEqual(inv.walk_in_name, 'محمد سعيد')
        self.assertEqual(inv.customer_display_name, 'محمد سعيد')
        self.assertEqual(inv.notes_display, 'صوت في العفشة')

        registered = make_sale_invoice(make_customer(name='عميل مسجل', phone='01055554444'), self.branch)
        registered.notes = 'العميل: حد تاني'
        self.assertEqual(registered.walk_in_name, '')
        self.assertEqual(registered.customer_display_name, 'عميل مسجل')
        self.assertEqual(registered.notes_display, 'العميل: حد تاني')

    def test_set_walk_in_name_keeps_the_other_notes(self):
        inv = make_sale_invoice(walk_in(), self.branch)
        inv.notes = 'صوت في العفشة'
        inv.set_walk_in_name('  أحمد   علي ')
        self.assertEqual(inv.notes, 'العميل: أحمد علي — صوت في العفشة')
        inv.set_walk_in_name('أحمد — علي')   # الفاصل مايدخلش في الاسم
        self.assertEqual(inv.walk_in_name, 'أحمد - علي')
        self.assertEqual(inv.notes_display, 'صوت في العفشة')
        inv.set_walk_in_name('')
        self.assertEqual(inv.notes, 'صوت في العفشة')
        inv.notes = None
        inv.set_walk_in_name('')
        self.assertIsNone(inv.notes)

    def test_a_return_shows_the_name_of_its_original_invoice(self):
        orig = make_sale_invoice(walk_in(), self.branch)
        SaleInvoice.objects.filter(pk=orig.pk).update(notes='العميل: سامي')
        ret = SaleInvoice.objects.create(
            invoice_type='sale', is_return=True, original_invoice=orig, customer=orig.customer,
            branch=self.branch, notes=f'مرتجع فاتورة #{orig.pk}')
        self.assertEqual(ret.customer_display_name, 'سامي')

    # ---------------------------------------------------------- list / print
    def test_pos_name_without_phone_shows_in_the_list_search_and_prints(self):
        inv = self._pos_sale('زبون من غير رقم')
        self.assertTrue(inv.customer.is_walk_in)

        r = self._get('/system/invoices/')
        self.assertContains(r, 'زبون من غير رقم')

        r = self._get('/system/invoices/?q=' + 'زبون')
        self.assertContains(r, f'#{inv.pk}')

        for kind in ('thermal', 'a4'):
            r = self._get(f'/system/invoice/{inv.pk}/print/{kind}/')
            self.assertContains(r, 'زبون من غير رقم')
            self.assertNotContains(r, '+00000000')

    # -------------------------------------------------------- set name action
    def test_name_can_be_written_on_an_old_walk_in_invoice(self):
        old = make_sale_invoice(walk_in(), self.branch, items=[(self.filt, 1, '250.00')])
        balance = walk_in().balance

        r = self._post(f'/system/invoices/{old.pk}/name/', {'name': 'كريم'})
        self.assertEqual(r.status_code, 200, r.content)
        old.refresh_from_db()
        self.assertEqual(old.walk_in_name, 'كريم')
        self.assertContains(self._get('/system/invoices/'), 'كريم')
        self.assertEqual(walk_in().balance, balance)   # اسم للعرض بس

        r = self._post(f'/system/invoices/{old.pk}/name/', {'name': ''})
        self.assertEqual(r.status_code, 200)
        old.refresh_from_db()
        self.assertEqual(old.walk_in_name, '')

    def test_name_action_refuses_a_registered_customer_invoice(self):
        inv = make_sale_invoice(make_customer(name='عميل مسجل', phone='01055553333'), self.branch)
        r = self._post(f'/system/invoices/{inv.pk}/name/', {'name': 'اسم تاني'})
        self.assertEqual(r.status_code, 400)
        inv.refresh_from_db()
        self.assertFalse(inv.notes)
