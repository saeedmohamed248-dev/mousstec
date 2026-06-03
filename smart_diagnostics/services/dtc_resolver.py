"""
🧠 DTC Resolver — Cache-First Cost-Aware Lookup
================================================
Flow:
  1. local DTCDefinition catalog (free, instant)
  2. shared DTCExternalLookupCache (free, persisted external response)
  3. external provider (pay-per-call) — gated by quota
  4. permanent persist into shared cache + catalog upsert
  5. APICallLog audit (per-tenant)

كل request external بـ يـ deduct 1 من الـ tenant quota.
Duplicate requests لنفس (dtc, vehicle_signature) مستحيل تـ hit
الـ external — الـ unique constraint في DTCExternalLookupCache
بـ يضمن ده على مستوى DB.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import F

from diagnostics_catalog.models import (
    DTCDefinition,
    DTCExternalLookupCache,
    VINDecodeCache,
)
from smart_diagnostics.models import APICallLog
from smart_diagnostics.services.adapters import (
    AbstractDTCProvider,
    AbstractVINDecoder,
    DTCLookupResult,
    VINDecodeResult,
    get_default_dtc_provider,
    get_default_vin_decoder,
)
from smart_diagnostics.services.quota import DiagnosticsQuotaService, QuotaCheckResult

logger = logging.getLogger('mouss_tec_core')


@dataclass
class ResolvedDTC:
    code: str
    short_description: str
    full_description: str
    severity: str
    guided_steps: list
    likely_oem_parts: list
    source: str  # 'catalog' | 'cache' | 'external'
    cost_charged_usd: Decimal = Decimal('0')


class DTCResolver:
    """Cache-first resolver. يـ enforce quota + يـ log كل call."""

    def __init__(
        self,
        tenant,
        provider: Optional[AbstractDTCProvider] = None,
        user=None,
    ):
        self.tenant = tenant
        self.provider = provider or get_default_dtc_provider()
        self.user = user

    def resolve(
        self,
        dtc_code: str,
        vehicle_signature: str = '',
        allow_external: bool = True,
    ) -> tuple[Optional[ResolvedDTC], Optional[QuotaCheckResult]]:
        """
        ترجع (ResolvedDTC | None, QuotaCheckResult | None).
        - لو لقاها locally → (resolved, None) — مفيش quota check محتاج.
        - لو محتاجة external والـ quota منعها → (None, denial).
        - لو external نجح → (resolved, None).
        """
        dtc_code = dtc_code.strip().upper()

        # 1. Local catalog (zero cost)
        defn = DTCDefinition.objects.filter(code=dtc_code).first()
        if defn:
            self._log_call(
                endpoint='dtc_lookup', dtc=dtc_code,
                cache_hit=True, cost=Decimal('0'), provider='local_catalog',
            )
            return self._from_definition(defn, source='catalog'), None

        # 2. Shared external cache (zero cost — already paid for in a previous lifetime)
        cache_row = DTCExternalLookupCache.objects.filter(
            dtc_code=dtc_code,
            vehicle_signature=vehicle_signature,
            provider=self.provider.provider_name,
        ).first()
        if cache_row:
            self._log_call(
                endpoint='dtc_lookup', dtc=dtc_code,
                cache_hit=True, cost=Decimal('0'),
                provider=self.provider.provider_name,
            )
            return self._from_cache(cache_row), None

        if not allow_external:
            return None, QuotaCheckResult(
                allowed=False, reason='Not in local catalog/cache and external disabled.',
                feature_code='diagnostics_external_api_scans',
            )

        # 3. External call — gated by quota
        gate = DiagnosticsQuotaService.check_external_api_quota(self.tenant)
        if not gate.allowed:
            return None, gate

        consumed = DiagnosticsQuotaService.consume_external_api_quota(self.tenant)
        if not consumed:
            return None, QuotaCheckResult(
                allowed=False, reason='نفدت الحصة (race)',
                upgrade_required=True,
                feature_code='diagnostics_external_api_scans',
            )

        # 4. Call provider
        try:
            result = self.provider.lookup(dtc_code, vehicle_signature)
        except Exception as e:
            logger.error(f"[DTCResolver] external call failed: {e}")
            self._log_call(
                endpoint='dtc_lookup', dtc=dtc_code,
                cache_hit=False, cost=Decimal('0'),
                provider=self.provider.provider_name,
                error=str(e)[:200],
            )
            # Refund the quota — the provider failed
            from clients.models import TenantSubscription
            TenantSubscription.objects.filter(tenant=self.tenant).update(
                diag_api_quota_remaining=F('diag_api_quota_remaining') + 1,
            )
            raise

        # 5. Persist permanently (both shared cache + upsert into catalog)
        self._persist_external_result(result, dtc_code, vehicle_signature)
        self._log_call(
            endpoint='dtc_lookup', dtc=dtc_code,
            cache_hit=False, cost=result.cost_usd,
            provider=result.provider,
        )

        return ResolvedDTC(
            code=dtc_code,
            short_description=result.short_description,
            full_description=result.full_description,
            severity=result.severity,
            guided_steps=result.guided_steps,
            likely_oem_parts=result.likely_oem_parts,
            source='external',
            cost_charged_usd=result.cost_usd,
        ), None

    # ── helpers ─────────────────────────────────────────────────
    def _from_definition(self, defn: DTCDefinition, source: str) -> ResolvedDTC:
        return ResolvedDTC(
            code=defn.code,
            short_description=defn.short_description,
            full_description=defn.full_description,
            severity=defn.severity,
            guided_steps=defn.guided_steps or [],
            likely_oem_parts=defn.likely_oem_parts or [],
            source=source,
        )

    def _from_cache(self, row: DTCExternalLookupCache) -> ResolvedDTC:
        p = row.payload or {}
        return ResolvedDTC(
            code=row.dtc_code,
            short_description=p.get('short_description', ''),
            full_description=p.get('full_description', ''),
            severity=p.get('severity', 'medium'),
            guided_steps=p.get('guided_steps', []) or [],
            likely_oem_parts=p.get('likely_oem_parts', []) or [],
            source='cache',
        )

    @transaction.atomic
    def _persist_external_result(
        self, r: DTCLookupResult, dtc_code: str, vehicle_signature: str,
    ):
        payload = {
            'short_description': r.short_description,
            'full_description': r.full_description,
            'severity': r.severity,
            'guided_steps': r.guided_steps,
            'likely_oem_parts': r.likely_oem_parts,
        }
        DTCExternalLookupCache.objects.update_or_create(
            dtc_code=dtc_code,
            vehicle_signature=vehicle_signature,
            provider=r.provider,
            defaults={'payload': payload},
        )
        # Upsert into the public catalog so the next tenant gets it free
        DTCDefinition.objects.update_or_create(
            code=dtc_code,
            defaults={
                'short_description': r.short_description or dtc_code,
                'full_description': r.full_description,
                'severity': r.severity,
                'guided_steps': r.guided_steps,
                'likely_oem_parts': r.likely_oem_parts,
                'source': r.provider,
                'system': dtc_code[0] if dtc_code and dtc_code[0] in 'PCBU' else 'P',
            },
        )

    def _log_call(
        self, endpoint: str, dtc: str = '', vin: str = '',
        cache_hit: bool = False, cost: Decimal = Decimal('0'),
        provider: str = '', error: str = '',
    ):
        APICallLog.objects.create(
            provider=provider or self.provider.provider_name,
            endpoint=endpoint,
            dtc_code=dtc,
            vin=vin,
            cache_hit=cache_hit,
            cost_usd=cost,
            error=error,
            triggered_by=self.user if (self.user and self.user.is_authenticated) else None,
        )


# ──────────────────────────────────────────────────────────────
class VINResolver:
    """Same pattern for VINs. NHTSA is free → almost always allowed."""

    def __init__(
        self,
        tenant,
        decoder: Optional[AbstractVINDecoder] = None,
        user=None,
    ):
        self.tenant = tenant
        self.decoder = decoder or get_default_vin_decoder()
        self.user = user

    def resolve(self, vin: str) -> tuple[Optional[VINDecodeCache], Optional[QuotaCheckResult]]:
        vin = vin.strip().upper()
        cached = VINDecodeCache.objects.filter(vin=vin).first()
        if cached:
            APICallLog.objects.create(
                provider=cached.provider, endpoint='vin_decode',
                vin=vin, cache_hit=True, cost_usd=Decimal('0'),
                triggered_by=self.user if (self.user and self.user.is_authenticated) else None,
            )
            return cached, None

        # NHTSA = free, no quota needed. For paid decoders, gate here.
        cost = self.decoder.get_cost()
        if cost > 0:
            gate = DiagnosticsQuotaService.check_external_api_quota(self.tenant)
            if not gate.allowed:
                return None, gate
            DiagnosticsQuotaService.consume_external_api_quota(self.tenant)

        try:
            result = self.decoder.decode(vin)
        except Exception as e:
            logger.error(f"[VINResolver] decode failed: {e}")
            APICallLog.objects.create(
                provider=self.decoder.provider_name, endpoint='vin_decode',
                vin=vin, cache_hit=False, cost_usd=Decimal('0'),
                error=str(e)[:200],
                triggered_by=self.user if (self.user and self.user.is_authenticated) else None,
            )
            raise

        row, _ = VINDecodeCache.objects.update_or_create(
            vin=vin,
            defaults={
                'decoded_data': result.raw,
                'make': result.make,
                'model': result.model,
                'model_year': result.model_year,
                'engine': result.engine,
                'provider': result.provider,
            },
        )
        APICallLog.objects.create(
            provider=result.provider, endpoint='vin_decode',
            vin=vin, cache_hit=False, cost_usd=cost,
            triggered_by=self.user if (self.user and self.user.is_authenticated) else None,
        )
        return row, None
