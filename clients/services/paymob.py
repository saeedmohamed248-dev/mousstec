"""
Paymob payment gateway service — single source of truth for iframe creation.

All Paymob integrations (SaaS subscriptions, Design Store, Parts Marketplace,
Customer Diagnostics) call into ``create_iframe_url`` here so the auth → order
→ payment-key handshake lives in one place and HMAC behavior stays uniform.

Returned iframe URL is short-lived (Paymob's payment_token expires in 1h).
Callers should redirect immediately — never persist the URL.
"""
from __future__ import annotations

import logging
import os
import uuid
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger('mouss_tec_core')


def _resolve_credentials() -> tuple[str, str, str]:
    """Pull Paymob credentials from settings → env, in that order."""
    api_key        = getattr(settings, 'PAYMOB_API_KEY', '')        or os.getenv('PAYMOB_API_KEY', '')
    integration_id = getattr(settings, 'PAYMOB_INTEGRATION_ID', '') or os.getenv('PAYMOB_INTEGRATION_ID', '')
    iframe_id      = getattr(settings, 'PAYMOB_IFRAME_ID', '')      or os.getenv('PAYMOB_IFRAME_ID', '')
    return api_key, integration_id, iframe_id


def create_iframe_url(
    *,
    amount_egp,
    customer_phone: Optional[str] = None,
    customer_name: Optional[str]  = None,
    customer_email: Optional[str] = None,
    order_ref: Optional[str] = None,
    callback_url: Optional[str] = None,  # noqa: ARG001 — Paymob uses dashboard-configured callbacks
    item_name: str = 'Mouss Tec Purchase',
    metadata: Optional[dict] = None,
    cache_key_prefix: Optional[str] = None,
) -> str:
    """
    Build a Paymob iframe URL for the given amount.

    Raises ``RuntimeError`` with a user-facing Arabic message on any failure
    so callers can surface a clean error to the UI.

    Parameters
    ----------
    amount_egp : Decimal | float | str
        Amount in EGP (not cents). Will be converted internally.
    customer_phone, customer_name, customer_email :
        Used in Paymob billing block. Sensible defaults if missing.
    order_ref :
        Merchant order id — appended with a uuid suffix for uniqueness.
    metadata :
        Stored in cache under ``{cache_key_prefix}_{paymob_order_id}`` so
        the callback view can route by order id. 2-hour TTL.
    cache_key_prefix :
        e.g. 'paymob_diag', 'paymob_design'. Required if metadata is given.
    """
    import requests as http_requests

    api_key, integration_id, iframe_id = _resolve_credentials()
    if not api_key:
        raise RuntimeError("بوابة الدفع غير مهيّأة (PAYMOB_API_KEY مفقود).")
    try:
        integration_id_int = int(integration_id)
    except (TypeError, ValueError):
        raise RuntimeError("إعدادات بوابة الدفع غير صحيحة.")
    if not iframe_id:
        raise RuntimeError("إعدادات بوابة الدفع غير مكتملة (PAYMOB_IFRAME_ID مفقود).")

    amount_cents = int(Decimal(str(amount_egp)) * 100)
    if amount_cents <= 0:
        raise RuntimeError("المبلغ غير صحيح.")

    merchant_order_id = f"{order_ref or 'mt'}_{uuid.uuid4().hex[:10]}"

    # Build billing block — Paymob requires every field, fall back to 'NA' for
    # what we don't have (shipping is not used for digital goods).
    name_parts = (customer_name or 'Customer').split(maxsplit=1)
    first_name = (name_parts[0][:50]) or 'Customer'
    last_name  = (name_parts[1][:50] if len(name_parts) > 1 else 'MoussTec')
    phone = (customer_phone or '01000000000').lstrip('+')
    email = (customer_email or 'customer@mousstec.com')
    billing = {
        'first_name': first_name, 'last_name': last_name,
        'email': email, 'phone_number': phone,
        'apartment': 'NA', 'floor': 'NA', 'street': 'NA', 'building': 'NA',
        'shipping_method': 'NA', 'postal_code': 'NA', 'city': 'Cairo',
        'country': 'EG', 'state': 'Cairo',
    }

    try:
        # 1) Auth
        auth_res = http_requests.post(
            'https://accept.paymob.com/api/auth/tokens',
            json={'api_key': api_key}, timeout=15,
        )
        if auth_res.status_code not in (200, 201):
            logger.error("[PAYMOB] auth failed: %s — %s", auth_res.status_code, auth_res.text[:200])
            raise RuntimeError("فشل المصادقة مع بوابة الدفع.")
        auth_token = auth_res.json().get('token')
        if not auth_token:
            raise RuntimeError("بوابة الدفع لم ترسل رمز المصادقة.")

        # 2) Order
        order_res = http_requests.post(
            'https://accept.paymob.com/api/ecommerce/orders',
            json={
                'auth_token': auth_token, 'delivery_needed': 'false',
                'amount_cents': amount_cents, 'currency': 'EGP',
                'items': [{'name': item_name, 'amount_cents': amount_cents, 'quantity': '1'}],
                'merchant_order_id': merchant_order_id,
            },
            timeout=15,
        )
        if order_res.status_code not in (200, 201):
            logger.error("[PAYMOB] order failed: %s — %s", order_res.status_code, order_res.text[:200])
            raise RuntimeError("فشل إنشاء طلب الدفع.")
        paymob_order_id = order_res.json().get('id')
        if not paymob_order_id:
            raise RuntimeError("بوابة الدفع لم ترسل رقم الطلب.")

        # 3) Payment key
        key_res = http_requests.post(
            'https://accept.paymob.com/api/acceptance/payment_keys',
            json={
                'auth_token': auth_token, 'amount_cents': amount_cents,
                'expiration': 3600, 'order_id': paymob_order_id,
                'billing_data': billing, 'currency': 'EGP',
                'integration_id': integration_id_int,
                'lock_order_when_paid': 'true',
            },
            timeout=15,
        )
        if key_res.status_code not in (200, 201):
            logger.error("[PAYMOB] key failed: %s — %s", key_res.status_code, key_res.text[:200])
            raise RuntimeError("فشل إصدار رمز الدفع.")
        payment_token = key_res.json().get('token')
        if not payment_token:
            raise RuntimeError("بوابة الدفع لم ترسل رمز الدفع.")

        # 4) Persist metadata for the callback to route this transaction
        if metadata and cache_key_prefix:
            cache.set(f'{cache_key_prefix}_{paymob_order_id}', metadata, timeout=7200)

        iframe_url = f'https://accept.paymob.com/api/acceptance/iframes/{iframe_id}?payment_token={payment_token}'
        logger.info("[PAYMOB] iframe ready: order_id=%s amount=%s EGP merchant_ref=%s",
                    paymob_order_id, amount_egp, merchant_order_id)
        return iframe_url

    except http_requests.Timeout:
        logger.error("[PAYMOB] timeout")
        raise RuntimeError("بوابة الدفع لا تستجيب. حاول لاحقاً.")
    except http_requests.RequestException as exc:
        logger.exception("[PAYMOB] network error: %s", exc)
        raise RuntimeError("تعذر الاتصال ببوابة الدفع.")
