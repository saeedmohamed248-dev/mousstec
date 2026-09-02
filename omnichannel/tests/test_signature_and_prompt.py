"""Signature verification + prompt/crypto tests (no DB needed)."""
import hashlib
import hmac

from django.test import SimpleTestCase, override_settings

from omnichannel.services import meta_api


class SignatureTests(SimpleTestCase):
    def test_valid_signature_accepted(self):
        secret = "app_secret_xyz"
        body = b'{"object":"page"}'
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(meta_api.verify_signature(secret, body, sig))

    def test_tampered_body_rejected(self):
        secret = "app_secret_xyz"
        body = b'{"object":"page"}'
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertFalse(meta_api.verify_signature(secret, b'{"object":"x"}', sig))

    def test_missing_pieces_rejected(self):
        self.assertFalse(meta_api.verify_signature("", b"x", "sha256=abc"))
        self.assertFalse(meta_api.verify_signature("s", b"x", ""))
        self.assertFalse(meta_api.verify_signature("s", b"x", "md5=abc"))


@override_settings(SECRET_KEY="unit-test-secret-key", OMNICHANNEL_SECRET_KEK="")
class CryptoRoundTripTests(SimpleTestCase):
    def test_encrypt_decrypt_roundtrip(self):
        from omnichannel import crypto
        token = "EAAG_super_secret_meta_token_123"
        enc = crypto.encrypt(token)
        self.assertNotEqual(enc, token)          # actually encrypted
        self.assertEqual(crypto.decrypt(enc), token)

    def test_decrypt_empty_and_garbage(self):
        from omnichannel import crypto
        self.assertEqual(crypto.decrypt(""), "")
        self.assertEqual(crypto.decrypt("not-a-valid-token"), "")
