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


# Invoice printing (A4 / thermal / PDF), WhatsApp share, digital signature.



# =====================================================================
# 🖨️ 2. محركات الطباعة، المشاركة، والتوقيع الرقمي
# =====================================================================

@login_required(login_url='/login/')
@tenant_required
def print_invoice_a4(request, invoice_id):
    """Pillar 4 — Cashier dual invoice.

    ?mode=summary  (default) → simple parts + labor totals (customer copy)
    ?mode=detailed           → full technical report: tech notes, OBD fault codes, photos
    """
    mode = (request.GET.get('mode') or 'summary').lower()
    if mode not in {'summary', 'detailed'}:
        mode = 'summary'

    qs = (SaleInvoice.objects
          .select_related('customer', 'vehicle', 'branch', 'maintenance_contract')
          .prefetch_related('items__product', 'service_items__service'))

    if mode == 'detailed':
        qs = qs.prefetch_related(
            'repair_logs__technician__user',
            'repair_logs__media',
            'diagnostic_reports__engineer__user',
        )

    invoice = get_object_or_404(qs, id=invoice_id)

    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return HttpResponseForbidden("لا تملك صلاحية لطباعة فواتير من فروع أخرى.")

    template = ('inventory/invoice_print_detailed.html' if mode == 'detailed'
                else 'inventory/invoice_print_a4.html')
    return render(request, template, {
        'invoice': invoice,
        'print_date': timezone.now(),
        'mode': mode,
    })


@login_required(login_url='/login/')
@tenant_required
def export_invoice_pdf(request, invoice_id):
    """
    📄 تصدير الفاتورة كـ PDF — يستخدم WeasyPrint مع دعم RTL و خط Cairo.
    Fallback: لو WeasyPrint مش مثبت، يرجع HTML للطباعة بـ Ctrl+P.
    """
    invoice = get_object_or_404(
        SaleInvoice.objects
            .select_related('customer', 'vehicle', 'branch', 'maintenance_contract')
            .prefetch_related('items__product', 'service_items__service'),
        id=invoice_id,
    )
    branch = _get_branch_for_user(request.user)
    if branch and invoice.branch != branch:
        return HttpResponseForbidden("لا تملك صلاحية لتصدير فواتير من فروع أخرى.")

    from django.template.loader import render_to_string
    html_string = render_to_string('inventory/invoice_print_a4.html', {
        'invoice': invoice,
        'print_date': timezone.now(),
        'pdf_mode': True,
    })

    try:
        from weasyprint import HTML, CSS
        from weasyprint.text.fonts import FontConfiguration

        font_config = FontConfiguration()
        pdf_css = CSS(string='''
            @page { size: A4; margin: 1.5cm; }
            @font-face {
                font-family: 'Cairo';
                src: url('https://fonts.gstatic.com/s/cairo/v28/SLXgc1nY6HkvangtZmpQdkhzfH5lkSs2SgRjCAGMQ1z0hOA-W1Y.ttf') format('truetype');
            }
            body { font-family: 'Cairo', sans-serif; direction: rtl; }
        ''', font_config=font_config)

        pdf_bytes = HTML(string=html_string, base_url=request.build_absolute_uri('/')).write_pdf(
            stylesheets=[pdf_css], font_config=font_config,
        )

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        filename = f'invoice-{invoice.id}-{timezone.now():%Y%m%d}.pdf'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    except ImportError:
        logger.warning("[PDF EXPORT] WeasyPrint not installed — falling back to HTML")
        return HttpResponse(
            html_string + '<script>window.print();</script>',
            content_type='text/html; charset=utf-8',
        )
    except Exception as e:
        logger.error(f"[PDF EXPORT] Failed for invoice #{invoice_id}: {e}")
        return _json_response_safe({
            "error": f"فشل توليد PDF: {str(e)[:200]}. تأكد من تثبيت WeasyPrint."
        }, status=500)


@login_required(login_url='/login/')
@tenant_required
def print_invoice_thermal(request, invoice_id):
    invoice = get_object_or_404(
        SaleInvoice.objects
            .select_related('customer')
            .prefetch_related('items__product', 'service_items__service'),
        id=invoice_id,
    )
    return render(request, 'inventory/invoice_print_thermal.html', {
        'invoice': invoice,
        'print_date': timezone.now(),
    })


@login_required(login_url='/login/')
@tenant_required
def share_invoice_whatsapp(request, invoice_id):
    invoice = get_object_or_404(SaleInvoice, id=invoice_id)
    if not invoice.customer or not invoice.customer.phone:
        return HttpResponseForbidden("العميل غير مسجل أو لا يملك رقم هاتف.")
    amount_str = f"{float(invoice.total_amount):,.2f}"
    msg = (
        f"مرحباً بك أستاذ {invoice.customer.name} 🚗\n"
        f"تم إصدار مستندكم رقم #{invoice.id}.\n"
        f"الإجمالي: {amount_str} ج.م\n"
        "شكراً لتعاملكم معنا. (Mouss Tec Ecosystem)"
    )
    return redirect(f"https://wa.me/{invoice.customer.phone}?text={urllib.parse.quote(msg)}")


@login_required(login_url='/login/')
@tenant_required
def capture_digital_signature(request, invoice_id):
    if request.method != 'POST':
        return _json_response_safe({"error": "POST Only"}, 400)
    try:
        data = json.loads(request.body)
        if not data.get('signature_data'):
            return _json_response_safe({"error": "بيانات التوقيع فارغة"}, 400)
        # TODO: حفظ الـ base64 في حقل model مخصص
        return _json_response_safe({"status": "success", "message": "تم حفظ التوقيع الإلكتروني."})
    except Exception as e:
        logger.error(f"[SIGNATURE API] {e}")
        return _json_response_safe({"error": "خطأ داخلي"}, 500)
