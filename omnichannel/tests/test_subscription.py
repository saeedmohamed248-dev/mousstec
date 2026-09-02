"""Subscription lifecycle property tests (no DB — pure field logic)."""
from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from omnichannel.models import TenantChannelConfig


def _cfg(**kw):
    # Unsaved instance — we only exercise read-only property logic.
    return TenantChannelConfig(**kw)


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
        self.assertEqual(c.subscription_days_left, 0)

    def test_is_operational_requires_valid_sub_ai_and_token(self):
        # Valid sub + ai on, but no token → not operational.
        c = _cfg(is_subscription_active=True, ai_enabled=True,
                 subscription_expires_at=timezone.now() + timedelta(days=5))
        self.assertFalse(c.is_operational)  # token missing
        # Expired sub → not operational even with everything else.
        c2 = _cfg(is_subscription_active=True, ai_enabled=True,
                  subscription_expires_at=timezone.now() - timedelta(days=1))
        c2._meta_access_token = "x"
        self.assertFalse(c2.is_operational)

    def test_price_constant(self):
        self.assertEqual(str(TenantChannelConfig.MONTHLY_PRICE), "250.00")
