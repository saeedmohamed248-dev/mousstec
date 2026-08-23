# 🛡️ خدمة حارس المرتجعات — التنسيق بين الصور والذكاء الاصطناعي والموقع
# =====================================================================
# مسؤولة عن:
#   • حفظ صور الصرف/الإرجاع + بصمة نزاهة (SHA-256)
#   • استدعاء الذكاء الاصطناعي لعمل بصمة الصرف والحكم على المرتجع
#   • مطابقة طلبات الموقع بالحارس الصح (Part Number + رقم الطلب/الهاتف)
#   • إبلاغ موقع FixIt بالحكم (اختياري) على نفس قناة المزامنة
import base64
import binascii
import hashlib
import logging
import os
import threading

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')


# ---------------------------------------------------------------------
# أدوات مساعدة للصور
# ---------------------------------------------------------------------
def _decode_image(image_b64):
    """يفك base64 (بيقبل data URI أو نص خام) → (bytes, sha256) أو (None, None)."""
    if not image_b64:
        return None, None
    raw = image_b64.split(',', 1)[-1].strip()  # يشيل data:image/...;base64,
    try:
        data = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError):
        return None, None
    if not data:
        return None, None
    return data, hashlib.sha256(data).hexdigest()


def _product_context(product):
    return {
        'name': getattr(product, 'name', ''),
        'part_number': getattr(product, 'part_number', ''),
        'brand': getattr(product, 'brand', ''),
        'condition': getattr(product, 'get_condition_display', lambda: getattr(product, 'condition', ''))(),
    }


def _save_photo(guard, image_bytes, sha256, stage, source='internal', uploaded_by=None, external_url=''):
    from ..models import PartReturnPhoto

    photo = PartReturnPhoto(
        guard=guard, stage=stage, source=source, sha256=sha256 or '',
        uploaded_by=uploaded_by, uploaded_at=timezone.now(),
        image_url_external=external_url or '',
    )
    if image_bytes:
        ext = 'jpg'
        photo.image.save(f"{stage}_{sha256[:12] if sha256 else 'img'}.{ext}",
                         ContentFile(image_bytes), save=False)
    photo.save()
    return photo


# ---------------------------------------------------------------------
# إيجاد / إنشاء الحارس
# ---------------------------------------------------------------------
def get_or_create_guard_for_item(invoice_item):
    """حارس لكل سطر فاتورة بيع. بيربط المنتج والعميل والفاتورة تلقائياً."""
    from ..models import PartReturnGuard

    guard = PartReturnGuard.objects.filter(invoice_item=invoice_item).first()
    if guard:
        return guard, False
    invoice = getattr(invoice_item, 'invoice', None)
    guard = PartReturnGuard.objects.create(
        product=invoice_item.product,
        part_number=invoice_item.product.part_number,
        invoice_item=invoice_item,
        original_invoice=invoice,
        customer=getattr(invoice, 'customer', None),
        source='internal',
    )
    return guard, True


def find_guard(part_number, external_ref='', phone=''):
    """مطابقة طلب الموقع بالحارس. الأولوية: رقم الطلب ثم آخر حارس بنفس القطعة/العميل."""
    from ..models import PartReturnGuard

    part_number = (part_number or '').strip()
    external_ref = (external_ref or '').strip()
    qs = PartReturnGuard.objects.all()
    if external_ref:
        g = qs.filter(external_ref=external_ref, part_number=part_number).first() \
            or qs.filter(external_ref=external_ref).first()
        if g:
            return g
    if phone:
        g = qs.filter(part_number=part_number, customer__phone=str(phone).strip()).first()
        if g:
            return g
    if part_number:
        return qs.filter(part_number=part_number).first()
    return None


def _resolve_product(part_number):
    from ..models import Product
    return Product.objects.filter(part_number=(part_number or '').strip()).first()


def create_website_guard(part_number, external_ref='', phone='', name=''):
    """حارس ناشئ من الموقع (لسه مفيش صرف محل)."""
    from ..models import Customer, PartReturnGuard

    product = _resolve_product(part_number)
    if not product:
        return None
    customer = None
    if phone:
        customer = Customer.objects.filter(phone=str(phone).strip()).first()
        if not customer:
            customer = Customer.objects.create(
                phone=str(phone).strip(), name=name or 'عميل الموقع')
    return PartReturnGuard.objects.create(
        product=product, part_number=product.part_number,
        external_ref=(external_ref or '').strip(), customer=customer,
        source='website',
    )


