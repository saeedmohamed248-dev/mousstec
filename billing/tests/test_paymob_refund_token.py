"""Coverage for the Paymob refund + saved-card-token paths.

All network calls are mocked. These pin the branching contract the admin
refund flow and the auto-renewal task depend on:
  • refund_transaction never raises and returns (ok, detail)
  • charge_with_saved_token rejects bad args before any network call
  • the TOKEN-callback HMAC verifies with Paymob's lowercase-JSON scheme
"""
from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, override_settings

from billing.services import paymob


def _resp(status_code, json_body):
    m = mock.Mock()
    m.status_code = status_code
    m.json.return_value = json_body
    m.text = str(json_body)
    return m


class RefundTransactionTests(SimpleTestCase):
    def test_zero_amount_rejected_without_network(self):
        ok, detail = paymob.refund_transaction('txn123', 0)
        self.assertFalse(ok)
        self.assertEqual(detail, 'invalid_refund_args')

    def test_non_numeric_amount_rejected(self):
        ok, detail = paymob.refund_transaction('txn123', 'abc')
        self.assertFalse(ok)
        self.assertEqual(detail, 'invalid_amount')

    def test_missing_txn_id_rejected(self):
        ok, detail = paymob.refund_transaction('', Decimal('50'))
        self.assertFalse(ok)
        self.assertEqual(detail, 'invalid_refund_args')

    @mock.patch('billing.services.paymob._fetch_auth_token', return_value='tok')
    @mock.patch('requests.post')
    def test_successful_refund_returns_refund_txn_id(self, mock_post, _auth):
        mock_post.return_value = _resp(200, {'id': 999, 'success': True})
        ok, detail = paymob.refund_transaction('txn123', Decimal('50'))
        self.assertTrue(ok)
        self.assertEqual(detail, '999')

    @mock.patch('billing.services.paymob._fetch_auth_token', return_value='tok')
    @mock.patch('requests.post')
    def test_gateway_rejection_is_reported_not_raised(self, mock_post, _auth):
        mock_post.return_value = _resp(400, {'message': 'bad'})
        ok, detail = paymob.refund_transaction('txn123', Decimal('50'))
        self.assertFalse(ok)
        self.assertEqual(detail, 'gateway_rejected_400')


class ChargeWithSavedTokenGuardTests(SimpleTestCase):
    def test_invalid_amount_rejected_before_network(self):
        with mock.patch('requests.post') as mock_post:
            ok, detail = paymob.charge_with_saved_token(
                'card_tok', 0, order_ref='autorenew_x')
        self.assertFalse(ok)
        self.assertEqual(detail, 'invalid_charge_args')
        mock_post.assert_not_called()

    @override_settings(PAYMOB_API_KEY='', PAYMOB_INTEGRATION_ID='')
    def test_unconfigured_gateway_rejected(self):
        ok, detail = paymob.charge_with_saved_token(
            'card_tok', Decimal('100'), order_ref='autorenew_x')
        self.assertFalse(ok)
        self.assertEqual(detail, 'gateway_not_configured')


@override_settings(PAYMOB_HMAC_SECRET='token-secret')
class TokenHmacTests(SimpleTestCase):
    def _obj(self):
        return {
            'card_subtype': 'MasterCard',
            'created_at': '2026-07-30T10:00:00',
            'email': 'c@x.com',
            'id': 555,
            'masked_pan': '2346',
            'merchant_id': 42,
            'order_id': 987,
            'token': 'abc123',
        }

    def _sign(self, obj):
        concatenated = ''.join(
            paymob._hmac_value(obj.get(k, ''))
            for k in paymob._PAYMOB_TOKEN_HMAC_FIELDS
        )
        return hmac.new(b'token-secret', concatenated.encode('utf-8'),
                        hashlib.sha512).hexdigest()

    def test_valid_token_callback_verifies(self):
        obj = self._obj()
        sig = self._sign(obj)
        request = RequestFactory().post('/payment/paymob/callback/')
        ok, reason = paymob.verify_paymob_token_hmac(
            request, {'type': 'TOKEN', 'obj': obj, 'hmac': sig})
        self.assertTrue(ok, f'reason={reason}')

    def test_tampered_token_rejected(self):
        obj = self._obj()
        sig = self._sign(obj)
        obj['token'] = 'HIJACKED'  # change after signing
        request = RequestFactory().post('/payment/paymob/callback/')
        ok, reason = paymob.verify_paymob_token_hmac(
            request, {'type': 'TOKEN', 'obj': obj, 'hmac': sig})
        self.assertFalse(ok)
        self.assertEqual(reason, 'hmac_mismatch')
