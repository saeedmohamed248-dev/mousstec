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


# Job card review, RFQ workflow, CRM retention nudges, vehicle search.



# ─────────────────────────────────────────────────────────────────────
# 🧮 Accountant / Sales Review — Unified Job Card billing decision
# ─────────────────────────────────────────────────────────────────────
@login_required(login_url='/login/')
@tenant_required
def job_card_review(request, invoice_id):
    """One-screen review for accountant/sales:
        • AI diagnostic findings (read-only, with photos)
        • Tech repair logs + photos (read-only)
        • Parts (SaleInvoiceItem) and Services (SaleInvoiceServiceItem)
          each with an `is_billable` checkbox and a billing note input.

    POST flow:
        Form fields per row: `item_<id>_billable` (checkbox) +
        `item_<id>_note` (text). Same for `svc_<id>_*`. We update only
        the rows actually present in the POST so partial submissions
        stay safe.
    """
    invoice = get_object_or_404(
        SaleInvoice.objects
            .select_related('customer', 'vehicle', 'branch')
            .prefetch_related(
                'items__product', 'items__salesperson__user',
                'service_items__service', 'service_items__technician__user',
                'repair_logs__technician__user', 'repair_logs__media',
                'diagnostic_reports__engineer__user',
                'diagnostic_reports__photos',
            ),
        id=invoice_id,
    )

    # RBAC — only sales/cashier/accountant/admin/manager/superuser.
    profile = getattr(request.user, 'employee_profile', None)
    allowed_roles = {'sales', 'cashier', 'accountant', 'admin', 'manager'}
    if not (request.user.is_superuser or
            (profile and profile.role in allowed_roles)):
        return HttpResponseForbidden(
            "هذه الشاشة مخصّصة لطاقم المبيعات والمحاسبة فقط."
        )

    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return HttpResponseForbidden("لا تملك صلاحية لمراجعة فواتير فروع أخرى.")

    if request.method == 'POST':
        updated_items = 0
        updated_svcs = 0
        with transaction.atomic():
            for item in invoice.items.all():
                key = f"item_{item.id}_billable"
                note_key = f"item_{item.id}_note"
                # Checkbox absence == unchecked
                new_billable = key in request.POST
                new_note = (request.POST.get(note_key) or '').strip()[:200]
                if (item.is_billable != new_billable
                        or item.billing_note != new_note):
                    item.is_billable = new_billable
                    item.billing_note = new_note
                    item.save(update_fields=['is_billable', 'billing_note'])
                    updated_items += 1

            for svc in invoice.service_items.all():
                key = f"svc_{svc.id}_billable"
                note_key = f"svc_{svc.id}_note"
                new_billable = key in request.POST
                new_note = (request.POST.get(note_key) or '').strip()[:200]
                if (svc.is_billable != new_billable
                        or svc.billing_note != new_note):
                    svc.is_billable = new_billable
                    svc.billing_note = new_note
                    svc.save(update_fields=['is_billable', 'billing_note'])
                    updated_svcs += 1

        logger.info(
            "[Job Card Review] tenant=%s user=%s invoice=%s items=%s svcs=%s",
            getattr(getattr(request, 'tenant', None), 'schema_name', None),
            request.user.username, invoice.id, updated_items, updated_svcs,
        )
        from django.contrib import messages
        messages.success(
            request,
            f"تم حفظ المراجعة — {updated_items} قطعة و {updated_svcs} خدمة."
        )
        return redirect('inventory:job_card_review', invoice_id=invoice.id)

    # Compute customer-billable totals (what actually goes on the invoice)
    from decimal import Decimal
    parts_total = sum(
        (Decimal(str(i.quantity or 0)) * Decimal(str(i.unit_price or 0))
         for i in invoice.items.all() if i.is_billable),
        Decimal('0.00'),
    )
    services_total = sum(
        (Decimal(str(s.price or 0))
         for s in invoice.service_items.all() if s.is_billable),
        Decimal('0.00'),
    )
    excluded_total = sum(
        (Decimal(str(i.quantity or 0)) * Decimal(str(i.unit_price or 0))
         for i in invoice.items.all() if not i.is_billable),
        Decimal('0.00'),
    ) + sum(
        (Decimal(str(s.price or 0))
         for s in invoice.service_items.all() if not s.is_billable),
        Decimal('0.00'),
    )

    return render(request, 'inventory/job_card_review.html', {
        'invoice': invoice,
        'parts_total': parts_total,
        'services_total': services_total,
        'billable_total': parts_total + services_total,
        'excluded_total': excluded_total,
        'reviewer_name': request.user.get_full_name() or request.user.username,
    })


