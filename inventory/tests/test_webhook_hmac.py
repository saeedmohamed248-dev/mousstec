"""🛡️ التحقق من توقيع الـ webhooks الخارجية (`_verify_webhook_hmac`).

قبل كده: Shopify بيبعت التوقيع base64 والكود كان بيقارن بـ hex بس ⇒ كل webhooks Shopify
كانت بترجع 403.
"""
import base64
import hashlib
import hmac

from django.test import RequestFactory, SimpleTestCase, override_settings

from inventory.views.webhooks import _verify_webhook_hmac

BODY = b'{"id": 1}'
SECRET = 'top-secret'
DIGEST = hmac.new(SECRET.encode(), BODY, hashlib.sha256).digest()


def _req(sig, header='HTTP_X_SHOPIFY_HMAC_SHA256'):
    return RequestFactory().post('/x/', data=BODY, content_type='application/json', **{header: sig})


@override_settings(SHOPIFY_WEBHOOK_SECRET=SECRET, PAYMENT_GW_WEBHOOK_SECRET=SECRET)
class WebhookHmacTests(SimpleTestCase):
    def test_shopify_base64_signature_accepted(self):
        self.assertTrue(_verify_webhook_hmac(_req(base64.b64encode(DIGEST).decode()), 'SHOPIFY_WEBHOOK_SECRET'))

    def test_hex_signature_accepted(self):
        self.assertTrue(_verify_webhook_hmac(_req(DIGEST.hex(), 'HTTP_X_SIGNATURE'),
                                             'PAYMENT_GW_WEBHOOK_SECRET', 'HTTP_X_SIGNATURE'))

    def test_wrong_or_missing_signature_rejected(self):
        self.assertFalse(_verify_webhook_hmac(_req('deadbeef'), 'SHOPIFY_WEBHOOK_SECRET'))
        self.assertFalse(_verify_webhook_hmac(_req(''), 'SHOPIFY_WEBHOOK_SECRET'))

    @override_settings(SHOPIFY_WEBHOOK_SECRET='')
    def test_unconfigured_secret_rejects_everything(self):
        self.assertFalse(_verify_webhook_hmac(_req(DIGEST.hex()), 'SHOPIFY_WEBHOOK_SECRET'))
