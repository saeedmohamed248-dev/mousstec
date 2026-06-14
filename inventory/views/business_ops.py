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


# Business operations: core-charge return, blind-bid creation, scrap cost distribution.



# =====================================================================
# 🏎️ 8. عمليات الأعمال (Business Operations)
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def return_core_charge_api(request, item_id):
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    item = get_object_or_404(SaleInvoiceItem, id=item_id)
    if item.is_core_returned:
        return _json_response_safe({"error": "تم استرداد هذا التالف مسبقاً."}, 400)
    if item.core_charge_applied <= 0:
        return _json_response_safe({"error": "الصنف لا يقع تحت بند التوالف."}, 400)
    item.is_core_returned = True
    item.save()  # Signal في models.py سيتولى الحسابات المالية
    return _json_response_safe({
        "status": "success",
        "refunded_amount": float(item.core_charge_applied * item.quantity),
    })


@login_required(login_url='/login/')
@tenant_required
@require_feature('b2b_marketplace')
def create_blind_bid_api(request):
    """🔒 Blind bidding is a B2B-marketplace feature — same gate as the page."""
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    try:
        data = json.loads(request.body)
        tenant = get_object_or_404(Client, schema_name=connection.schema_name)
        bid = BlindBiddingRequest.objects.create(
            buyer=tenant,
            part_number=data.get('part_number', '').strip(),
            required_qty=int(data.get('required_qty', 1)),
            target_price=data.get('target_price') or None,
            expires_at=timezone.now() + timezone.timedelta(hours=24),
        )
        return _json_response_safe({"status": "success", "bid_ref": str(bid.request_id)})
    except Exception as e:
        logger.error(f"[CREATE BID] {e}")
        return _json_response_safe({"error": str(e)}, 500)


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def distribute_scrap_cost_api(request, job_id):
    """HTTP adapter for scrap cost distribution — delegates to InventoryService."""
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)

    job = get_object_or_404(ScrapDismantlingJob, id=job_id)

    try:
        from inventory.services.inventory_service import InventoryService
        items_count = InventoryService.distribute_scrap_cost(job)
        return _json_response_safe({
            "status": "success",
            "message": "تم توزيع التكلفة بالوزن النسبي وإضافة المكونات للمخزن.",
            "items_processed": items_count,
        })
    except Exception as e:
        return _json_response_safe({"error": str(e)}, 400)