# ─────────────────────────────────────────────────────────────────────
# 🔧 DTC → Suggested Parts (with live per-branch stock)
# ─────────────────────────────────────────────────────────────────────
import json as _json


@login_required(login_url='/login/')
@tenant_required
def job_card_suggested_parts(request, invoice_id):
    """GET → JSON list of AI-suggested parts for the Job Card, with
    live per-branch stock. Lazy-loaded by the Review UI because the
    LLM round-trip is multi-second on a cold cache."""
    invoice = get_object_or_404(
        SaleInvoice.objects.prefetch_related('diagnostic_reports'),
        id=invoice_id,
    )
    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return JsonResponse({"error": "forbidden"}, status=403)

    from smart_diagnostics.services.parts_resolver import (
        resolve_parts_for_job_card,
    )
    try:
        parts = resolve_parts_for_job_card(invoice)
    except Exception as exc:
        logger.exception("[suggested_parts] resolve failed: %s", exc)
        return JsonResponse({
            "parts": [],
            "error": "تعذّر استخراج القطع المقترحة، حاول مرة أخرى.",
        }, status=200)

    return JsonResponse({
        "invoice_id": invoice.id,
        "parts": parts,
        "count": len(parts),
    })


@login_required(login_url='/login/')
@tenant_required
@csrf_exempt   # CSRF is enforced by the X-CSRFToken header check below
def job_card_suggested_part_add(request, invoice_id):
    """POST {product_id, quantity, unit_price?} → create a SaleInvoiceItem
    on the Job Card. Used by the 'Add to Job Card' button next to each
    suggested part. is_billable=True by default; accountant can untick
    later from the review screen.

    RBAC: sales / cashier / accountant / admin / manager / superuser."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)

    # Lightweight CSRF: require a header carrying the cookie value.
    cookie_token = request.META.get('CSRF_COOKIE') or request.COOKIES.get('mt_csrf')
    sent_token = request.headers.get('X-CSRFToken', '')
    if not cookie_token or not sent_token or cookie_token != sent_token:
        return JsonResponse({"error": "csrf_failed"}, status=403)

    invoice = get_object_or_404(SaleInvoice, id=invoice_id)
    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return JsonResponse({"error": "forbidden"}, status=403)

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    try:
        payload = _json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    product_id = payload.get('product_id')
    qty = int(payload.get('quantity') or 1)
    if qty < 1 or qty > 99:
        return JsonResponse({"error": "bad_quantity"}, status=400)
    if not product_id:
        return JsonResponse({"error": "product_required"}, status=400)

    product = Product.objects.filter(id=product_id).first()
    if product is None:
        return JsonResponse({"error": "product_not_found"}, status=404)

    unit_price = payload.get('unit_price')
    try:
        unit_price = Decimal(str(unit_price)) if unit_price is not None \
                     else Decimal(str(product.retail_price or 0))
    except (InvalidOperation, TypeError):
        unit_price = Decimal(str(product.retail_price or 0))

    with transaction.atomic():
        # Idempotency: if the same product already sits on this Job Card,
        # bump the quantity instead of duplicating the line.
        existing = (SaleInvoiceItem.objects
                    .filter(invoice=invoice, product=product)
                    .first())
        if existing:
            existing.quantity = (existing.quantity or 0) + qty
            existing.save(update_fields=['quantity'])
            item = existing
            action = 'incremented'
        else:
            item = SaleInvoiceItem.objects.create(
                invoice=invoice,
                product=product,
                quantity=qty,
                unit_price=unit_price,
                is_billable=True,
                billing_note='أُضيفت تلقائياً من اقتراح الـ AI',
            )
            action = 'created'

    logger.info(
        "[suggested_parts.add] tenant=%s user=%s invoice=%s product=%s "
        "qty=%s action=%s",
        getattr(getattr(request, 'tenant', None), 'schema_name', None),
        request.user.username, invoice.id, product.id, qty, action,
    )
    return JsonResponse({
        "ok": True,
        "action": action,
        "item_id": item.id,
        "product_name": product.name,
        "quantity": item.quantity,
        "unit_price": float(unit_price),
    })


# ─────────────────────────────────────────────────────────────────────
# 📩 RFQ Engine — multi-supplier fan-out for out-of-stock parts
# ─────────────────────────────────────────────────────────────────────
@login_required(login_url='/login/')
@tenant_required
@csrf_exempt   # X-CSRFToken header validated explicitly
def rfq_create(request, invoice_id):
    """POST {part_number, part_name?, product_id?, quantity?, vendor_ids?[]}
    Creates an RFQ for the Job Card, returns the per-vendor wa.me links."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)

    cookie_token = request.COOKIES.get('mt_csrf')
    sent_token = request.headers.get('X-CSRFToken', '')
    if not cookie_token or cookie_token != sent_token:
        return JsonResponse({"error": "csrf_failed"}, status=403)

    invoice = get_object_or_404(SaleInvoice, id=invoice_id)
    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return JsonResponse({"error": "forbidden"}, status=403)

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager', 'stock'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    pn = (payload.get('part_number') or '').strip().upper()
    if not pn:
        return JsonResponse({"error": "part_number_required"}, status=400)
    part_name = (payload.get('part_name') or '').strip()
    qty = int(payload.get('quantity') or 1)
    product_id = payload.get('product_id')

    product = Product.objects.filter(id=product_id).first() if product_id else None

    vendor_ids = payload.get('vendor_ids') or []
    from inventory.models import Vendor
    vendors = list(Vendor.objects.filter(id__in=vendor_ids)) if vendor_ids else []

    rfq_branch = invoice.branch or _get_branch_for_user(request.user)
    if rfq_branch is None:
        return JsonResponse({"error": "branch_required"}, status=400)

    from smart_diagnostics.services.rfq_engine import (
        create_rfq, build_whatsapp_messages,
    )

    try:
        rfq = create_rfq(
            branch=rfq_branch, product=product,
            part_number_requested=pn, part_name_requested=part_name,
            quantity=qty, job_card=invoice,
            requested_by=request.user, vendors=vendors,
            notes='',
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("[rfq_create] failed: %s", exc)
        return JsonResponse({"error": "rfq_create_failed"}, status=500)

    tenant = getattr(request, 'tenant', None)
    workshop_name = (
        getattr(tenant, 'name', None)
        or getattr(tenant, 'schema_name', None)
        or ''
    )
    messages_payload = build_whatsapp_messages(rfq, workshop_name=workshop_name)
    return JsonResponse({
        "ok": True,
        "rfq_id": rfq.id,
        "status": rfq.status,
        "part_number": rfq.part_number_requested,
        "quantity": rfq.quantity,
        "messages": messages_payload,
    }, status=201)


@login_required(login_url='/login/')
@tenant_required
@csrf_exempt
def rfq_log_quote(request, quote_id):
    """POST {price, eta_days?, notes?} — inventory manager pastes vendor reply."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    cookie_token = request.COOKIES.get('mt_csrf')
    if not cookie_token or cookie_token != request.headers.get('X-CSRFToken', ''):
        return JsonResponse({"error": "csrf_failed"}, status=403)

    from inventory.models import RFQQuote
    quote = get_object_or_404(RFQQuote.objects.select_related('rfq'), id=quote_id)

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager', 'stock'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    from smart_diagnostics.services.rfq_engine import log_quote_response
    try:
        log_quote_response(
            quote,
            price=payload.get('price'),
            eta_days=payload.get('eta_days'),
            notes=payload.get('notes') or '',
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse({
        "ok": True,
        "quote_id": quote.id,
        "rfq_status": quote.rfq.status,
        "price": float(quote.quoted_price),
        "eta_days": quote.quoted_eta_days,
    })


@login_required(login_url='/login/')
@tenant_required
@csrf_exempt
def rfq_accept_quote(request, quote_id):
    """POST → promote this quote to a draft PurchaseInvoice."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    cookie_token = request.COOKIES.get('mt_csrf')
    if not cookie_token or cookie_token != request.headers.get('X-CSRFToken', ''):
        return JsonResponse({"error": "csrf_failed"}, status=403)

    from inventory.models import RFQQuote
    quote = get_object_or_404(
        RFQQuote.objects.select_related('rfq', 'rfq__product', 'vendor'),
        id=quote_id,
    )

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'admin', 'manager', 'stock'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    from smart_diagnostics.services.rfq_engine import accept_quote
    try:
        po = accept_quote(quote)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("[rfq_accept_quote] failed: %s", exc)
        return JsonResponse({"error": "accept_failed"}, status=500)

    return JsonResponse({
        "ok": True,
        "purchase_invoice_id": po.id,
        "vendor_name": po.vendor.name,
        "total": float(po.total_amount),
        "status": po.status,
    })


# ─────────────────────────────────────────────────────────────────────
# 🗼 Central RFQ Control Tower — Inventory Manager dashboard
# ─────────────────────────────────────────────────────────────────────
@login_required(login_url='/login/')
@tenant_required
def rfq_management(request):
    """Macro view of every open / quoted / ordered RFQ across the floor.

    Built for the inventory manager who's managing 10-20 parallel
    WhatsApp threads with suppliers. Inline-editable quote inputs +
    side-by-side comparison reduce per-RFQ ops from a 3-click drill-down
    to a single screen.

    RBAC: stock / admin / manager / superuser only.
    """
    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'stock', 'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return HttpResponseForbidden(
            "هذه الشاشة مخصّصة لمديري المخزون فقط."
        )

    # 🐛 [Bug #1 fix] Wrap the entire query/sort/render in a try/except so any
    # data-state issue (a half-migrated tenant, a deleted vendor still linked
    # by a quote row, etc.) renders an empty-state instead of bubbling up to
    # the project-level JSON 500 handler in erp_core/urls.py.
    try:
        from inventory.models import RFQ

        branch = _get_branch_for_user(request.user)
        qs = RFQ.objects.select_related(
            'job_card', 'branch', 'product', 'requested_by',
            'accepted_quote__vendor', 'purchase_invoice',
        ).prefetch_related('quotes__vendor')
        if branch is not None:
            qs = qs.filter(branch=branch)

        open_rfqs = list(qs.filter(status=RFQ.STATUS_OPEN).order_by('-created_at'))
        quoted_rfqs = list(qs.filter(status=RFQ.STATUS_QUOTED).order_by('-created_at'))
        ordered_rfqs = list(qs.filter(status=RFQ.STATUS_ORDERED)
                              .order_by('-created_at')[:25])
        cancelled_count = qs.filter(status=RFQ.STATUS_CANCELLED).count()

        # Sort quotes within each RFQ: responded (cheapest first), then unresponded.
        for rfq in open_rfqs + quoted_rfqs:
            quotes = list(rfq.quotes.all())
            # Defensive: `quoted_price` is Decimal-or-None, `quoted_eta_days`
            # is int-or-None — cast both before sort so a `None` from either
            # never sneaks into a Decimal comparison.
            responded = sorted(
                (q for q in quotes if q.quoted_price is not None),
                key=lambda q: (
                    float(q.quoted_price) if q.quoted_price is not None else 0.0,
                    int(q.quoted_eta_days) if q.quoted_eta_days is not None else 9999,
                ),
            )
            unresponded = [q for q in quotes if q.quoted_price is None]
            rfq.sorted_quotes = responded + unresponded
            rfq.best_quote = responded[0] if responded else None
            # Stamp `is_best` on EVERY quote (including unresponded) so the
            # template never sees a missing attribute.
            best_id = rfq.best_quote.id if rfq.best_quote else None
            for q in quotes:
                q.is_best = (q.id == best_id) if best_id else False

    except Exception as exc:
        logger.exception("[rfq_management] failed: %s", exc)
        # Degrade to an empty board rather than 500
        open_rfqs = []
        quoted_rfqs = []
        ordered_rfqs = []
        cancelled_count = 0

    return render(request, 'inventory/rfq_management.html', {
        'open_rfqs': open_rfqs,
        'quoted_rfqs': quoted_rfqs,
        'ordered_rfqs': ordered_rfqs,
        'cancelled_count': cancelled_count,
        'open_count': len(open_rfqs),
        'quoted_count': len(quoted_rfqs),
        'ordered_count': len(ordered_rfqs),
        'reviewer_name': request.user.get_full_name() or request.user.username,
        'branch': branch,
    })


