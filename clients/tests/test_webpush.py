"""Web Push service — configuration gate + payload shape.

The delivery path (pywebpush + VAPID) is integration-tested against a live
push service, not here. These lock the two behaviours the rest of the system
relies on: push stays fully disabled unless VAPID keys are set, and the
payload carries the fields the service worker's push handler reads.
"""
from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from clients.services import webpush


class WebPushConfigTests(SimpleTestCase):
    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY='', WEBPUSH_VAPID_PUBLIC_KEY='')
    def test_disabled_when_keys_missing(self):
        self.assertFalse(webpush.webpush_configured())

    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY='priv', WEBPUSH_VAPID_PUBLIC_KEY='pub')
    def test_enabled_when_both_keys_present(self):
        self.assertTrue(webpush.webpush_configured())

    @override_settings(WEBPUSH_VAPID_PRIVATE_KEY='priv', WEBPUSH_VAPID_PUBLIC_KEY='')
    def test_disabled_when_only_one_key_present(self):
        self.assertFalse(webpush.webpush_configured())

    def test_payload_carries_sw_fields(self):
        p = webpush._payload('عنوان', 'نص', '/marketplace/notifications/')
        self.assertEqual(p['title'], 'عنوان')
        self.assertEqual(p['body'], 'نص')
        self.assertEqual(p['url'], '/marketplace/notifications/')
        # icon/badge are what showNotification() renders
        self.assertIn('icon', p)
        self.assertIn('badge', p)

    def test_payload_defaults_url_to_root(self):
        p = webpush._payload('t', 'b', '')
        self.assertEqual(p['url'], '/')
