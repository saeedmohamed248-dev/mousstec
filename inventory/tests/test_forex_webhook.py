"""Forex sync webhook — HMAC gate + rate ingestion.

The webhook writes exchange rates the customer-facing display layer reads.
It was an empty stub before; these pin the fail-closed HMAC contract and
that only supported, non-base currencies get written.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, override_settings

from inventory.views.webhooks import regional_tax_forex_sync_webhook

SECRET = 'forex-secret'


def _signed_request(body: dict, *, secret: str = SECRET):
    raw = json.dumps(body).encode('utf-8')
    sig = hmac.new(secret.encode('utf-8'), raw, hashlib.sha256).hexdigest()
    req = RequestFactory().post('/system/webhooks/regional/tax-forex-sync/',
                                data=raw, content_type='application/json')
    req.META['HTTP_X_FOREX_HMAC_SHA256'] = sig
    return req


@override_settings(FOREX_WEBHOOK_SECRET=SECRET)
class ForexWebhookTests(SimpleTestCase):
    def test_get_is_rejected(self):
        req = RequestFactory().get('/system/webhooks/regional/tax-forex-sync/')
        resp = regional_tax_forex_sync_webhook(req)
        self.assertEqual(resp.status_code, 403)

    def test_bad_signature_is_rejected(self):
        req = _signed_request({'base': 'EGP', 'rates': {'USD': 0.02}}, secret='wrong')
        resp = regional_tax_forex_sync_webhook(req)
        self.assertEqual(resp.status_code, 403)

    def test_non_egp_base_is_rejected(self):
        req = _signed_request({'base': 'USD', 'rates': {'EGP': 50}})
        resp = regional_tax_forex_sync_webhook(req)
        self.assertEqual(resp.status_code, 400)

    @mock.patch('clients.services.currency.update_rate', return_value=True)
    def test_valid_payload_updates_supported_currencies(self, mock_update):
        req = _signed_request({'base': 'EGP', 'rates': {'USD': 0.02, 'SAR': 0.076}})
        resp = regional_tax_forex_sync_webhook(req)
        self.assertEqual(resp.status_code, 200)
        called = {c.args[0] for c in mock_update.call_args_list}
        self.assertEqual(called, {'USD', 'SAR'})

    @mock.patch('clients.services.currency.update_rate', return_value=True)
    def test_unsupported_currency_is_skipped(self, mock_update):
        req = _signed_request({'base': 'EGP', 'rates': {'USD': 0.02, 'XYZ': 1.0}})
        resp = regional_tax_forex_sync_webhook(req)
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.content)
        self.assertIn('XYZ', body['skipped'])
        self.assertIn('USD', body['updated'])
