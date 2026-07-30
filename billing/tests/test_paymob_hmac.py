"""Regression coverage for ``verify_paymob_hmac`` — the POST-callback path.

Pinned production bug:

    Paymob's server-to-server ``transaction-processed`` callback is JSON, so
    ``json.loads`` hands us Python booleans for fields like ``success`` and
    ``is_3d_secure``. The old concatenation used ``str()`` which renders
    ``"True"``/``"False"`` — but Paymob signed the lowercase JSON form
    ``"true"``/``"false"``. Result: every legitimate POST confirmation
    failed verification with ``hmac_mismatch`` and the payment was dropped.
    (GET redirect callbacks carry string params and were unaffected, which
    is why the bug survived manual testing in the browser.)

``_hmac_value`` now renders booleans lowercase and ``None`` as the empty
string, matching Paymob's signing rules.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from django.test import RequestFactory, SimpleTestCase, override_settings

from billing.services.paymob import (
    _PAYMOB_HMAC_FIELDS,
    _extract_paymob_fields,
    _hmac_value,
    verify_paymob_hmac,
)

SECRET = 'paymob-test-hmac-secret'


def _paymob_post_body(*, success=True, order_id=987654):
    """A realistic (trimmed) Paymob POST payload with native JSON booleans."""
    return {
        'type': 'TRANSACTION',
        'obj': {
            'id': 123456789,
            'amount_cents': 45000,
            'created_at': '2026-07-30T10:00:00.000000',
            'currency': 'EGP',
            'error_occured': False,
            'has_parent_transaction': False,
            'integration_id': 11223,
            'is_3d_secure': True,
            'is_auth': False,
            'is_capture': False,
            'is_refunded': False,
            'is_standalone_payment': True,
            'is_voided': False,
            'order': {'id': order_id},
            'owner': 4321,
            'pending': False,
            'source_data': {'pan': '2346', 'sub_type': 'MasterCard', 'type': 'card'},
            'success': success,
        },
    }


def _sign(body: dict) -> str:
    """Sign the way Paymob does: lowercase JSON booleans, empty for null."""
    fields = _extract_paymob_fields(body, {})
    concatenated = ''.join(_hmac_value(fields[k]) for k in _PAYMOB_HMAC_FIELDS)
    return hmac.new(
        SECRET.encode('utf-8'), concatenated.encode('utf-8'), hashlib.sha512
    ).hexdigest()


@override_settings(PAYMOB_HMAC_SECRET=SECRET)
class PaymobPostHmacTests(SimpleTestCase):
    def _post(self, body: dict, sig: str):
        raw = json.dumps({**body, 'hmac': sig}).encode('utf-8')
        return RequestFactory().post(
            '/payment/paymob/callback/', data=raw, content_type='application/json'
        )

    def test_post_callback_with_json_booleans_verifies(self):
        body = _paymob_post_body()
        request = self._post(body, _sign(body))
        ok, reason = verify_paymob_hmac(request, body_data={**body, 'hmac': _sign(body)})
        self.assertTrue(ok, f'expected valid HMAC, got reason={reason}')

    def test_tampered_success_flag_is_rejected(self):
        body = _paymob_post_body(success=False)
        sig = _sign(body)
        forged = _paymob_post_body(success=True)  # flip after signing
        request = self._post(forged, sig)
        ok, reason = verify_paymob_hmac(request, body_data={**forged, 'hmac': sig})
        self.assertFalse(ok)
        self.assertEqual(reason, 'hmac_mismatch')

    def test_hmac_value_rendering_rules(self):
        self.assertEqual(_hmac_value(True), 'true')
        self.assertEqual(_hmac_value(False), 'false')
        self.assertEqual(_hmac_value(None), '')
        self.assertEqual(_hmac_value(45000), '45000')
        self.assertEqual(_hmac_value('MasterCard'), 'MasterCard')
