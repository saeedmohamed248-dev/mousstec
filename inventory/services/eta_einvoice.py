"""🇪🇬 ETA e-Invoicing — منظومة الفاتورة الإلكترونية المصرية (groundwork).

المسار الكامل: SaleInvoice (posted) → queue_invoice() → ETAInvoiceSubmission
(قائمة انتظار) → submit_pending_invoices (Celery) → ETA documentsubmissions API.

الحالة الحالية: البنية كاملة ومختبرة الشكل لكن **مقفولة** خلف
ETA_EINVOICE_ENABLED (default False) لسببين تشغيليين لا برمجيين:

1. **بيانات المُصدِر (issuer)** — رقم التسجيل الضريبي (RIN) وكود النشاط
   لكل شركة لازم يتسجلوا في بوابة ETA الأول. بنقرأهم من env
   (ETA_ISSUER_RIN وأخواتها) أو من حقول الـ tenant لو موجودة.
2. **التوقيع الإلكتروني (e-seal)** — مستندات الـ B2B لازم توقيع CAdES-BES
   من شهادة ختم إلكتروني على HSM/USB token معتمد من ITIDA. ده جهاز فعلي
   مش كود؛ الخدمة هنا بتوفر hook (`ETA_SIGNATURE_HOOK`) يتوصّل بمزوّد
   التوقيع لما الشهادة تتوفر. للتجارب على preprod ممكن ترفع بدون توقيع
   بـ ETA_ALLOW_UNSIGNED=1.

Endpoints:
    preprod: https://id.preprod.eta.gov.eg  /  https://api.preprod.invoicing.eta.gov.eg
    prod:    https://id.eta.gov.eg          /  https://api.invoicing.eta.gov.eg
"""
from __future__ import annotations

import importlib
import logging
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')

_ENVIRONMENTS = {
    'preprod': {
        'identity': 'https://id.preprod.eta.gov.eg',
        'api': 'https://api.preprod.invoicing.eta.gov.eg',
    },
    'prod': {
        'identity': 'https://id.eta.gov.eg',
        'api': 'https://api.invoicing.eta.gov.eg',
    },
}


def eta_enabled() -> bool:
    return bool(getattr(settings, 'ETA_EINVOICE_ENABLED', False))


def _env_urls() -> dict:
    env_name = getattr(settings, 'ETA_ENVIRONMENT', 'preprod')
    return _ENVIRONMENTS.get(env_name, _ENVIRONMENTS['preprod'])


# ─────────────────────────────────────────────────────────────────────
# Auth — OAuth2 client-credentials against ETA IdSrv
# ─────────────────────────────────────────────────────────────────────

def get_eta_token() -> str:
    """Access token (cached ~50 min; ETA tokens live 60). Raises RuntimeError."""
    import requests

    cached = cache.get('eta_access_token')
    if cached:
        return cached

    client_id = getattr(settings, 'ETA_CLIENT_ID', '')
    client_secret = getattr(settings, 'ETA_CLIENT_SECRET', '')
    if not client_id or not client_secret:
        raise RuntimeError('ETA_CLIENT_ID / ETA_CLIENT_SECRET غير مضبوطين.')

    try:
        res = requests.post(
            f"{_env_urls()['identity']}/connect/token",
            data={'grant_type': 'client_credentials',
                  'client_id': client_id, 'client_secret': client_secret,
                  'scope': 'InvoicingAPI'},
            timeout=20,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f'تعذر الاتصال بمنظومة الضرائب: {exc}')
    if res.status_code != 200:
        logger.error('[ETA] token failed: %s — %s', res.status_code, res.text[:200])
        raise RuntimeError('فشل المصادقة مع منظومة الضرائب.')
    token = res.json().get('access_token', '')
    if not token:
        raise RuntimeError('منظومة الضرائب لم ترسل access_token.')
    cache.set('eta_access_token', token, 50 * 60)
    return token


# ─────────────────────────────────────────────────────────────────────
# Document builder — SaleInvoice → ETA document JSON (v1.0)
# ─────────────────────────────────────────────────────────────────────