# ---------------------------------------------------------------------
# العمليات الأساسية
# ---------------------------------------------------------------------
def fingerprint_dispatch(guard, image_b64=None, image_bytes=None, sha256=None,
                         stage='dispatch', source='internal', uploaded_by=None):
    """يصوّر القطعة وقت الصرف، يحلّلها بالذكاء الاصطناعي، ويثبّت بصمة الصرف."""
    from .. import ai_services

    if image_bytes is None:
        image_bytes, sha256 = _decode_image(image_b64)
    if not image_bytes:
        return {"ok": False, "error": "no_image"}

    _save_photo(guard, image_bytes, sha256, stage=stage, source=source, uploaded_by=uploaded_by)

    b64 = base64.b64encode(image_bytes).decode('ascii')
    fp = ai_services.fingerprint_part_image(b64, _product_context(guard.product))

    if fp and fp.get('available'):
        guard.dispatch_fingerprint = fp
        guard.dispatch_analyzed_at = timezone.now()
        if guard.status == 'awaiting_dispatch':
            guard.status = 'fingerprinted'
        guard.save(update_fields=['dispatch_fingerprint', 'dispatch_analyzed_at', 'status', 'updated_at'])
        return {"ok": True, "fingerprinted": True, "fingerprint": fp}

    guard.save(update_fields=['updated_at'])
    return {"ok": True, "fingerprinted": False, "reason": (fp or {}).get('reason', 'unknown')}


def verify_return(guard, image_b64=None, image_bytes=None, sha256=None,
                  stage='return', source='internal', uploaded_by=None,
                  push_to_website=True):
    """يصوّر القطعة الراجعة، يقارنها ببصمة الصرف، يصدر الحكم ويحفظه."""
    from .. import ai_services

    if image_bytes is None:
        image_bytes, sha256 = _decode_image(image_b64)
    if not image_bytes:
        return {"ok": False, "error": "no_image"}

    _save_photo(guard, image_bytes, sha256, stage=stage, source=source, uploaded_by=uploaded_by)

    guard.status = 'return_requested'
    b64 = base64.b64encode(image_bytes).decode('ascii')
    verdict = ai_services.verify_part_return(
        guard.dispatch_fingerprint, b64, _product_context(guard.product))

    guard.return_fingerprint = verdict
    guard.verdict = verdict
    guard.verdict_at = timezone.now()

    returnable = verdict.get('returnable')
    if verdict.get('needs_human') or returnable is None:
        guard.status = 'needs_human'
    elif returnable:
        guard.status = 'return_approved'
    else:
        guard.status = 'return_rejected'

    guard.save(update_fields=[
        'return_fingerprint', 'verdict', 'verdict_at', 'status', 'updated_at'])

    if push_to_website:
        push_verdict_to_website(guard)

    return {"ok": True, "verdict": verdict, "status": guard.status,
            "public_token": str(guard.public_token)}


def public_verdict(guard):
    """صيغة الحكم اللي بتتعرض للعميل على الموقع (بدون تفاصيل داخلية)."""
    v = guard.verdict if isinstance(guard.verdict, dict) else {}
    returnable = v.get('returnable')
    if guard.status == 'needs_human' or returnable is None:
        message = "طلب الإرجاع بيتراجع من فريق المحل، هنبلغك بالنتيجة قريب."
    elif returnable:
        message = "مبدئياً القطعة تنفع ترجع ✅"
    else:
        message = "للأسف القطعة مش هتنفع ترجع ❌"
    return {
        'public_token': str(guard.public_token),
        'part_number': guard.part_number,
        'status': guard.status,
        'returnable': returnable,
        'match_score': v.get('match_score'),
        'reasons': v.get('reasons') or [],
        'message': message,
    }


# ---------------------------------------------------------------------
# إبلاغ الموقع بالحكم (نفس قناة FixIt، اختياري)
# ---------------------------------------------------------------------
def _fixit_config():
    url = getattr(settings, 'FIXIT_RETURN_STATUS_URL', None) or os.environ.get('FIXIT_RETURN_STATUS_URL')
    url = url or getattr(settings, 'FIXIT_SYNC_URL', None) or os.environ.get('FIXIT_SYNC_URL')
    secret = getattr(settings, 'FIXIT_SYNC_SECRET', None) or os.environ.get('FIXIT_SYNC_SECRET')
    return (url, secret) if url and secret else (None, None)


def _post_verdict(payload, url, secret):
    try:
        r = requests.post(url, json=payload, headers={'X-Sync-Secret': secret}, timeout=8)
        if r.status_code >= 400:
            logger.warning("FixIt return-status rejected (%s): %s", r.status_code, r.text[:200])
    except requests.RequestException as exc:
        logger.warning("FixIt return-status push failed (non-blocking): %s", exc)


def push_verdict_to_website(guard):
    """يبعت حكم الإرجاع للموقع في خيط منفصل — الفشل ما يوقفش العملية."""
    url, secret = _fixit_config()
    if not url:
        return
    payload = {'action': 'return_status', **public_verdict(guard),
               'external_ref': guard.external_ref}
    threading.Thread(target=_post_verdict, args=(payload, url, secret), daemon=True).start()
