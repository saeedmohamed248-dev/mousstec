"""
🔧 _check_ai_access — bonus-grant fix tests (post Phase N.6 smoke)
=====================================================================
Pre-fix bug: _check_ai_access() short-circuited on `not sub.ai_addon`
before honoring the bonus pool. The /printing/ai/status/ endpoint already
honored bonus grants → UI showed "Active 60/60 designs" → user clicks
prompt-engineer → 403 "لم يتم تفعيل حزمة AI Studio".

These tests pin the corrected access-gate semantics:

  • Tenant w/ paid addon + quota left          → ALLOWED
  • Tenant w/ no addon, bonus > 0              → ALLOWED  (was: DENIED ❌)
  • Tenant w/ no addon, bonus == 0             → DENIED + clear message
  • Tenant w/ addon exists but monthly used up → DENIED + "quota exhausted"
"""
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.utils import timezone

from printing.views import _check_ai_access


def _build_tenant_mock(*, has_addon=True, is_active=True, bonus=0,
                       monthly_used=0, addon_limit=100):
    """Return a tenant-like object that satisfies _check_ai_access' reads,
    plus a context manager that patches AILimitTracker.* to match."""

    addon = None
    if has_addon:
        addon = MagicMock()
        addon.ai_generations_limit = addon_limit
        addon.whatsapp_messages_limit = addon_limit
        addon.name = 'Test Addon'

    sub = MagicMock()
    sub.is_active = is_active
    sub.ai_addon = addon

    tenant = MagicMock()
    tenant.subscription = sub
    tenant.name = 'مطبعة الاختبار'
    return tenant, bonus, monthly_used


def _patch_tracker(bonus, monthly_used):
    """Context-manager bundle: patches the two AILimitTracker classmethods
    that _check_ai_access (transitively, via can_use) reads from."""
    can_use_patch = patch(
        'clients.models.AILimitTracker.can_use',
        side_effect=lambda tenant, action_type: (
            bonus > 0 or (
                tenant.subscription.ai_addon is not None
                and monthly_used < tenant.subscription.ai_addon.ai_generations_limit
            )
        ),
    )
    bonus_patch = patch(
        'clients.models.AILimitTracker._get_bonus_remaining',
        return_value=bonus,
    )
    return can_use_patch, bonus_patch


class CheckAIAccessTests(TestCase):

    # ── Failure modes that pre-date this fix (shouldn't have regressed) ──
    def test_no_tenant_denied(self):
        allowed, err = _check_ai_access(None)
        self.assertFalse(allowed)
        self.assertIn('المستأجر', err)

    def test_missing_subscription_denied(self):
        from clients.models import TenantSubscription
        tenant = MagicMock()
        # Accessing tenant.subscription raises DoesNotExist
        type(tenant).subscription = property(
            lambda self: (_ for _ in ()).throw(TenantSubscription.DoesNotExist())
        )
        allowed, err = _check_ai_access(tenant)
        self.assertFalse(allowed)
        self.assertIn('لا يوجد اشتراك', err)

    def test_inactive_subscription_denied(self):
        tenant, bonus, used = _build_tenant_mock(is_active=False)
        cu, br = _patch_tracker(bonus, used)
        with cu, br:
            allowed, err = _check_ai_access(tenant)
        self.assertFalse(allowed)
        self.assertIn('غير مفعّل', err)

    # ── The actual fix: bonus-only tenants must be allowed ──────────────
    def test_bonus_only_tenant_is_allowed(self):
        """Tenant has NO paid addon but HAS bonus grants → allowed.
        This is the regression the user hit in production."""
        tenant, bonus, used = _build_tenant_mock(has_addon=False, bonus=60)
        cu, br = _patch_tracker(bonus, used)
        with cu, br:
            allowed, err = _check_ai_access(tenant, 'ai_generation')
        self.assertTrue(allowed, msg=f'Expected allowed, got err={err}')
        self.assertIsNone(err)

    def test_paid_addon_with_quota_left_is_allowed(self):
        tenant, bonus, used = _build_tenant_mock(
            has_addon=True, bonus=0, monthly_used=10, addon_limit=100,
        )
        cu, br = _patch_tracker(bonus, used)
        with cu, br:
            allowed, err = _check_ai_access(tenant, 'ai_generation')
        self.assertTrue(allowed)
        self.assertIsNone(err)

    def test_both_addon_and_bonus_allowed(self):
        tenant, bonus, used = _build_tenant_mock(
            has_addon=True, bonus=20, monthly_used=99, addon_limit=100,
        )
        cu, br = _patch_tracker(bonus, used)
        with cu, br:
            allowed, _ = _check_ai_access(tenant)
        self.assertTrue(allowed)

    # ── Denial paths after the fix ──────────────────────────────────────
    def test_no_addon_and_no_bonus_denied_with_specific_message(self):
        """The 'truly inactive' case — message must guide user to admin."""
        tenant, bonus, used = _build_tenant_mock(has_addon=False, bonus=0)
        cu, br = _patch_tracker(bonus, used)
        with cu, br:
            allowed, err = _check_ai_access(tenant)
        self.assertFalse(allowed)
        self.assertIn('لم يتم تفعيل حزمة AI Studio', err)
        # Also mentions bonuses so the user knows both options
        self.assertIn('هدايا', err)

    def test_addon_exists_but_quota_exhausted_specific_message(self):
        """User has paid plan but used everything — different message
        (renewal-focused, not 'add addon')."""
        tenant, bonus, used = _build_tenant_mock(
            has_addon=True, bonus=0, monthly_used=100, addon_limit=100,
        )
        cu, br = _patch_tracker(bonus, used)
        with cu, br:
            allowed, err = _check_ai_access(tenant)
        self.assertFalse(allowed)
        self.assertIn('استنفاد', err)
        # Does NOT tell user to "add an addon" — they already have one
        self.assertNotIn('لم يتم تفعيل', err)

    # ── Action-type specificity ─────────────────────────────────────────
    def test_action_type_is_forwarded_to_tracker(self):
        """The action_type kwarg propagates through to can_use lookup."""
        tenant, bonus, used = _build_tenant_mock(has_addon=False, bonus=5)
        seen = {}

        def _capturing_can_use(t, action_type):
            seen['action'] = action_type
            return True

        with patch('clients.models.AILimitTracker.can_use',
                   side_effect=_capturing_can_use):
            allowed, _ = _check_ai_access(tenant, 'whatsapp_send')
        self.assertTrue(allowed)
        self.assertEqual(seen['action'], 'whatsapp_send')
