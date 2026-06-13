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
from functools import wraps
from typing import Optional, Iterable

from django.db import connection
from django.http import HttpResponseForbidden, JsonResponse

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
        """ترجع dict كامل بـ entitlements الـ tenant الـ effective
        (snapshot لو موجود، وإلا Plan الحالي)."""
        sub = cls._get_subscription(tenant)
        if sub is None:
            return {}
        return dict(sub.effective_entitlements)

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
    def _get_subscription(cls, tenant):
        """Resolve الـ TenantSubscription للـ tenant. الـ source of truth الجديد
        للـ entitlements (مش الـ Plan مباشرة) عشان نـ honor الـ grandfathering."""
        if tenant is None or not getattr(tenant, 'pk', None):
            return None
        try:
            sub = getattr(tenant, 'subscription', None)
        except Exception:
            sub = None
        return sub

    @classmethod
    def _lookup(cls, tenant, feature_code: str) -> Optional[dict]:
        """Return raw config dict for a feature on a tenant, or None if absent.

        🎯 Phase 2: يقرأ من sub.effective_entitlements اللي بتـ pick:
          - locked_entitlements لو الـ snapshot موجود (grandfathering)
          - plan.entitlements كـ fallback (للـ tenants من قبل ما الـ Phase 2
            backfill يجري — defensive نظراً لأن الـ data migration بتـ snapshot
            كل الـ subscriptions الموجودة)
        """
        sub = cls._get_subscription(tenant)
        if sub is None:
            return None
        entitlements = sub.effective_entitlements
        if not isinstance(entitlements, dict):
            return None
        return entitlements.get(feature_code)


# ─────────────────────────────────────────────────────────────────────
# View decorator
# ─────────────────────────────────────────────────────────────────────
def require_feature(feature_code: str, *, upgrade_url: str = '/pricing/'):
    """🛡️ View decorator — blocks tenants whose plan doesn't include the feature.

    Use after ``@login_required`` and ``@tenant_required`` so the request is
    guaranteed to have a tenant. Resolves the tenant from
    ``connection.tenant`` (django-tenants) and consults
    :class:`EntitlementService`.

    Behavior on deny:
      * JSON requests (``Accept: application/json`` or path under ``/api/``,
        ``/system/api/``) → 403 JSON ``{error, code, upgrade_url}``
      * Browser requests → 403 HTML with a link to the pricing page

    Usage::

        @login_required
        @tenant_required
        @require_feature('b2b_marketplace')
        def b2b_marketplace(request):
            ...
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            tenant = getattr(connection, 'tenant', None)
            if tenant is None or getattr(tenant, 'schema_name', 'public') == 'public':
                # Defensive — caller forgot @tenant_required. Don't leak.
                return HttpResponseForbidden(
                    '🛑 هذه الخدمة مخصصة للحسابات المسجلة فقط.',
                )
            if EntitlementService.has(tenant, feature_code):
                return view_func(request, *args, **kwargs)

            logger.info(
                f"[require_feature] denied tenant={tenant.schema_name} "
                f"feature={feature_code} path={request.path}"
            )
            wants_json = (
                request.path.startswith('/api/')
                or request.path.startswith('/system/api/')
                or 'application/json' in request.headers.get('Accept', '')
            )
            if wants_json:
                return JsonResponse({
                    'error': 'هذه الميزة غير متاحة في باقتك الحالية.',
                    'code': 'feature_not_in_plan',
                    'feature': feature_code,
                    'upgrade_url': upgrade_url,
                }, status=403)
            return HttpResponseForbidden(
                f'<h1>🔒 الميزة غير متاحة في باقتك</h1>'
                f'<p>الميزة المطلوبة (<code>{feature_code}</code>) جزء من '
                f'باقة أعلى. <a href="{upgrade_url}">ترقّى الآن</a>.</p>'
            )
        return _wrapped
    return decorator
