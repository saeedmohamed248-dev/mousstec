"""Subscription lifecycle property tests (no DB — pure field logic)."""
from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from social_ads.models import SocialAdsConfig


def _cfg(**kw):
    return SocialAdsConfig(**kw)


class SubscriptionStateTests(SimpleTestCase):
    def test_inactive_by_default(self):
        c = _cfg()
        self.assertFalse(c.subscription_is_valid)
        self.assertEqual(c.subscription_state, "inactive")
        self.assertEqual(c.subscription_days_left, 0)

    def test_lifetime(self):
        c = _cfg(is_subscription_active=True, subscription_expires_at=None)
        self.assertTrue(c.subscription_is_valid)
        self.assertTrue(c.subscription_is_lifetime)
        self.assertEqual(c.subscription_state, "lifetime")
        self.assertIsNone(c.subscription_days_left)

    def test_active_future_expiry(self):
        c = _cfg(is_subscription_active=True,
                 subscription_expires_at=timezone.now() + timedelta(days=10))
        self.assertTrue(c.subscription_is_valid)
        self.assertEqual(c.subscription_state, "active")
        self.assertGreaterEqual(c.subscription_days_left, 9)

    def test_expired(self):
        c = _cfg(is_subscription_active=True,
                 subscription_expires_at=timezone.now() - timedelta(days=1))
        self.assertFalse(c.subscription_is_valid)
        self.assertEqual(c.subscription_state, "expired")

    def test_is_operational_requires_valid_sub_token_and_page(self):
        # Valid sub but no page/token → not operational.
        c = _cfg(is_subscription_active=True,
                 subscription_expires_at=timezone.now() + timedelta(days=5))
        self.assertFalse(c.is_operational)

        # Add a real (encrypted) token + page → operational.
        c.facebook_page_id = "123456"
        c.page_access_token = "EAAG-real-token"   # setter stores Fernet ciphertext
        self.assertEqual(c.page_access_token, "EAAG-real-token")  # round-trips
        self.assertTrue(c.is_operational)
        self.assertTrue(c.has_facebook())

        # Expired sub → not operational even with credentials.
        c.subscription_expires_at = timezone.now() - timedelta(days=1)
        self.assertFalse(c.is_operational)

    def test_price_constant_and_region(self):
        self.assertEqual(str(SocialAdsConfig.MONTHLY_PRICE), "250.00")
        self.assertEqual(str(SocialAdsConfig.price_for_country("EG")), "250")
        self.assertEqual(str(SocialAdsConfig.price_for_country("AE")), "25")

    def test_preferred_times_parsing(self):
        c = _cfg(preferred_times="09:30, 20:00 ,")
        self.assertEqual(c.preferred_times_list(), ["09:30", "20:00"])
        self.assertEqual(_cfg(preferred_times="").preferred_times_list(), ["11:00", "19:00"])