# ─────────────────────────────────────────────────────────────────────
# 💚 Retention & Campaigns — CRM Dashboard (Week 4 Phase 2)
# ─────────────────────────────────────────────────────────────────────
@login_required(login_url='/login/')
@tenant_required
def retention_crm(request):
    """Macro view of every customer with at least one due/overdue service.

    The list is driven by `ServiceNudge` rows (populated by the daily
    Celery sweep + recomputed live on Job Card posts). The advisor can
    refresh, send a WhatsApp reminder, or dismiss/snooze each nudge.

    RBAC: sales / cashier / accountant / admin / manager / superuser.
    """
    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return HttpResponseForbidden(
            "هذه الشاشة مخصّصة لطاقم المبيعات والمحاسبة فقط."
        )

    try:
        from inventory.models import ServiceNudge

        branch = _get_branch_for_user(request.user)

        base_qs = (ServiceNudge.objects
                   .select_related(
                       'rule', 'vehicle', 'vehicle__customer',
                       'sent_by',
                   )
                   .filter(status__in=[
                       ServiceNudge.STATUS_PENDING,
                       ServiceNudge.STATUS_SENT,
                   ]))

        # Branch-scope by the customer's most-recent job-card branch (cheap proxy)
        # — skipped if the user is unscoped (admin/superuser without a branch pin).
        # Customers with NO job cards still appear; they won't have a branch hint.

        # Group by urgency
        overdue = list(base_qs.filter(urgency=ServiceNudge.URGENCY_OVERDUE)
                              .order_by('due_at'))
        due = list(base_qs.filter(urgency=ServiceNudge.URGENCY_DUE)
                          .order_by('due_at'))
        upcoming = list(base_qs.filter(urgency=ServiceNudge.URGENCY_UPCOMING)
                                .order_by('due_at')[:100])

        # KPIs
        kpi_total_actionable = len(overdue) + len(due)
        kpi_sent_today = base_qs.filter(
            status=ServiceNudge.STATUS_SENT,
            sent_at__date=timezone.localdate(),
        ).count()
        kpi_unique_customers = base_qs.filter(
            urgency__in=[ServiceNudge.URGENCY_OVERDUE, ServiceNudge.URGENCY_DUE],
        ).values('vehicle__customer_id').distinct().count()

    except Exception as exc:
        logger.exception("[retention_crm] failed: %s", exc)
        overdue = []
        due = []
        upcoming = []
        kpi_total_actionable = 0
        kpi_sent_today = 0
        kpi_unique_customers = 0

    return render(request, 'inventory/retention_crm.html', {
        'overdue_nudges': overdue,
        'due_nudges': due,
        'upcoming_nudges': upcoming,
        'kpi_total_actionable': kpi_total_actionable,
        'kpi_sent_today': kpi_sent_today,
        'kpi_unique_customers': kpi_unique_customers,
        'reviewer_name': request.user.get_full_name() or request.user.username,
    })


