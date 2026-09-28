"""
🧪 Fixes found while using the spare-parts system end to end in a browser.

* stock movement history had every change twice (and receipts labelled
  "manual adjustment");
* the offline business advisor answers from real numbers when no AI key;
* WhatsApp/Messenger quick catalogue reply + tokenizer;
* visitor log no longer fails for company users;
* login lands on the business dashboard, not the raw admin;
* the purchase screen gets the last purchase price, the POS doesn't.
"""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, SimpleTestCase, override_settings

from inventory.models import Inventory, InventoryMovement
from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_inventory, make_product, make_purchase_invoice,
    make_treasury, make_vendor,
)

D = Decimal


@override_settings(ALLOWED_HOSTS=['*'])
class HandsOnReviewTests(ERPTenantTestCase):

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        self.host = self.domain.domain
        self.branch = make_branch()
        self.treasury = make_treasury(self.branch, balance='1000.00')
        self.product = make_product(part_number='HR-FLT-1', name='فلتر زيت N52', retail_price='200.00',
                                    purchase_price='120.00', average_cost='120.00')
        make_inventory(self.product, self.branch, quantity=10)
        self.owner = User.objects.create_user('boss', password='x', is_staff=True, is_superuser=True)

    def _client(self):
        c = Client()
        c.force_login(self.owner)
        return c

    def _movements(self):
        return list(InventoryMovement.objects.filter(product=self.product).order_by('id'))

    # ── stock movement history ────────────────────────────────────────
    def test_pos_sale_records_one_movement(self):
        before = len(self._movements())
        r = self._client().post('/system/lightning-pos/checkout/', data=json.dumps({
            'branch_id': self.branch.pk, 'customer_name': 'عميل', 'customer_phone': '01011112222',
            'payments': [{'treasury_id': self.treasury.pk, 'amount': 400}],
            'items': [{'product_id': self.product.pk, 'qty': 2, 'price': 200, 'discount': 0}],
        }), content_type='application/json', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 200, r.content)
        new = self._movements()[before:]
        self.assertEqual(len(new), 1, [(m.reason, m.quantity_change) for m in new])
        self.assertEqual((new[0].reason, new[0].quantity_change), ('sale', -2))

    def test_purchase_receipt_is_labelled_purchase_once(self):
        vendor = make_vendor()
        pi = make_purchase_invoice(vendor, self.branch, treasury=self.treasury,
                                   items=[(self.product, 5, '110.00')], paid_amount='0.00')
        before = len(self._movements())
        pi.status = 'posted'
        pi.save()
        new = self._movements()[before:]
        self.assertEqual([(m.reason, m.quantity_change) for m in new], [('purchase', 5)])
        self.assertEqual(Inventory.objects.get(product=self.product, branch=self.branch).quantity, 15)

    def test_direct_quantity_edit_is_still_tracked(self):
        before = len(self._movements())
        inv = Inventory.objects.get(product=self.product, branch=self.branch)
        inv.quantity = 7
        inv.save()
        new = self._movements()[before:]
        self.assertEqual([(m.reason, m.quantity_change) for m in new], [('manual', -3)])

    # ── offline advisor ───────────────────────────────────────────────
    def test_offline_advisor_answers_from_data(self):
        from erp_core.ai.advisor_offline import offline_answer
        cash = offline_answer('الكاش كام؟')
        self.assertTrue(cash['success'])
        self.assertIn('1,000.00', cash['answer'])
        stock = offline_answer('عندي فلتر زيت؟')
        self.assertIn('HR-FLT-1', stock['answer'])
        self.assertIn('10 قطعة', stock['answer'])
        hello = offline_answer('ازيك')
        self.assertNotIn('مالقيتش', hello['answer'])

    def test_advisor_endpoint_uses_offline_mode_without_key(self):
        with self.settings(TOGETHER_API_KEY=''):
            r = self._client().post('/advisor/api/chat/', data=json.dumps(
                {'query': 'مين عليه فلوس؟', 'sector': 'automotive'}),
                content_type='application/json', HTTP_HOST=self.host)
        body = r.json()
        self.assertTrue(body['success'])
        self.assertEqual(body.get('mode'), 'offline')

    # ── bots ──────────────────────────────────────────────────────────
    def test_bot_quick_reply_only_for_relevant_in_stock_parts(self):
        from omnichannel.services.inventory_context import quick_catalog_reply
        make_product(part_number='HR-PAD-1', name='تيل فرامل F30', retail_price='900.00')  # no stock
        reply = quick_catalog_reply('السلام عليكم عندكم فلتر زيت؟', currency='ج.م')
        self.assertIn('فلتر زيت N52', reply)
        self.assertNotIn('تيل', reply)
        self.assertEqual(quick_catalog_reply('عندكم تيل فرامل؟'), '')   # out of stock → handoff

    # ── visitor log / login landing / purchase cost ──────────────────
    def test_visitor_log_written_for_company_user(self):
        from clients.models import VisitorLog
        from django_tenants.utils import schema_context
        self._client().get('/system/dashboard/', HTTP_HOST=self.host)
        with schema_context('public'):
            row = VisitorLog.objects.filter(tenant_schema=self.tenant.schema_name,
                                            path='/system/dashboard/').first()
        self.assertIsNotNone(row)
        self.assertIsNone(row.user_id)

    def test_auto_login_lands_on_business_dashboard(self):
        import time
        from django.core import signing
        token = signing.dumps({'schema_name': self.tenant.schema_name, 'user_id': self.owner.pk,
                               'created': int(time.time()), 'next': ''}, salt='tenant-auto-login-token')
        r = Client().get(f'/auto-login/?token={token}', HTTP_HOST=self.host)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], '/system/dashboard/')

    def test_purchase_search_shows_cost_to_buyers_only(self):
        r = self._client().get('/system/lightning-pos/search/?scope=all&q=فلتر', HTTP_HOST=self.host)
        self.assertEqual(r.json()['results'][0]['cost'], 120.0)
        r = self._client().get('/system/lightning-pos/search/?q=فلتر', HTTP_HOST=self.host)
        self.assertNotIn('cost', r.json()['results'][0])


class OfflineHelpersTests(SimpleTestCase):
    def test_dtc_extraction_and_family(self):
        from erp_core.ai.diagnostic_offline import _family, extract_codes
        self.assertEqual(extract_codes('طالع P0300 و p 0171 وكمان C1234'), ['P0300', 'P0171', 'C1234'])
        self.assertIn('الإشعال', _family('P0302')[0])
        self.assertIn('ABS', _family('C0035')[0])

    def test_arabic_question_mark_is_not_part_of_a_word(self):
        from omnichannel.services.inventory_context import _keywords
        self.assertEqual(_keywords('عندكم كشاف F30؟'), ['عندكم', 'كشاف', 'f30'])
