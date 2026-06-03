"""
🛡️ Quota & Entitlement Gateway
=================================
الـ single source of truth لكل subscription/quota check في الـ module.
Layered enforcement:
  1. Subscription active?
  2. Entitlement (feature flag) موجود؟
  3. Monthly feature limit متعدّاش؟
  4. external_api_quota متعدّاش؟ (للـ pay-per-call فقط)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from django.db import transaction

from clients.services.entitlements import EntitlementService


# Feature codes used by this module
FEATURE_LIVE_DATA = 'diagnostics_live_data'
FEATURE_GUIDED_TESTS = 'diagnostics_guided_tests'
FEATURE_PARTS_FINDER = 'diagnostics_smart_parts_finder'
FEATURE_EXTERNAL_API = 'diagnostics_external_api_scans'


@dataclass
class QuotaCheckResult:
    allowed: bool
    reason: str = ''
    upgrade_required: bool = False
    feature_code: str = ''


class DiagnosticsQuotaService:
    """Stateless. كل الـ methods classmethods."""

    @classmethod
    def _subscription(cls, tenant):
        try:
            return getattr(tenant, 'subscription', None)
        except Exception:
            return None

    @classmethod
    def check_feature(cls, tenant, feature_code: str) -> QuotaCheckResult:
        """التحقق العام: subscription active + feature enabled."""
        sub = cls._subscription(tenant)
        if sub is None or not sub.is_active:
            return QuotaCheckResult(
                allowed=False,
                reason='Subscription غير مفعّل',
                upgrade_required=True,
                feature_code=feature_code,
            )
        if not EntitlementService.has(tenant, feature_code):
            return QuotaCheckResult(
                allowed=False,
                reason=f'Feature {feature_code} غير مفعّل في الباقة الحالية',
                upgrade_required=True,
                feature_code=feature_code,
            )
        return QuotaCheckResult(allowed=True, feature_code=feature_code)

    @classmethod
    def check_external_api_quota(cls, tenant) -> QuotaCheckResult:
        """قبل أي external API call (DTC pay-per-call أو VIN paid decoder)."""
        gate = cls.check_feature(tenant, FEATURE_EXTERNAL_API)
        if not gate.allowed:
            return gate

        sub = cls._subscription(tenant)
        if sub.diag_api_quota_remaining <= 0:
            return QuotaCheckResult(
                allowed=False,
                reason='نفدت حصة الفحوصات الخارجية لهذا الشهر',
                upgrade_required=True,
                feature_code=FEATURE_EXTERNAL_API,
            )
        return QuotaCheckResult(allowed=True, feature_code=FEATURE_EXTERNAL_API)

    @classmethod
    @transaction.atomic
    def consume_external_api_quota(cls, tenant, amount: int = 1) -> bool:
        """Deduct atomically. يـ return False لو الحصة نفدت بالفعل (race)."""
        from clients.models import TenantSubscription
        try:
            sub = (
                TenantSubscription.objects
                .select_for_update()
                .get(tenant=tenant)
            )
        except TenantSubscription.DoesNotExist:
            return False
        if sub.diag_api_quota_remaining < amount:
            return False
        sub.diag_api_quota_remaining -= amount
        sub.diag_api_scans_used_total += amount
        sub.save(update_fields=[
            'diag_api_quota_remaining', 'diag_api_scans_used_total',
        ])
        return True