def _issuer_block(tenant=None) -> tuple[dict, str]:
    """بيانات المُصدِر — من حقول الـ tenant لو متوفرة، وإلا من env.

    Returns (issuer_dict, activity_code).
    """
    rin = (getattr(tenant, 'eta_rin', '') or getattr(settings, 'ETA_ISSUER_RIN', ''))
    name = (getattr(tenant, 'name', '') or getattr(settings, 'ETA_ISSUER_NAME', ''))
    activity = (getattr(tenant, 'eta_activity_code', '')
                or getattr(settings, 'ETA_ISSUER_ACTIVITY_CODE', ''))
    if not rin or not activity:
        raise RuntimeError(
            'بيانات المُصدِر ناقصة — اضبط ETA_ISSUER_RIN و ETA_ISSUER_ACTIVITY_CODE '
            '(رقم التسجيل الضريبي وكود النشاط من بوابة ETA).')
    return {
        'type': 'B',
        'id': rin,
        'name': name,
        'address': {
            'country': 'EG',
            'governate': getattr(settings, 'ETA_ISSUER_GOVERNATE', 'Cairo'),
            'regionCity': getattr(settings, 'ETA_ISSUER_CITY', 'Cairo'),
            'street': getattr(settings, 'ETA_ISSUER_STREET', 'N/A'),
            'buildingNumber': getattr(settings, 'ETA_ISSUER_BUILDING', '0'),
        },
    }, activity


def _receiver_block(invoice) -> dict:
    """المشتري — B2C أفراد بيتقبلوا بـ type P بدون رقم ضريبي تحت حد الإلزام."""
    customer = invoice.customer
    tax_id = getattr(customer, 'tax_number', '') or ''
    return {
        'type': 'B' if tax_id else 'P',
        'id': tax_id,
        'name': getattr(customer, 'name', 'عميل نقدي'),
        'address': {
            'country': 'EG',
            'governate': 'Cairo', 'regionCity': 'Cairo',
            'street': (getattr(customer, 'address', '') or 'N/A')[:100],
            'buildingNumber': '0',
        },
    }


def build_eta_document(invoice, tenant=None) -> dict:
    """يبني JSON مستند الفاتورة بصيغة ETA v1.0 من SaleInvoice.

    أكواد الأصناف: بنستخدم EGS (كود داخلي مسجّل) بصيغة EG-{RIN}-{internal}.
    لازم الأكواد دي تتسجل وتتعمد في بوابة ETA قبل أول رفع فعلي.
    """
    issuer, activity_code = _issuer_block(tenant)
    rin = issuer['id']
    tax_pct = Decimal(str(invoice.tax_percentage or 0))

    lines = []
    for item in invoice.items.select_related('product').all():
        unit_price = Decimal(str(item.unit_price))
        qty = Decimal(str(item.quantity))
        sales_total = (unit_price * qty).quantize(Decimal('0.00001'))
        tax_amount = (sales_total * tax_pct / 100).quantize(Decimal('0.00001'))
        code = getattr(item.product, 'part_number', '') or f'P{item.product_id}'
        lines.append({
            'description': item.product.name[:250],
            'itemType': 'EGS',
            'itemCode': f'EG-{rin}-{code}'[:50],
            'unitType': 'EA',
            'quantity': float(qty),
            'unitValue': {'currencySold': 'EGP', 'amountEGP': float(unit_price)},
            'salesTotal': float(sales_total),
            'total': float(sales_total + tax_amount),
            'valueDifference': 0, 'totalTaxableFees': 0,
            'netTotal': float(sales_total), 'itemsDiscount': 0,
            'taxableItems': [
                {'taxType': 'T1', 'subType': 'V009',
                 'rate': float(tax_pct), 'amount': float(tax_amount)},
            ] if tax_pct else [],
        })
    for svc in invoice.service_items.select_related('service').all():
        price = Decimal(str(svc.price))
        tax_amount = (price * tax_pct / 100).quantize(Decimal('0.00001'))
        lines.append({
            'description': svc.service.name[:250],
            'itemType': 'EGS',
            'itemCode': f'EG-{rin}-SVC{svc.service_id}'[:50],
            'unitType': 'EA', 'quantity': 1.0,
            'unitValue': {'currencySold': 'EGP', 'amountEGP': float(price)},
            'salesTotal': float(price),
            'total': float(price + tax_amount),
            'valueDifference': 0, 'totalTaxableFees': 0,
            'netTotal': float(price), 'itemsDiscount': 0,
            'taxableItems': [
                {'taxType': 'T1', 'subType': 'V009',
                 'rate': float(tax_pct), 'amount': float(tax_amount)},
            ] if tax_pct else [],
        })

    net = sum(Decimal(str(l['netTotal'])) for l in lines)
    total_tax = sum(Decimal(str(t['amount'])) for l in lines for t in l['taxableItems'])
    discount = Decimal(str(invoice.discount or 0))

    return {
        'issuer': issuer,
        'receiver': _receiver_block(invoice),
        'documentType': 'I',
        'documentTypeVersion': '1.0',
        'dateTimeIssued': invoice.date_created.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'taxpayerActivityCode': activity_code,
        'internalID': str(invoice.pk),
        'invoiceLines': lines,
        'totalDiscountAmount': float(discount),
        'totalSalesAmount': float(net),
        'netAmount': float(net - discount),
        'taxTotals': ([{'taxType': 'T1', 'amount': float(total_tax)}]
                      if total_tax else []),
        'totalAmount': float(net - discount + total_tax),
        'extraDiscountAmount': 0,
        'totalItemsDiscountAmount': 0,
    }


