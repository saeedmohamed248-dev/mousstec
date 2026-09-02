"""
Paymob payment gateway service — single source of truth for iframe creation
AND callback HMAC verification.

All Paymob integrations (SaaS subscriptions, Design Store, Parts Marketplace,
Customer Diagnostics) call into ``create_iframe_url`` here so the auth → order
→ payment-key handshake lives in one place and HMAC behavior stays uniform.

Returned iframe URL is short-lived (Paymob's payment_token expires in 1h).
Callers should redirect immediately — never persist the URL.

For inbound payment-confirmation callbacks every view MUST call
``verify_paymob_hmac(request)`` before trusting any ``success`` field.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# HMAC verification — single fail-closed implementation
# ─────────────────────────────────────────────────────────────────────

# Paymob's documented HMAC field order (alphabetical by key name).
# Both GET (transaction-processed) and POST (transaction-processed-callback)
# concatenate these same fields in this order, then HMAC-SHA512 with the
# integration's HMAC secret.
_PAYMOB_HMAC_FIELDS = (
    'amount_cents', 'created_at', 'currency', 'error_occured',
    'has_parent_transaction', 'id', 'integration_id',
    'is_3d_secure', 'is_auth', 'is_capture', 'is_refunded',
    'is_standalone_payment', 'is_voided',
    'order_id', 'owner', 'pending',
    'source_data_pan', 'source_data_sub_type', 'source_data_type',
    'success',
)


def _hmac_value(v) -> str:
    """Render one HMAC field exactly as Paymob concatenated it.

    Paymob signs the raw JSON representation: booleans are lowercase
    ``true``/``false`` and null is the empty string. ``json.loads`` gives us
    Python ``True``/``False``/``None``, so a plain ``str()`` would produce
    ``"True"``/``"None"`` and every POST callback would fail verification.
    GET params arrive as strings already and pass through unchanged.
    """
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'true' if v else 'false'
    return str(v)


def _is_production() -> bool:
    """True unless explicitly running in a non-production environment.
    Defaults to production so HMAC enforcement is strict by default."""
    env_marker = os.getenv('DJANGO_ENV', '').lower()
    if env_marker in ('production', 'prod', ''):
        return True
    return False


def _extract_paymob_fields(body_data: dict, get_params: dict) -> dict:
    """Normalize the Paymob payload to a flat dict keyed by HMAC field names.

    Handles both shapes:
      • POST  → ``{'obj': {...nested...}}`` with ``obj.order.id`` and
                ``obj.source_data.{pan,sub_type,type}``.
      • GET   → flat query params with ``order``, ``source_data.pan`` etc.
    """
    obj = body_data.get('obj') if isinstance(body_data.get('obj'), dict) else {}
    if obj:
        order_obj = obj.get('order') if isinstance(obj.get('order'), dict) else {}
        source_data = obj.get('source_data') if isinstance(obj.get('source_data'), dict) else {}
        return {
            'amount_cents':          obj.get('amount_cents', ''),
            'created_at':            obj.get('created_at', ''),
            'currency':              obj.get('currency', ''),
            'error_occured':         obj.get('error_occured', ''),
            'has_parent_transaction': obj.get('has_parent_transaction', ''),
            'id':                    obj.get('id', ''),
            'integration_id':        obj.get('integration_id', ''),
            'is_3d_secure':          obj.get('is_3d_secure', ''),
            'is_auth':               obj.get('is_auth', ''),
            'is_capture':            obj.get('is_capture', ''),
            'is_refunded':           obj.get('is_refunded', ''),
            'is_standalone_payment': obj.get('is_standalone_payment', ''),
            'is_voided':             obj.get('is_voided', ''),
            'order_id':              order_obj.get('id', ''),
            'owner':                 obj.get('owner', ''),
            'pending':               obj.get('pending', ''),
            'source_data_pan':       source_data.get('pan', ''),
            'source_data_sub_type':  source_data.get('sub_type', ''),
            'source_data_type':      source_data.get('type', ''),
            'success':               obj.get('success', ''),
        }
    # GET-style flat params
    src = get_params or body_data
    return {
        'amount_cents':          src.get('amount_cents', ''),
        'created_at':            src.get('created_at', ''),
        'currency':              src.get('currency', ''),
        'error_occured':         src.get('error_occured', ''),
        'has_parent_transaction': src.get('has_parent_transaction', ''),
        'id':                    src.get('id', ''),
        'integration_id':        src.get('integration_id', ''),
        'is_3d_secure':          src.get('is_3d_secure', ''),
        'is_auth':               src.get('is_auth', ''),
        'is_capture':            src.get('is_capture', ''),
        'is_refunded':           src.get('is_refunded', ''),
        'is_standalone_payment': src.get('is_standalone_payment', ''),
        'is_voided':             src.get('is_voided', ''),
        'order_id':              src.get('order', ''),
        'owner':                 src.get('owner', ''),
        'pending':               src.get('pending', ''),
        'source_data_pan':       src.get('source_data.pan', src.get('source_data_pan', '')),
        'source_data_sub_type':  src.get('source_data.sub_type', src.get('source_data_sub_type', '')),
        'source_data_type':      src.get('source_data.type', src.get('source_data_type', '')),
        'success':               src.get('success', ''),
    }


def verify_paymob_hmac(request, body_data: Optional[dict] = None) -> tuple[bool, str]:
    """🛡️ Verify Paymob callback HMAC. Fail-closed by default.

    Behavior matrix:
        secret unset  + production  → REJECT (logs CRITICAL).
        secret unset  + non-prod    → ACCEPT (logs WARNING, dev-only path).
        secret set    + no hmac     → REJECT.
        secret set    + hmac wrong  → REJECT (logs CRITICAL — forgery attempt).
        secret set    + hmac match  → ACCEPT.

    To override the dev-mode skip, set env ``PAYMOB_REQUIRE_HMAC=1`` —
    then unset secret rejects in every environment.

    Returns ``(ok, reason)``. ``reason`` is a short machine-readable token
    suitable for redirect query-string or JSON error responses.
    """
    secret = (
        getattr(settings, 'PAYMOB_HMAC_SECRET', '')
        or os.getenv('PAYMOB_HMAC_SECRET', '')
    )
    body_data = body_data if body_data is not None else {}
    if not body_data and request.method != 'GET' and request.body:
        try:
            body_data = json.loads(request.body)
        except (ValueError, TypeError):
            body_data = {}
    received = request.GET.get('hmac', '') or body_data.get('hmac', '')

    if not secret:
        force_required = os.getenv('PAYMOB_REQUIRE_HMAC', '').lower() in ('1', 'true', 'yes')
        if _is_production() or force_required:
            logger.critical(
                "🚨 [PAYMOB HMAC] PAYMOB_HMAC_SECRET not configured in "
                "%s — rejecting callback for security",
                'production' if _is_production() else 'enforced-mode',
            )
            return False, 'hmac_secret_missing'
        logger.warning(
            "⚠️ [PAYMOB HMAC] PAYMOB_HMAC_SECRET unset — accepting callback "
            "(non-production dev mode). DO NOT deploy this way."
        )
        return True, 'dev_skip'

    if not received:
        logger.critical("🚨 [PAYMOB HMAC] No hmac param in callback — rejected")
        return False, 'no_hmac_param'

    fields = _extract_paymob_fields(body_data, request.GET.dict() if request.method == 'GET' else {})
    concatenated = ''.join(_hmac_value(fields[k]) for k in _PAYMOB_HMAC_FIELDS)
    computed = hmac.new(
        secret.encode('utf-8'),
        concatenated.encode('utf-8'),
        hashlib.sha512,
    ).hexdigest()

    if not hmac.compare_digest(computed, received):
        logger.critical(
            "🚨 [PAYMOB HMAC MISMATCH] IP=%s — possible payment-forgery attempt",
            request.META.get('REMOTE_ADDR', '?'),
        )
        return False, 'hmac_mismatch'

    return True, 'ok'


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


# ─────────────────────────────────────────────────────────────────────
# Card tokenization (TOKEN callback) + charge-with-saved-token
# ─────────────────────────────────────────────────────────────────────

# Paymob's documented HMAC field order for the type=TOKEN callback
# (alphabetical, same scheme as the transaction callback but different keys).
_PAYMOB_TOKEN_HMAC_FIELDS = (
    'card_subtype', 'created_at', 'email', 'id',
    'masked_pan', 'merchant_id', 'order_id', 'token',
)


def verify_paymob_token_hmac(request, body_data: dict) -> tuple[bool, str]:
    """🛡️ Verify the HMAC of a ``type=TOKEN`` (card-saved) callback.

    Same fail-closed matrix as ``verify_paymob_hmac`` — unset secret rejects
    in production, accepts with a warning in dev.
    """
    secret = (
        getattr(settings, 'PAYMOB_HMAC_SECRET', '')
        or os.getenv('PAYMOB_HMAC_SECRET', '')
    )
    if not secret:
        if _is_production():
            logger.critical("🚨 [PAYMOB TOKEN HMAC] secret not configured — rejecting")
            return False, 'hmac_secret_missing'
        logger.warning("⚠️ [PAYMOB TOKEN HMAC] secret unset — accepting (dev only)")
        return True, 'dev_skip'

    received = request.GET.get('hmac', '') or body_data.get('hmac', '')
    if not received:
        return False, 'no_hmac_param'

    obj = body_data.get('obj') if isinstance(body_data.get('obj'), dict) else {}
    concatenated = ''.join(_hmac_value(obj.get(k, '')) for k in _PAYMOB_TOKEN_HMAC_FIELDS)
    computed = hmac.new(secret.encode('utf-8'), concatenated.encode('utf-8'),
                        hashlib.sha512).hexdigest()
    if not hmac.compare_digest(computed, received):
        logger.critical("🚨 [PAYMOB TOKEN HMAC MISMATCH] IP=%s",
                        request.META.get('REMOTE_ADDR', '?'))
        return False, 'hmac_mismatch'
    return True, 'ok'


def charge_with_saved_token(
    card_token: str,
    amount_egp,
    *,
    order_ref: str,
    item_name: str = 'Mouss Tec Subscription Renewal',
    metadata: Optional[dict] = None,
    cache_key_prefix: Optional[str] = None,
    customer_email: str = 'customer@mousstec.com',
) -> tuple[bool, str]:
    """💳 Charge a previously-saved Paymob card token (MOTO / recurring).

    Mirrors ``create_iframe_url``'s auth → order → payment-key handshake, then
    hits ``/api/acceptance/payments/pay`` with ``source={identifier: token,
    subtype: 'TOKEN'}`` instead of returning an iframe.

    Paymob still fires the normal transaction-processed webhook afterwards, so
    the existing callback (with its cached ``{cache_key_prefix}_{order_id}``
    metadata) finalizes the subscription exactly like an interactive payment.

    Returns ``(ok, detail)`` — ``detail`` is the Paymob transaction id on
    success or a machine-readable failure token. Never raises.
    """
    import requests as http_requests

    api_key, integration_id, _ = _resolve_credentials()
    if not api_key or not integration_id:
        return False, 'gateway_not_configured'
    try:
        integration_id_int = int(integration_id)
    except (TypeError, ValueError):
        return False, 'bad_integration_id'
    try:
        amount_cents = int(Decimal(str(amount_egp)) * 100)
    except Exception:
        return False, 'invalid_amount'
    if amount_cents <= 0 or not card_token:
        return False, 'invalid_charge_args'

    merchant_order_id = f"{order_ref}_{uuid.uuid4().hex[:10]}"
    try:
        auth_token = _fetch_auth_token()

        order_res = http_requests.post(
            'https://accept.paymob.com/api/ecommerce/orders',
            json={
                'auth_token': auth_token, 'delivery_needed': 'false',
                'amount_cents': amount_cents, 'currency': 'EGP',
                'items': [{'name': item_name, 'amount_cents': amount_cents, 'quantity': '1'}],
                'merchant_order_id': merchant_order_id,
            }, timeout=15,
        )
        if order_res.status_code not in (200, 201):
            logger.error("[PAYMOB TOKEN-CHARGE] order failed: %s — %s",
                         order_res.status_code, order_res.text[:200])
            return False, 'order_failed'
        paymob_order_id = order_res.json().get('id')
        if not paymob_order_id:
            return False, 'order_no_id'

        key_res = http_requests.post(
            'https://accept.paymob.com/api/acceptance/payment_keys',
            json={
                'auth_token': auth_token, 'amount_cents': amount_cents,
                'expiration': 3600, 'order_id': paymob_order_id,
                'billing_data': {
                    'first_name': 'Subscriber', 'last_name': 'MoussTec',
                    'email': customer_email, 'phone_number': '01000000000',
                    'apartment': 'NA', 'floor': 'NA', 'street': 'NA', 'building': 'NA',
                    'shipping_method': 'NA', 'postal_code': 'NA', 'city': 'Cairo',
                    'country': 'EG', 'state': 'Cairo',
                },
                'currency': 'EGP', 'integration_id': integration_id_int,
            }, timeout=15,
        )
        if key_res.status_code not in (200, 201):
            logger.error("[PAYMOB TOKEN-CHARGE] key failed: %s — %s",
                         key_res.status_code, key_res.text[:200])
            return False, 'payment_key_failed'
        payment_token = key_res.json().get('token')
        if not payment_token:
            return False, 'payment_key_no_token'

        # Metadata FIRST — the webhook can arrive before the pay call returns.
        if metadata and cache_key_prefix:
            cache.set(f'{cache_key_prefix}_{paymob_order_id}', metadata, timeout=7200)

        pay_res = http_requests.post(
            'https://accept.paymob.com/api/acceptance/payments/pay',
            json={
                'source': {'identifier': card_token, 'subtype': 'TOKEN'},
                'payment_token': payment_token,
            }, timeout=30,
        )
        if pay_res.status_code not in (200, 201):
            logger.error("[PAYMOB TOKEN-CHARGE] pay failed: %s — %s",
                         pay_res.status_code, pay_res.text[:300])
            return False, f'pay_rejected_{pay_res.status_code}'
        body = pay_res.json()
        txn_id = body.get('id', '')
        success = str(body.get('success', '')).lower() == 'true' or body.get('success') is True
        if not success:
            logger.warning("[PAYMOB TOKEN-CHARGE] declined: txn=%s order=%s",
                           txn_id, paymob_order_id)
            return False, f'declined_txn_{txn_id}'
        logger.info("[PAYMOB TOKEN-CHARGE] ok: txn=%s order=%s amount=%s EGP",
                    txn_id, paymob_order_id, amount_egp)
        return True, str(txn_id)

    except RuntimeError as exc:
        return False, str(exc)
    except http_requests.RequestException as exc:
        logger.exception("[PAYMOB TOKEN-CHARGE] network error: %s", exc)
        return False, 'network_error'


# ─────────────────────────────────────────────────────────────────────
# Refunds — return money to the card through the gateway
# ─────────────────────────────────────────────────────────────────────

def _fetch_auth_token() -> str:
    """Authenticate against Paymob; raises RuntimeError on any failure."""
    import requests as http_requests

    api_key, _, _ = _resolve_credentials()
    if not api_key:
        raise RuntimeError("بوابة الدفع غير مهيّأة (PAYMOB_API_KEY مفقود).")
    try:
        res = http_requests.post(
            'https://accept.paymob.com/api/auth/tokens',
            json={'api_key': api_key}, timeout=15,
        )
    except http_requests.RequestException as exc:
        logger.exception("[PAYMOB] auth network error: %s", exc)
        raise RuntimeError("تعذر الاتصال ببوابة الدفع.")
    token = res.json().get('token') if res.status_code in (200, 201) else None
    if not token:
        logger.error("[PAYMOB] auth failed: %s — %s", res.status_code, res.text[:200])
        raise RuntimeError("فشل المصادقة مع بوابة الدفع.")
    return token


def refund_transaction(transaction_id, amount_egp) -> tuple[bool, str]:
    """💸 Refund a captured Paymob transaction back to the customer's card.

    Parameters
    ----------
    transaction_id : the Paymob transaction id captured at payment time
        (e.g. ``PartOrder.paymob_txn_id``).
    amount_egp : Decimal | str | float — amount to refund in EGP (full or
        partial; Paymob rejects amounts above the original charge).

    Returns ``(ok, detail)``. Never raises — refund is always attempted from
    an admin flow that must be able to fall back to a manual transfer, so the
    caller branches on ``ok`` and logs ``detail`` either way.
    """
    import requests as http_requests

    try:
        amount_cents = int(Decimal(str(amount_egp)) * 100)
    except Exception:
        return False, 'invalid_amount'
    if amount_cents <= 0 or not transaction_id:
        return False, 'invalid_refund_args'

    try:
        auth_token = _fetch_auth_token()
        res = http_requests.post(
            'https://accept.paymob.com/api/acceptance/void_refund/refund',
            json={
                'auth_token': auth_token,
                'transaction_id': str(transaction_id),
                'amount_cents': amount_cents,
            },
            timeout=20,
        )
    except RuntimeError as exc:
        return False, str(exc)
    except http_requests.RequestException as exc:
        logger.exception("[PAYMOB REFUND] network error: %s", exc)
        return False, 'network_error'

    if res.status_code not in (200, 201):
        logger.error("[PAYMOB REFUND] failed: txn=%s status=%s body=%s",
                     transaction_id, res.status_code, res.text[:300])
        return False, f'gateway_rejected_{res.status_code}'

    try:
        body = res.json()
    except ValueError:
        body = {}
    refund_txn_id = body.get('id', '')
    logger.info("[PAYMOB REFUND] ok: original_txn=%s refund_txn=%s amount=%s EGP",
                transaction_id, refund_txn_id, amount_egp)
    return True, str(refund_txn_id)
