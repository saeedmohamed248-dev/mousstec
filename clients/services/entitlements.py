"""
🎯 EntitlementService — Phase 1: Hybrid Feature Catalog Gateway
================================================================
Service طبقة واحدة بتـ wrap الـ entitlements lookup للـ tenants.
كل callsite في النظام مفروض يستخدم الـ methods دي بدل ما يقرأ
`plan.entitlements` مباشرة. ده بيضمن إن:

  1. الـ logic موحدة (caching، fallback، grandfathering لاحقاً).
  2. التغييرات في Phase 2/3 (مثلاً إضافة TenantSubscription.locked_entitlements
     للـ grandfathering) ميـ break أي callsite.
  3. الـ tests تقدر mock layer واحدة.

Usage:
    from clients.services.entitlements import EntitlementService

    if EntitlementService.has(tenant, 'b2b_marketplace'):
        # عرض الـ B2B UI

    limit = EntitlementService.limit(tenant, 'workshop_repair_cards')
    # → 150 أو None لو unlimited/بدون limit
"""
from __future__ import annotations

import logging
from typing import Optional, Iterable

logger = logging.getLogger('mouss_tec_core')


class EntitlementService:
    """Stateless service — كل الـ methods classmethods."""

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────
    @classmethod
    def has(cls, tenant, feature_code: str) -> bool:
        """
        هل الـ tenant عنده الـ feature ده مفعّل؟
        بنرجع False لو:
          - tenant بدون TenantSubscription
          - subscription بدون Plan
          - الـ feature غير موجود في entitlements
          - الـ feature موجود بـ enabled=False
        """
        config = cls._lookup(tenant, feature_code)
        if config is None:
            return False
        return bool(config.get('enabled', False))

    @classmethod
    def limit(cls, tenant, feature_code: str) -> Optional[int]:
        """
        ترجع الـ monthly_limit للـ feature ده على الـ tenant، أو None لو:
          - الـ feature مش quantitative
          - مفيش limit مضبوط (= unlimited)
          - الـ tenant مفيهوش الـ feature
        """
        config = cls._lookup(tenant, feature_code)
        if config is None or not config.get('enabled', False):
            return None
        raw = config.get('monthly_limit')
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                f"[Entitlements] tenant={getattr(tenant, 'schema_name', '?')} "
                f"feature={feature_code}: monthly_limit invalid ({raw!r})"
            )
            return None

    @classmethod
    def all_for_tenant(cls, tenant) -> dict:
        """ترجع dict كامل بـ entitlements الـ tenant الحالية (مفيدة للـ UI/debugging)."""
        plan = cls._get_plan(tenant)
        if plan is None:
            return {}
        return dict(plan.entitlements or {})

    @classmethod
    def validate_codes(cls, codes: Iterable[str]) -> list[str]:
        """
        ترجع list بالـ codes اللي مش في الـ catalog (للتحقق قبل الـ save).
        أي callsite عاوز يـ enforce input validation يستخدم ده.
        """
        from clients.models import Feature
        valid = set(Feature.objects.filter(is_active=True).values_list('code', flat=True))
        return [c for c in codes if c not in valid]

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────
    @classmethod
    def _get_plan(cls, tenant):
        """Resolve الـ effective Plan للـ tenant. Phase 1: من TenantSubscription.plan.
        Phase 3 هيـ override لـ TenantSubscription.locked_entitlements عشان الـ grandfathering.
        """
        if tenant is None or not getattr(tenant, 'pk', None):
            return None
        try:
            sub = getattr(tenant, 'subscription', None)
        except Exception:
            sub = None
        if sub is None:
            return None
        return sub.plan if sub.plan_id else None

    @classmethod
    def _lookup(cls, tenant, feature_code: str) -> Optional[dict]:
        """Return raw config dict for a feature on a tenant, or None if absent."""
        plan = cls._get_plan(tenant)
        if plan is None:
            return None
        entitlements = plan.entitlements or {}
        if not isinstance(entitlements, dict):
            return None
        return entitlements.get(feature_code)
