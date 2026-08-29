"""
🛡️ حارس المرتجعات بالصور — نقاط النهاية
=========================================
جزءان:

1) ويب هوك موقع FixIt (حماية X-Sync-Secret):
   • POST /inventory/webhooks/fixit/return/verify/
     العميل يصوّر القطعة قبل الشرا (stage=pre) وبعد الشرا/وقت الإرجاع
     (stage=post). في الحالة post بنرجّع الحكم فوراً + السبب اللي يتعرض له.

2) واجهات داخلية للمحل (تسجيل دخول مطلوب):
   • POST /inventory/returns/fingerprint/<item_id>/  — تصوير الصرف
   • POST /inventory/returns/verify/<guard_id>/      — فحص المرتجع

كل الردود JSON.
"""
import json
import logging
import os

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ..models import PartReturnGuard, SaleInvoiceItem
from ..services import return_verification as rv

logger = logging.getLogger('mouss_tec_core')


def _secret():
    return getattr(settings, 'FIXIT_SYNC_SECRET', None) or os.environ.get('FIXIT_SYNC_SECRET')


# =====================================================================
# 1) ويب هوك الموقع
# =====================================================================
@csrf_exempt
@require_POST
def fixit_return_verify_webhook(request):
    secret = _secret()
    if not secret or request.headers.get('X-Sync-Secret') != secret:
        return JsonResponse({'error': 'unauthorized'}, status=401)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'invalid json'}, status=400)

    sku = str(body.get('sku') or '').strip()
    stage = str(body.get('stage') or 'post').strip().lower()  # pre | post
    order_ref = str(body.get('order') or body.get('external_ref') or '').strip()
    phone = str(body.get('phone') or '').strip()
    name = str(body.get('name') or '').strip()
    images = body.get('images') or []
    if isinstance(body.get('image'), str):
        images = [body['image']] + list(images)

    if not sku or not images:
        return JsonResponse({'error': 'missing sku/images'}, status=400)

    # مطابقة أو إنشاء حارس
    guard = rv.find_guard(sku, external_ref=order_ref, phone=phone)
    if guard is None:
        guard = rv.create_website_guard(sku, external_ref=order_ref, phone=phone, name=name)
    if guard is None:
        return JsonResponse({'error': 'unknown_sku', 'sku': sku}, status=404)

    if order_ref and not guard.external_ref:
        guard.external_ref = order_ref
        guard.save(update_fields=['external_ref', 'updated_at'])

    # قبل الشرا: نحفظ صور العميل كـ baseline. لو مفيش بصمة صرف محل،
    # نعمل بصمة من صورة العميل عشان يبقى فيه مرجع للمقارنة وقت الإرجاع.
    if stage == 'pre':
        saved = 0
        for img in images:
            img_bytes, sha = rv._decode_image(img)
            if not img_bytes:
                continue
            rv._save_photo(guard, img_bytes, sha, stage='customer_pre', source='website')
            saved += 1
        if not guard.dispatch_fingerprint and images:
            rv.fingerprint_dispatch(
                guard, image_b64=images[0], stage='customer_pre', source='website')
        return JsonResponse({'ok': True, 'stage': 'pre', 'saved': saved,
                             'public_token': str(guard.public_token)})

    # بعد الشرا / الإرجاع: نحكم على أول صورة ونحفظ الباقي كأدلة
    result = rv.verify_return(
        guard, image_b64=images[0], stage='customer_post', source='website',
        push_to_website=False)
    for extra in images[1:]:
        b, s = rv._decode_image(extra)
        if b:
            rv._save_photo(guard, b, s, stage='customer_post', source='website')

    if not result.get('ok'):
        return JsonResponse({'error': result.get('error', 'verify_failed')}, status=400)

    return JsonResponse({'ok': True, 'stage': 'post', **rv.public_verdict(guard)})


# =====================================================================
# 2) واجهات المحل الداخلية
# =====================================================================
def _first_uploaded_image(request):
    """يرجّع (bytes, sha) من request.FILES['image'] أو من body.image base64."""
    f = request.FILES.get('image') or request.FILES.get('photo')
    if f:
        import hashlib
        data = f.read()
        return (data, hashlib.sha256(data).hexdigest()) if data else (None, None)
    # fallback: JSON base64
    try:
        body = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None, None
    return rv._decode_image(body.get('image'))


@login_required
@require_POST
def fingerprint_dispatch_view(request, item_id):
    """المحل بيصوّر القطعة وقت الصرف → بصمة الصرف تتثبّت."""
    item = get_object_or_404(SaleInvoiceItem, pk=item_id)
    guard, _created = rv.get_or_create_guard_for_item(item)

    img_bytes, sha = _first_uploaded_image(request)
    if not img_bytes:
        return JsonResponse({'error': 'no_image'}, status=400)

    result = rv.fingerprint_dispatch(
        guard, image_bytes=img_bytes, sha256=sha, uploaded_by=request.user)
    return JsonResponse({
        'guard_id': guard.pk, 'public_token': str(guard.public_token),
        'status': guard.status, **result})


@login_required
@require_POST
def verify_return_view(request, guard_id):
    """المحل بيصوّر القطعة الراجعة → الحكم بالمقارنة ببصمة الصرف."""
    guard = get_object_or_404(PartReturnGuard, pk=guard_id)

    img_bytes, sha = _first_uploaded_image(request)
    if not img_bytes:
        return JsonResponse({'error': 'no_image'}, status=400)

    result = rv.verify_return(
        guard, image_bytes=img_bytes, sha256=sha, uploaded_by=request.user)
    return JsonResponse({'guard_id': guard.pk, **result})
