"""
🚀 Mouss Tec Enterprise — Views & MAS Orchestrator Layer
=========================================================
المعمارية: كل وكيل (Agent) عبارة عن دالة نقية (Pure Function) تقبل بيانات وترجع بيانات.
الـ Views هي فقط HTTP adapters تستدعي الوكلاء — لا منطق داخل الـ view نفسه.
الـ Orchestrator يُدار بـ async-safe thread pool مع DB connection management صحيح.
"""

from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Sum, F, Q
from django.utils import timezone
from django.db import connection, transaction, close_old_connections
from django.core.cache import cache
from django.conf import settings
from django_tenants.utils import schema_context
from decimal import Decimal, InvalidOperation

import json
import urllib.parse
import base64
import uuid
import re
import logging
import concurrent.futures

from ..ai_services import predict_parts_from_dtc, scan_invoice_image_ai, call_gemini_layer
from clients.models import GlobalB2BMarketplace, Client, BlindBiddingRequest
from clients.services.entitlements import require_feature

try:
    import qrcode
    from io import BytesIO
except ImportError:
    qrcode = None

from ..models import (
    Product, Inventory, SaleInvoice, SaleInvoiceItem, Branch,
    Customer, Vehicle, ScrapDismantlingJob, ScrapDismantlingYield,
    FinancialTransaction, EmployeeShift, MaintenanceContract, Treasury,
    ChartOfAccount, AccountingEntry, InventoryMovement, StockAlert,
    ImportSession, AuditLog, PurchaseInvoice, Vendor,
)


# Shared utilities live in their own submodule and are re-exported here
# so existing view definitions (defined below) and external imports still see them.
from .utils import *  # noqa: F401, F403
from .utils import _json_response_safe, _get_branch_for_user, _require_tenant  # noqa: F401


# External webhooks: Shopify, payment, market-price, tax/forex, legacy sync, multiplexer.



# =====================================================================
# 🌐 4. Webhooks الخارجية والمزامنة الإقليمية
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def api_documentation_view(request):
    return HttpResponse(
        "<h1>Mouss Tec B2B API Gateway v1.0</h1>"
        "<p>OpenAPI Documentation — Secure Mode.</p>"
    )


@login_required(login_url='/login/')
@tenant_required
def graphql_gateway_view(request):
    return _json_response_safe({"data": {"message": "GraphQL Federation Gateway Active."}})


def _verify_webhook_hmac(request, secret_setting_name, header_name='HTTP_X_SHOPIFY_HMAC_SHA256'):
    """
    🛡️ التحقق من HMAC للـ webhooks الخارجية.
    يقارن التوقيع المرسل مع التوقيع المحسوب من body + secret.
    """
    import hmac as _hmac
    import hashlib as _hashlib
    secret = getattr(settings, secret_setting_name, None)
    if not secret:
        logger.warning(f"⚠️ [WEBHOOK] {secret_setting_name} not configured — rejecting webhook")
        return False
    received_hmac = request.META.get(header_name, '')
    if not received_hmac:
        return False
    computed = _hmac.new(
        secret.encode('utf-8'),
        request.body,
        _hashlib.sha256,
    ).hexdigest()
    return _hmac.compare_digest(computed, received_hmac)


@csrf_exempt
def shopify_webhook_receiver(request):
    if request.method != 'POST':
        return HttpResponseForbidden()
    # 🛡️ HMAC-SHA256 verification بدلاً من User-Agent check
    if not _verify_webhook_hmac(request, 'SHOPIFY_WEBHOOK_SECRET', 'HTTP_X_SHOPIFY_HMAC_SHA256'):
        logger.warning("🛑 [SHOPIFY] HMAC verification failed — possible spoofing attempt.")
        return HttpResponseForbidden("Invalid HMAC signature")
    try:
        logger.info("⚙️ [SHOPIFY] Sync initiated (HMAC verified).")
        return _json_response_safe({"status": "success", "message": "Order accepted for sync."})
    except Exception as e:
        return _json_response_safe({"status": "error", "message": str(e)}, 500)


@csrf_exempt
def payment_gateway_callback(request):
    """🛡️ Stub — No logic, safe. When activated must add HMAC verification."""
    if request.method != 'POST':
        return HttpResponseForbidden()
    logger.info("⚙️ [PAYMENT GW] Callback received (stub).")
    return _json_response_safe({"status": "success", "channel": "fintech_sync_active"})


@csrf_exempt
def market_price_sync_webhook(request):
    """🛡️ Stub — When activated must add HMAC verification."""
    if request.method != 'POST':
        return HttpResponseForbidden()
    return _json_response_safe({"status": "acknowledged"})


@csrf_exempt
def regional_tax_forex_sync_webhook(request):
    """🛡️ Stub — When activated must add HMAC verification."""
    if request.method != 'POST':
        return HttpResponseForbidden()
    return _json_response_safe({"status": "success", "message": "أسعار الصرف تم تحديثها."})


# =====================================================================
# 🔌 10. مسارات الـ API Gateway الأخرى
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def legacy_system_sync_api(request):
    return _json_response_safe({"status": "success", "channel": "decentralized_legacy_sync_active"})


@csrf_exempt
def universal_webhook_multiplexer(request):
    """🛡️ Webhook multiplexer with HMAC verification."""
    if request.method != 'POST':
        return HttpResponseForbidden()
    if not _verify_webhook_hmac(request, 'WEBHOOK_HMAC_SECRET', 'HTTP_X_WEBHOOK_SIGNATURE'):
        logger.warning("[WEBHOOK] HMAC verification failed — rejected.")
        return HttpResponseForbidden("Invalid signature")
    return _json_response_safe({"status": "success", "channel": "universal_webhook_active"})