# ─────────────────────────────────────────────────────────────────────
# Signature hook — pluggable e-seal provider
# ─────────────────────────────────────────────────────────────────────

def _sign_document(document: dict) -> Optional[dict]:
    """يوقّع المستند عبر ETA_SIGNATURE_HOOK ('module.path:function').

    الدالة الموصّلة بتاخد الـ document dict وترجعه ومعاه مفتاح 'signatures'
    (قائمة CAdES-BES) — التنفيذ الفعلي بيعتمد على جهاز الختم الإلكتروني
    (HSM/token) بتاع الشركة. None = مفيش موقّع متوصّل.
    """
    hook = getattr(settings, 'ETA_SIGNATURE_HOOK', '')
    if not hook:
        return None
    module_path, _, func_name = hook.partition(':')
    fn = getattr(importlib.import_module(module_path), func_name)
    return fn(document)


# ─────────────────────────────────────────────────────────────────────
# Submission
# ─────────────────────────────────────────────────────────────────────

def submit_document(document: dict) -> tuple[bool, dict]:
    """يرفع مستند واحد لـ documentsubmissions. Returns (accepted, response)."""
    import requests

    signed = _sign_document(document)
    if signed is not None:
        document = signed
    elif not getattr(settings, 'ETA_ALLOW_UNSIGNED', False):
        return False, {'error': 'signature_required',
                       'detail': 'اضبط ETA_SIGNATURE_HOOK بمزوّد الختم الإلكتروني، '
                                 'أو ETA_ALLOW_UNSIGNED=1 للتجارب على preprod فقط.'}

    token = get_eta_token()
    try:
        res = requests.post(
            f"{_env_urls()['api']}/api/v1/documentsubmissions",
            json={'documents': [document]},
            headers={'Authorization': f'Bearer {token}',
                     'Content-Type': 'application/json'},
            timeout=30,
        )
    except requests.RequestException as exc:
        return False, {'error': 'network', 'detail': str(exc)}

    try:
        body = res.json()
    except ValueError:
        body = {'raw': res.text[:500]}

    if res.status_code in (200, 201, 202) and body.get('acceptedDocuments'):
        return True, body
    return False, body


def queue_invoice(invoice) -> 'object':
    """يضيف فاتورة (posted) لقائمة انتظار الرفع — idempotent."""
    from inventory.models import ETAInvoiceSubmission
    submission, _ = ETAInvoiceSubmission.objects.get_or_create(
        sale_invoice=invoice, defaults={'status': 'pending'})
    return submission


def process_submission(submission, tenant=None) -> bool:
    """يبني ويرفع submission واحدة ويحدّث حالتها. Returns success."""
    from inventory.models import ETAInvoiceSubmission

    try:
        document = build_eta_document(submission.sale_invoice, tenant=tenant)
        accepted, response = submit_document(document)
    except RuntimeError as exc:
        submission.status = 'error'
        submission.error_json = {'error': 'config', 'detail': str(exc)}
        submission.attempts += 1
        submission.save(update_fields=['status', 'error_json', 'attempts'])
        return False
    except Exception as exc:  # defensive — قائمة الانتظار متتعطلش بصف واحد
        logger.exception('[ETA] build/submit crashed for INV#%s',
                         submission.sale_invoice_id)
        submission.status = 'error'
        submission.error_json = {'error': 'internal', 'detail': str(exc)[:500]}
        submission.attempts += 1
        submission.save(update_fields=['status', 'error_json', 'attempts'])
        return False

    submission.attempts += 1
    if accepted:
        doc = (response.get('acceptedDocuments') or [{}])[0]
        submission.status = 'submitted'
        submission.eta_uuid = doc.get('uuid', '')
        submission.eta_long_id = doc.get('longId', '')
        submission.submission_uuid = response.get('submissionId', '')
        submission.response_json = response
        submission.submitted_at = timezone.now()
        submission.save(update_fields=['status', 'eta_uuid', 'eta_long_id',
                                       'submission_uuid', 'response_json',
                                       'submitted_at', 'attempts'])
        logger.info('[ETA] INV#%s submitted (uuid=%s)',
                    submission.sale_invoice_id, submission.eta_uuid)
        return True

    submission.status = 'error'
    submission.error_json = response
    submission.save(update_fields=['status', 'error_json', 'attempts'])
    logger.warning('[ETA] INV#%s rejected: %s',
                   submission.sale_invoice_id, str(response)[:300])
    return False
