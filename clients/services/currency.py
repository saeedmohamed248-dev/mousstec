"""💱 Currency conversion service — display layer over the EGP ledger.

كل الأرصدة متخزّنة بالجنيه. الدوال دي بتحوّل للعرض بس، مع كاش 1 ساعة
للأسعار (بيتصفّر لما webhook يوصل سعر جديد عبر update_rate).

Public API:
    convert(amount_egp, 'USD')           → Decimal (بالعملة الهدف)
    format_amount(amount_egp, 'USD')     → "$3.20" (رمز + خانات صحيحة)
    get_rate('USD')                       → Decimal | None
    update_rate('USD', Decimal('0.02'), source='webhook')
"""
from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from django.core.cache import cache

from clients.currency_models import BASE_CURRENCY, SUPPORTED_CURRENCIES

logger = logging.getLogger('mouss_tec_core')

_CACHE_TTL = 60 * 60  # ساعة
_CACHE_PREFIX = 'fx_rate:'


def is_supported(currency: str) -> bool:
    return (currency or '').upper() in SUPPORTED_CURRENCIES


def get_rate(currency: str) -> Optional[Decimal]:
    """سعر صرف EGP → currency. Base currency = 1. None لو مش متوفر."""
    currency = (currency or '').upper()
    if currency == BASE_CURRENCY:
        return Decimal('1')
    if not is_supported(currency):
        return None

    cache_key = f'{_CACHE_PREFIX}{currency}'
    cached = cache.get(cache_key)
    if cached is not None:
        return Decimal(str(cached))

    # الكاش miss → أحدث صف من الـ DB
    try:
        from clients.models import ExchangeRate
        from django_tenants.utils import schema_context
        with schema_context('public'):
            row = (ExchangeRate.objects
                   .filter(target_currency=currency)
                   .order_by('-fetched_at').first())
    except Exception:
        row = None
    if row is None:
        return None
    cache.set(cache_key, str(row.rate), _CACHE_TTL)
    return row.rate


def update_rate(currency: str, rate, *, source: str = 'manual') -> bool:
    """يسجّل سعر جديد ويصفّر الكاش. Returns True لو اتحفظ."""
    currency = (currency or '').upper()
    if currency == BASE_CURRENCY or not is_supported(currency):
        return False
    try:
        rate_dec = Decimal(str(rate))
        if rate_dec <= 0:
            return False
    except Exception:
        return False

    from clients.models import ExchangeRate
    from django_tenants.utils import schema_context
    with schema_context('public'):
        ExchangeRate.objects.create(
            target_currency=currency, rate=rate_dec, source=source[:50])
    cache.set(f'{_CACHE_PREFIX}{currency}', str(rate_dec), _CACHE_TTL)
    logger.info("[FX] %s rate updated → %s (source=%s)", currency, rate_dec, source)
    return True


def convert(amount_egp, currency: str) -> Optional[Decimal]:
    """يحوّل مبلغ بالجنيه لعملة الهدف. None لو السعر مش متوفر."""
    currency = (currency or '').upper()
    rate = get_rate(currency)
    if rate is None:
        return None
    decimals = SUPPORTED_CURRENCIES[currency]['decimals']
    quant = Decimal(10) ** -decimals
    return (Decimal(str(amount_egp)) * rate).quantize(quant, rounding=ROUND_HALF_UP)


def format_amount(amount_egp, currency: str) -> str:
    """يحوّل ويرجّع نص جاهز للعرض بالرمز. Fallback للجنيه لو مفيش سعر."""
    currency = (currency or '').upper()
    converted = convert(amount_egp, currency)
    if converted is None:
        # سقوط آمن للجنيه — العميل يشوف السعر الأصلي بدل خطأ
        egp = SUPPORTED_CURRENCIES[BASE_CURRENCY]
        return f"{Decimal(str(amount_egp)).quantize(Decimal('0.01'))} {egp['symbol']}"
    meta = SUPPORTED_CURRENCIES[currency]
    return f"{converted} {meta['symbol']}"


def supported_list() -> list[dict]:
    """قائمة للـ UI: [{code, symbol, name, rate}] — rate=None لو مش متوفر."""
    out = []
    for code, meta in SUPPORTED_CURRENCIES.items():
        rate = get_rate(code)
        out.append({
            'code': code, 'symbol': meta['symbol'], 'name': meta['name'],
            'rate': str(rate) if rate is not None else None,
        })
    return out
