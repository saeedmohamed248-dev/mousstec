"""
💵 طلب من موقع FixIt بطريقة «عربون والباقي عند الاستلام» — الفاتورة المسودة هنا لازم
تقول للكاشير العربون كام (اتحوّل) والباقي كام (يتحصّل كاش من المندوب).
"""
import json

from django.test import RequestFactory, override_settings

from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_product
from inventory.models import SaleInvoice
from inventory.views.fixit_webhook import fixit_order_webhook


@override_settings(FIXIT_SYNC_SECRET='s3cret', FIXIT_BRANCH_ID=None)
class FixitDepositOrderTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.product = make_product(part_number='DEP-1', retail_price='2345.00')

    def _post(self, payload):
        req = RequestFactory().post('/inventory/webhooks/fixit/order/', data=json.dumps(payload),
                                    content_type='application/json', HTTP_X_SYNC_SECRET='s3cret')
        return fixit_order_webhook(req)

    def _order(self, number, **extra):
        return {'number': number, 'customer': {'name': 'عميل الموقع', 'phone': '01012345678'},
                'items': [{'sku': 'DEP-1', 'name': 'تيل', 'qty': 1, 'price': 2345}],
                'total': 2345, **extra}

    def test_deposit_order_notes_show_deposit_and_balance(self):
        r = self._post(self._order('ORD-DEP1', payment='deposit', deposit=470, balanceDue=1875))
        self.assertEqual(r.status_code, 200, r.content)
        inv = SaleInvoice.objects.get(pk=json.loads(r.content)['invoice_id'])
        self.assertEqual(inv.status, 'quotation')  # مسودة — مفيش أي حركة فلوس اتسجلت
        self.assertIn('عربون 470', inv.notes)
        self.assertIn('الباقي 1875', inv.notes)
        self.assertIn('كاش عند الاستلام', inv.notes)

    def test_full_transfer_order_has_no_deposit_line(self):
        r = self._post(self._order('ORD-DEP2', payment='instapay'))
        inv = SaleInvoice.objects.get(pk=json.loads(r.content)['invoice_id'])
        self.assertNotIn('عربون', inv.notes)
