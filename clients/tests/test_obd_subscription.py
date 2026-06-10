"""
Phase 3 #3 — OBD / Diagnostics-Room paid add-on tests.

Covers the subscription/expiry logic that gates `Client.obd_access_is_valid`
plus the helper methods used by the Super Admin grant UI.
"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from clients.models import Client


def _make_tenant(**overrides) -> Client:
    defaults = dict(
        name='OBD Tester Co.',
        owner_name='Test Owner',
        phone='+201000000001',
        schema_name=f'obd_test_{abs(hash(timezone.now())) % 10_000_000}',
    )
    defaults.update(overrides)
    # Skip auto schema creation — we only need the row in `public`.
    c = Client(**defaults)
    c.auto_create_schema = False
    c.save()
    return c


class OBDAccessFieldDefaultsTests(TestCase):
    def test_defaults_are_inactive(self):
        c = _make_tenant()
        self.assertFalse(c.has_obd_access)
        self.assertIsNone(c.obd_access_expiry)
        self.assertFalse(c.obd_access_is_valid)
        self.assertFalse(c.obd_access_is_lifetime)


class OBDAccessGrantTests(TestCase):
    def test_grant_one_day(self):
        c = _make_tenant()
        c.grant_obd_access(timedelta(days=1))
        self.assertTrue(c.has_obd_access)
        self.assertIsNotNone(c.obd_access_expiry)
        self.assertTrue(c.obd_access_is_valid)
        self.assertFalse(c.obd_access_is_lifetime)
        # Should be roughly 24h from now.
        delta = c.obd_access_expiry - timezone.now()
        self.assertGreater(delta.total_seconds(), 23 * 3600)
        self.assertLess(delta.total_seconds(), 25 * 3600)

    def test_grant_lifetime(self):
        c = _make_tenant()
        c.grant_obd_access(None)
        self.assertTrue(c.has_obd_access)
        self.assertIsNone(c.obd_access_expiry)
        self.assertTrue(c.obd_access_is_valid)
        self.assertTrue(c.obd_access_is_lifetime)

    def test_grant_stacks_on_existing_window(self):
        """+1m + +1m should give ~2 months, not reset to 1 month."""
        c = _make_tenant()
        c.grant_obd_access(timedelta(days=30))
        first_expiry = c.obd_access_expiry
        c.grant_obd_access(timedelta(days=30))
        delta = c.obd_access_expiry - first_expiry
        self.assertGreater(delta.total_seconds(), 29 * 86400)

    def test_grant_after_expiry_starts_fresh(self):
        c = _make_tenant(
            has_obd_access=True,
            obd_access_expiry=timezone.now() - timedelta(days=5),
        )
        self.assertFalse(c.obd_access_is_valid)  # expired
        c.grant_obd_access(timedelta(days=7))
        # New expiry should be ~7 days from now (not 7 days from past expiry).
        delta = c.obd_access_expiry - timezone.now()
        self.assertGreater(delta.total_seconds(), 6.5 * 86400)
        self.assertLess(delta.total_seconds(), 7.5 * 86400)


class OBDAccessExpiryTests(TestCase):
    def test_expired_access_invalidates(self):
        c = _make_tenant(
            has_obd_access=True,
            obd_access_expiry=timezone.now() - timedelta(minutes=1),
        )
        self.assertFalse(c.obd_access_is_valid)

    def test_future_access_valid(self):
        c = _make_tenant(
            has_obd_access=True,
            obd_access_expiry=timezone.now() + timedelta(days=1),
        )
        self.assertTrue(c.obd_access_is_valid)

    def test_flag_off_overrides_future_expiry(self):
        """If has_obd_access=False, future expiry doesn't matter."""
        c = _make_tenant(
            has_obd_access=False,
            obd_access_expiry=timezone.now() + timedelta(days=365),
        )
        self.assertFalse(c.obd_access_is_valid)


class OBDAccessRevokeTests(TestCase):
    def test_revoke_clears_state(self):
        c = _make_tenant()
        c.grant_obd_access(None)  # lifetime
        self.assertTrue(c.obd_access_is_valid)
        c.revoke_obd_access()
        self.assertFalse(c.has_obd_access)
        self.assertIsNone(c.obd_access_expiry)
        self.assertFalse(c.obd_access_is_valid)
