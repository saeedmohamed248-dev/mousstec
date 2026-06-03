"""
🔌 External API Adapters (API Gateway Pattern)
================================================
الـ core logic ميـ couple مع أي provider بعينه. كل provider
بـ يـ implement الـ ABC، والـ DTCResolver/VINDecoder بـ يـ depend
على الـ abstraction بس.

Providers:
  - NHTSAVinDecoder: free public API (vpic.nhtsa.dot.gov)
  - CarMDDTCProvider: pay-per-call (~$0.01-0.10)
  - MockDTCProvider: للـ tests والـ dev

التكلفة بـ تجي من APICostRate.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import requests

logger = logging.getLogger('mouss_tec_core')


@dataclass
class DTCLookupResult:
    """ناتج موحد من أي DTC provider."""
    code: str
    short_description: str = ''
    full_description: str = ''
    severity: str = 'medium'
    guided_steps: list = field(default_factory=list)
    likely_oem_parts: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    cost_usd: Decimal = Decimal('0')
    provider: str = 'unknown'


@dataclass
class VINDecodeResult:
    vin: str
    make: str = ''
    model: str = ''
    model_year: Optional[int] = None
    engine: str = ''
    raw: dict = field(default_factory=dict)
    cost_usd: Decimal = Decimal('0')
    provider: str = 'unknown'


# ──────────────────────────────────────────────────────────────
class AbstractDTCProvider(ABC):
    """كل DTC provider لازم يـ implement الـ interface ده."""

    provider_name: str = 'abstract'
    endpoint_key: str = 'dtc_lookup'

    @abstractmethod
    def lookup(self, dtc_code: str, vehicle_signature: str = '') -> DTCLookupResult:
        ...

    def get_cost(self) -> Decimal:
        """ترجع التكلفة من APICostRate (cached lookup)."""
        from diagnostics_catalog.models import APICostRate
        try:
            rate = APICostRate.objects.get(
                provider=self.provider_name,
                endpoint=self.endpoint_key,
                is_active=True,
            )
            return rate.cost_usd
        except APICostRate.DoesNotExist:
            return Decimal('0')


class AbstractVINDecoder(ABC):
    provider_name: str = 'abstract'
    endpoint_key: str = 'vin_decode'

    @abstractmethod
    def decode(self, vin: str) -> VINDecodeResult:
        ...

    def get_cost(self) -> Decimal:
        from diagnostics_catalog.models import APICostRate
        try:
            rate = APICostRate.objects.get(
                provider=self.provider_name,
                endpoint=self.endpoint_key,
                is_active=True,
            )
            return rate.cost_usd
        except APICostRate.DoesNotExist:
            return Decimal('0')


# ──────────────────────────────────────────────────────────────
class NHTSAVinDecoder(AbstractVINDecoder):
    """NHTSA vPIC — free public API. Cost = 0."""

    provider_name = 'nhtsa'
    BASE_URL = 'https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin'

    def decode(self, vin: str) -> VINDecodeResult:
        url = f"{self.BASE_URL}/{vin}?format=json"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        result = VINDecodeResult(vin=vin, raw=data, provider=self.provider_name)
        for row in data.get('Results', []):
            var = row.get('Variable')
            val = row.get('Value') or ''
            if var == 'Make':
                result.make = val
            elif var == 'Model':
                result.model = val
            elif var == 'Model Year' and val:
                try:
                    result.model_year = int(val)
                except ValueError:
                    pass
            elif var == 'Engine Model' and val:
                result.engine = val
        return result


# ──────────────────────────────────────────────────────────────
class CarMDDTCProvider(AbstractDTCProvider):
    """Pay-per-call DTC provider (CarMD-style). Stub جاهز للـ wiring
    لما تتـ activate الـ credentials في settings."""

    provider_name = 'carmd'

    def __init__(self, auth_key: str = '', partner_token: str = ''):
        self.auth_key = auth_key
        self.partner_token = partner_token

    def lookup(self, dtc_code: str, vehicle_signature: str = '') -> DTCLookupResult:
        if not self.auth_key:
            raise RuntimeError(
                "CarMD credentials غير مضبوطة — set CARMD_AUTH_KEY في .env"
            )
        headers = {
            'authorization': f'Basic {self.auth_key}',
            'partner-token': self.partner_token,
        }
        params = {'dtc': dtc_code}
        if vehicle_signature:
            params['vehicle'] = vehicle_signature
        resp = requests.get(
            'https://api.carmd.com/v3.0/diagnostic',
            headers=headers, params=params, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # Normalize — actual CarMD schema may need adjustment per docs
        payload = data.get('data', {}) or {}
        return DTCLookupResult(
            code=dtc_code,
            short_description=payload.get('description', ''),
            full_description=payload.get('repair', ''),
            severity=payload.get('severity', 'medium'),
            guided_steps=payload.get('steps', []) or [],
            likely_oem_parts=payload.get('parts', []) or [],
            raw=data,
            cost_usd=self.get_cost(),
            provider=self.provider_name,
        )


# ──────────────────────────────────────────────────────────────
class MockDTCProvider(AbstractDTCProvider):
    """للـ tests والـ dev — بـ يـ return نتيجة ثابتة بدون network."""

    provider_name = 'mock'

    def __init__(self, fixed_response: Optional[dict] = None):
        self.fixed_response = fixed_response or {}
        self.call_count = 0

    def lookup(self, dtc_code: str, vehicle_signature: str = '') -> DTCLookupResult:
        self.call_count += 1
        return DTCLookupResult(
            code=dtc_code,
            short_description=self.fixed_response.get('short', f'Mock description for {dtc_code}'),
            full_description=self.fixed_response.get('full', ''),
            severity=self.fixed_response.get('severity', 'medium'),
            guided_steps=self.fixed_response.get('steps', []),
            likely_oem_parts=self.fixed_response.get('parts', []),
            raw={'mock': True, 'code': dtc_code, 'sig': vehicle_signature},
            cost_usd=Decimal('0.05'),
            provider=self.provider_name,
        )


def get_default_dtc_provider() -> AbstractDTCProvider:
    """Factory — يـ return الـ provider المضبوط في settings."""
    from django.conf import settings
    provider = getattr(settings, 'DIAGNOSTICS_DTC_PROVIDER', 'mock')
    if provider == 'carmd':
        return CarMDDTCProvider(
            auth_key=getattr(settings, 'CARMD_AUTH_KEY', ''),
            partner_token=getattr(settings, 'CARMD_PARTNER_TOKEN', ''),
        )
    return MockDTCProvider()


def get_default_vin_decoder() -> AbstractVINDecoder:
    return NHTSAVinDecoder()
