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


# Shared utilities & decorators used across every view module.
# =====================================================================
# 🔌 0. طبقة الأدوات المشتركة (Shared Utilities)
# =====================================================================

def _json_response_safe(data, status=200):
    """مُغلّف آمن يمنع تسريب stack traces في الـ Production"""
    # 🛡️ في بيئة الإنتاج: إخفاء تفاصيل الأخطاء الداخلية لمنع Information Disclosure
    if status >= 500 and not settings.DEBUG:
        if 'error' in data:
            data = {"error": "حدث خطأ داخلي. يرجى المحاولة لاحقاً أو التواصل مع الدعم الفني."}
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})


def _get_branch_for_user(user):
    """استخراج فرع المستخدم بشكل آمن مع fallback"""
    if user.is_superuser:
        return None  # superuser يرى كل الفروع
    try:
        return user.employee_profile.branch
    except Exception:
        return None


def _require_tenant(request):
    """يتحقق أن الطلب قادم من tenant وليس من الـ public schema"""
    tenant = getattr(request, 'tenant', None)
    if not tenant or tenant.schema_name == 'public':
        return False
    return True


from functools import wraps

def tenant_required(view_func):
    """
    🛡️ درع العزل السحابي — ديكوريتور يمنع الوصول من public schema.
    يُطبّق على كل view يخدم بيانات tenant-specific.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not _require_tenant(request):
            return HttpResponseForbidden(
                '{"error": "🛑 هذه الخدمة مخصصة للفروع فقط. الوصول من public schema محظور."}',
                content_type='application/json'
            )
        return view_func(request, *args, **kwargs)
    return _wrapped


def role_required(*allowed_roles):
    """
    🛡️ RBAC Decorator — يمنع الوصول لغير الأدوار المسموح لها.
    Usage: @role_required('admin', 'manager')
    Superusers always pass.

    🐛 [BUG FIX — Issue #1 dashboard quick-actions]:
    Was returning JSON 403 for every denial. Browsers rendered that as raw
    `{"error":"..."}` which looked exactly like a "you got logged out" screen.
    Now: for HTML navigations we return a proper rendered 403 page; only
    AJAX / API clients still get the JSON shape they expect.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)
            try:
                role = request.user.employee_profile.role
            except Exception:
                role = None
            if role not in allowed_roles:
                wants_json = (
                    request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                    or 'application/json' in request.headers.get('Accept', '')
                    or request.path.startswith('/api/')
                )
                if wants_json:
                    return _json_response_safe(
                        {"error": "🔒 ليس لديك صلاحية للوصول لهذه الخدمة. تواصل مع المدير."},
                        status=403,
                    )
                # Browser nav → render an HTML page that keeps the user signed in
                # and offers a way back to the dashboard (NOT a login screen).
                from django.shortcuts import render
                return render(
                    request, 'inventory/forbidden.html',
                    {
                        'allowed_roles': allowed_roles,
                        'current_role': role or '—',
                    },
                    status=403,
                )
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator


