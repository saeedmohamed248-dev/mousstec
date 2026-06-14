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


# 🔀 Feature submodules — re-exported below so URL conf and external
# imports (`from inventory.views import X`) keep working unchanged.
from .ai_agents import *  # noqa: F401, F403
from .business_ops import *  # noqa: F401, F403
from .dashboards import *  # noqa: F401, F403
from .printing import *  # noqa: F401, F403
from .reports import *  # noqa: F401, F403
from .service import *  # noqa: F401, F403
from .stock_ops import *  # noqa: F401, F403
from .vehicles import *  # noqa: F401, F403
from .webhooks import *  # noqa: F401, F403

# Underscore-prefixed helpers skipped by `import *`.
from .webhooks import _verify_webhook_hmac  # noqa: F401
from .vehicles import _sign_passport_share, _unsign_passport_share  # noqa: F401
from .ai_agents import (  # noqa: F401
    _agent_diagnostic, _agent_b2b_market, _agent_vision_license,
    _get_auto_live_context, _query_auto_business_data,
    _sign_ai_diag_share, _unsign_ai_diag_share,
    _make_share_qr_data_url, _render_ai_diag_context,
)
