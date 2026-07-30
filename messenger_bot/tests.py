"""Messenger webhook — verification handshake + POST signature gate.

The POST signature check (X-Hub-Signature-256) is the guard that stops
anyone who finds the public URL from pumping fake events through the bot.
These pin both the GET verify-token handshake and the HMAC gate.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from django.test import RequestFactory, SimpleTestCase, override_settings

from messenger_bot.views import MessengerWebhookView

VERIFY_TOKEN = 'verify-me'
APP_SECRET = 'app-secret-123'


@override_settings(MESSENGER_VERIFY_TOKEN=VERIFY_TOKEN)
class WebhookVerifyTests(SimpleTestCase):
    def _get(self, **params):
        req = RequestFactory().get('/api/webhooks/messenger/', params)
        return MessengerWebhookView().get(req)

    def test_correct_token_echoes_challenge(self):
        resp = self._get(**{'hub.mode': 'subscribe',
                            'hub.verify_token': VERIFY_TOKEN,
                            'hub.challenge': '42'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b'42')

    def test_wrong_token_is_rejected(self):
        resp = self._get(**{'hub.mode': 'subscribe',
                            'hub.verify_token': 'nope',
                            'hub.challenge': '42'})
        self.assertEqual(resp.status_code, 403)


@override_settings(FB_APP_SECRET=APP_SECRET)
class WebhookSignatureTests(SimpleTestCase):
    def _post(self, body: bytes, sig_header):
        req = RequestFactory().post('/api/webhooks/messenger/', data=body,
                                    content_type='application/json')
        if sig_header is not None:
            req.META['HTTP_X_HUB_SIGNATURE_256'] = sig_header
        return req

    def test_valid_signature_accepted(self):
        body = json.dumps({'object': 'page', 'entry': []}).encode()
        sig = 'sha256=' + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(MessengerWebhookView._signature_ok(self._post(body, sig)))

    def test_bad_signature_rejected(self):
        body = json.dumps({'object': 'page'}).encode()
        self.assertFalse(
            MessengerWebhookView._signature_ok(self._post(body, 'sha256=deadbeef')))

    def test_missing_signature_rejected(self):
        body = b'{}'
        self.assertFalse(MessengerWebhookView._signature_ok(self._post(body, None)))

    @override_settings(FB_APP_SECRET='')
    def test_no_secret_accepts_but_warns(self):
        # Back-compat: without a configured secret the webhook stays open.
        body = b'{}'
        self.assertTrue(MessengerWebhookView._signature_ok(self._post(body, None)))
