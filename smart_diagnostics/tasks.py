"""
🔄 Celery tasks for smart_diagnostics
======================================
- monthly_refill_diag_api_quotas: مرة شهرياً (1st of month). لكل tenant
  مشترك في feature 'diagnostics_external_api_scans' بـ يـ refill حصته
  للقيمة المُعلنة في entitlements[...].monthly_limit.
- purge_old_telemetry_frames: rolling window cleanup (24 ساعة).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')


@shared_task(name='smart_diagnostics.tasks.monthly_refill_diag_api_quotas')
def monthly_refill_diag_api_quotas():
    """يـ refill diag_api_quota_remaining لكل اشتراك فعّال عنده الـ feature.

    Source of truth = effective_entitlements للـ subscription (locked
    snapshot أو plan fallback). الـ value =
    entitlements['diagnostics_external_api_scans']['monthly_limit'].
    """
    from clients.models import TenantSubscription
    from smart_diagnostics.services.quota import FEATURE_EXTERNAL_API

    refilled = 0
    skipped = 0
    for sub in TenantSubscription.objects.filter(is_active=True).select_related('plan', 'tenant'):
        entitlements = sub.effective_entitlements or {}
        feat = entitlements.get(FEATURE_EXTERNAL_API) or {}
        if not feat.get('enabled'):
            skipped += 1
            continue
        limit = feat.get('monthly_limit')
        if limit is None:
            # unlimited — set to high water mark (1_000_000) for accounting
            limit = 1_000_000
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            skipped += 1
            continue
        sub.refill_diag_api_quota(limit, reset=True)
        refilled += 1
        logger.info(
            f"🔄 [QuotaRefill] tenant={sub.tenant.schema_name} → {limit} scans"
        )
    logger.info(f"🔄 [QuotaRefill] done: refilled={refilled}, skipped={skipped}")
    return {'refilled': refilled, 'skipped': skipped}


@shared_task(name='smart_diagnostics.tasks.purge_old_telemetry_frames')
def purge_old_telemetry_frames(schema_name: str = None, hours: int = 24):
    """يـ delete LiveTelemetryFrame older than `hours` per tenant.
    Beat بـ يـ trigger per schema عن طريق kwargs.
    """
    from django_tenants.utils import schema_context, get_tenant_model
    from smart_diagnostics.models import LiveTelemetryFrame

    cutoff = timezone.now() - timedelta(hours=hours)
    total = 0
    if schema_name:
        schemas = [schema_name]
    else:
        schemas = list(
            get_tenant_model().objects.exclude(schema_name='public')
            .values_list('schema_name', flat=True)
        )
    for s in schemas:
        try:
            with schema_context(s):
                deleted, _ = LiveTelemetryFrame.objects.filter(timestamp__lt=cutoff).delete()
                total += deleted
        except Exception as e:
            logger.warning(f"[PurgeTelemetry] {s} failed: {e}")
    logger.info(f"🧹 [PurgeTelemetry] deleted {total} frames < {hours}h")
    return total