@login_required(login_url='/login/')
@tenant_required
@csrf_exempt
def retention_send_whatsapp(request, nudge_id):
    """POST → builds a wa.me URL from the rule's template, stamps
    `ServiceNudge.status=sent`, returns the URL for the JS to open.

    The advisor still has to click 'Send' inside WhatsApp Web/Mobile —
    we don't have outbound API yet — but the audit trail of who sent
    which reminder when is captured here."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    cookie_token = request.COOKIES.get('mt_csrf')
    if not cookie_token or cookie_token != request.headers.get('X-CSRFToken', ''):
        return JsonResponse({"error": "csrf_failed"}, status=403)

    from inventory.models import ServiceNudge
    nudge = get_object_or_404(
        ServiceNudge.objects.select_related(
            'rule', 'vehicle', 'vehicle__customer',
        ),
        id=nudge_id,
    )

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    customer = nudge.vehicle.customer
    if not customer.phone:
        return JsonResponse({"error": "no_customer_phone"}, status=400)

    # Build the personalised body from the rule's template
    tenant = getattr(request, 'tenant', None)
    workshop_name = (getattr(tenant, 'name', None)
                     or getattr(tenant, 'schema_name', '')
                     or 'مركزنا')

    vehicle_label = ' '.join(filter(None, [
        nudge.vehicle.brand, nudge.vehicle.model_name,
        f"({nudge.vehicle.car_plate})" if nudge.vehicle.car_plate else '',
    ]))

    template = nudge.rule.whatsapp_template or (
        "مرحباً {customer} 👋\n\n"
        "حسب آخر زيارة، حان موعد *{rule}* لسيارة *{vehicle}*.\n"
        "نسعد بحجز موعد لك في {workshop}."
    )
    body = template.format(
        customer=customer.name or 'عميلنا الكريم',
        vehicle=vehicle_label or 'سيارتك',
        rule=nudge.rule.name,
        workshop=workshop_name,
    )

    # Normalise phone (E.164 digits for wa.me)
    import re as _re, urllib.parse
    digits = _re.sub(r'[\s\-\(\)+]+', '', customer.phone)
    if digits.startswith('00'):
        digits = digits[2:]
    elif digits.startswith('0'):
        digits = '20' + digits[1:]    # EG default
    wa_url = f"https://wa.me/{digits}?text={urllib.parse.quote(body)}"

    # Stamp the audit trail BEFORE returning the URL (idempotent on re-send)
    nudge.status = ServiceNudge.STATUS_SENT
    nudge.sent_at = timezone.now()
    nudge.sent_by = request.user
    nudge.save(update_fields=['status', 'sent_at', 'sent_by'])

    logger.info(
        "[retention.send] tenant=%s user=%s nudge=%s customer=%s rule=%s",
        getattr(tenant, 'schema_name', None), request.user.username,
        nudge.id, customer.id, nudge.rule.name,
    )
    return JsonResponse({
        "ok": True,
        "wa_url": wa_url,
        "message_preview": body,
        "customer_name": customer.name,
        "vehicle_label": vehicle_label,
    })


@login_required(login_url='/login/')
@tenant_required
@csrf_exempt
def retention_dismiss(request, nudge_id):
    """POST → mark a nudge as dismissed (no outreach this cycle)."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    cookie_token = request.COOKIES.get('mt_csrf')
    if not cookie_token or cookie_token != request.headers.get('X-CSRFToken', ''):
        return JsonResponse({"error": "csrf_failed"}, status=403)

    from inventory.models import ServiceNudge
    nudge = get_object_or_404(ServiceNudge, id=nudge_id)

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    nudge.status = ServiceNudge.STATUS_DISMISSED
    nudge.save(update_fields=['status'])
    return JsonResponse({"ok": True, "nudge_id": nudge.id})


@login_required(login_url='/login/')
@tenant_required
def retention_refresh(request):
    """POST → trigger an on-demand bulk recompute of all nudges for this
    tenant. Admin/manager only — the daily Celery sweep handles the
    routine case."""
    if request.method != 'POST':
        return JsonResponse({"error": "method_not_allowed"}, status=405)
    cookie_token = request.COOKIES.get('mt_csrf')
    if not cookie_token or cookie_token != request.headers.get('X-CSRFToken', ''):
        return JsonResponse({"error": "csrf_failed"}, status=403)

    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    try:
        from inventory.predictive_engine import refresh_all_nudges
        result = refresh_all_nudges(limit=2000)
    except Exception as exc:
        logger.exception("[retention.refresh] failed: %s", exc)
        return JsonResponse({"error": "refresh_failed"}, status=500)

    return JsonResponse({"ok": True, **result})


# ─────────────────────────────────────────────────────────────────────
# 🔎 CRM Search — name / phone / VIN → Vehicle Health Passport
# ─────────────────────────────────────────────────────────────────────
@login_required(login_url='/login/')
@tenant_required
def crm_vehicle_search(request):
    """GET ?q=<term> → JSON list of matching vehicles for the CRM
    autocomplete. Searches Customer.name, Customer.phone, and
    Vehicle.chassis_number + Vehicle.car_plate. Capped at 12 results.

    RBAC: sales / cashier / accountant / admin / manager / superuser.
    """
    profile = getattr(request.user, 'employee_profile', None)
    allowed = {'sales', 'cashier', 'accountant', 'admin', 'manager'}
    if not (request.user.is_superuser or (profile and profile.role in allowed)):
        return JsonResponse({"error": "role_forbidden"}, status=403)

    q = (request.GET.get('q') or '').strip()
    if len(q) < 2:
        return JsonResponse({"query": q, "results": []})

    try:
        from django.db.models import Q
        qs = (
            Vehicle.objects.select_related('customer').filter(
                Q(customer__name__icontains=q) |
                Q(customer__phone__icontains=q) |
                Q(chassis_number__icontains=q.upper()) |
                Q(car_plate__icontains=q)
            )
            .order_by('-id')[:12]
        )
        results = [{
            "vin": v.chassis_number,
            "plate": v.car_plate or '',
            "brand": v.brand or '',
            "model": v.model_name or '',
            "customer_name": v.customer.name if v.customer_id else '',
            "customer_phone": v.customer.phone if v.customer_id else '',
            "passport_url": (
                request.build_absolute_uri('/system/crm/vehicle/')
                + v.chassis_number + '/'
            ),
        } for v in qs]
    except Exception as exc:
        logger.exception("[crm_vehicle_search] failed: %s", exc)
        results = []

    return JsonResponse({"query": q, "results": results, "count": len(results)})


# ─────────────────────────────────────────────────────────────────────
# 🪪 Public Vehicle Health Passport — signed share for WhatsApp delivery
# ─────────────────────────────────────────────────────────────────────
_PASSPORT_SHARE_SALT = 'vehicle-passport-share-v1'
_PASSPORT_SHARE_MAX_AGE = 30 * 24 * 60 * 60   # 30 days
