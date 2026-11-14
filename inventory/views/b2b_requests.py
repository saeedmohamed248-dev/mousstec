"""🛒 طلبات الشراء المباشر في سوق B2B (B2BPurchaseRequest).

المشتري يلاقي الصنف في البحث ← «اطلب من التاجر» بالكمية ← التاجر بيتبلّغ (إيميل + واتساب)
← يوافق أو يعتذر ← بعد الموافقة كل طرف بيشوف تليفون وإيميل التاني ويكملوا البيع بينهم.
"""
import json
import logging

from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.db import connection, transaction
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from clients.models import B2BPurchaseRequest, Client, GlobalB2BMarketplace
from clients.services.entitlements import require_feature

from .utils import _json_response_safe, role_required, tenant_required

logger = logging.getLogger('mouss_tec_core')

B2B_ROLES = ('admin', 'manager')
MAX_PENDING_PER_BUYER = 30


def _me():
    return Client.objects.get(schema_name=connection.schema_name)


def _requests_link(client):
    from clients.models import Domain
    dom = Domain.objects.filter(tenant=client, is_primary=True).first() or Domain.objects.filter(tenant=client).first()
    return f"https://{dom.domain}/system/b2b-market/requests/" if dom else ''


def _notify(client, subject, body):
    """إيميل + واتساب لصاحب الشركة — مايوقفش الطلب لو الإرسال فشل."""
    link = _requests_link(client)
    text = f"{body}\n{link}".strip()
    try:
        if client.email:
            send_mail(subject, text, None, [client.email], fail_silently=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[B2B REQUEST] email to %s failed: %s", client.schema_name, exc)
    try:
        from clients.services.whatsapp_notify import send_platform_whatsapp
        if client.phone:
            send_platform_whatsapp(client.phone, text=f"Mouss Tec — {subject}\n{text}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[B2B REQUEST] whatsapp to %s failed: %s", client.schema_name, exc)


@login_required(login_url='/login/')
@tenant_required
@require_feature('b2b_marketplace')
@role_required(*B2B_ROLES)
@require_POST
def b2b_request_create(request):
    try:
        data = json.loads(request.body or b'{}')
        listing_id = int(data.get('listing_id') or 0)
        quantity = int(data.get('quantity') or 0)
    except (ValueError, TypeError, AttributeError):
        return _json_response_safe({'error': 'بيانات الطلب مش مظبوطة.'}, 400)
    note = str(data.get('note') or '').strip()[:500]
    me = _me()
    listing = (GlobalB2BMarketplace.objects.select_related('tenant')
               .filter(pk=listing_id, available_qty__gt=0, tenant__is_active=True,
                       tenant__is_marketplace_active=True, tenant__is_fraud_flagged=False).first())
    if listing is None:
        return _json_response_safe({'error': 'الصنف ده مبقاش متاح في السوق.'}, 404)
    if listing.tenant_id == me.pk:
        return _json_response_safe({'error': 'ده صنفك انت — مينفعش تطلبه من نفسك.'}, 400)
    if quantity < 1 or quantity > listing.available_qty:
        return _json_response_safe(
            {'error': f'الكمية لازم من 1 لـ {listing.available_qty} (المتاح عند التاجر).'}, 400)

    with transaction.atomic():
        pending = B2BPurchaseRequest.objects.select_for_update().filter(buyer=me, status='pending')
        same = pending.filter(listing=listing).first()
        if same is not None:   # طلب مفتوح لنفس الصنف ⇒ نحدّثه بدل طلب مكرر
            same.quantity, same.note = quantity, note or same.note
            same.unit_price = listing.wholesale_price
            same.save(update_fields=['quantity', 'note', 'unit_price'])
            return _json_response_safe({'status': 'updated', 'request_id': same.pk,
                                        'message': 'اتحدّث طلبك المفتوح عند التاجر ده.'})
        if pending.count() >= MAX_PENDING_PER_BUYER:
            return _json_response_safe(
                {'error': 'عندك طلبات كتير مستنية رد — استنى الردود أو الغي شوية منهم.'}, 429)
        req = B2BPurchaseRequest.objects.create(
            buyer=me, seller=listing.tenant, listing=listing, part_number=listing.part_number,
            product_name=listing.product_name or '', unit_price=listing.wholesale_price,
            quantity=quantity, note=note)
        seller = listing.tenant
        transaction.on_commit(lambda: _notify(
            seller, 'طلب شراء جديد من سوق B2B',
            f"{me.name} عايز {req.quantity} × {req.product_name or req.part_number} ({req.part_number}) "
            f"بسعر {req.unit_price} للقطعة. رد على الطلب من هنا:"))
    return _json_response_safe({'status': 'success', 'request_id': req.pk,
                                'message': 'اتبعت الطلب للتاجر — هيوصلك رده هنا وعلى الواتساب.'})


@login_required(login_url='/login/')
@tenant_required
@require_feature('b2b_marketplace')
@role_required(*B2B_ROLES)
def b2b_requests_page(request):
    from clients.services.whatsapp_notify import normalize_phone
    me = _me()
    incoming = list(B2BPurchaseRequest.objects.filter(seller=me).select_related('buyer')[:100])
    outgoing = list(B2BPurchaseRequest.objects.filter(buyer=me).select_related('seller')[:100])
    for rows, side in ((incoming, 'buyer'), (outgoing, 'seller')):
        for r in rows:
            r.other = getattr(r, side)
            # تليفون الطرف التاني بيظهر بعد الموافقة بس (القالب)
            r.other_wa = (normalize_phone(r.other.phone or '', 'EG') or '') if r.status == 'accepted' else ''
    return render(request, 'inventory/b2b_requests.html', {
        'incoming': incoming, 'outgoing': outgoing,
        'incoming_pending': sum(1 for r in incoming if r.status == 'pending'),
        'tab': 'out' if request.GET.get('tab') == 'out' else 'in',
    })


@login_required(login_url='/login/')
@tenant_required
@require_feature('b2b_marketplace')
@role_required(*B2B_ROLES)
@require_POST
def b2b_request_respond(request, pk):
    action = request.POST.get('action')
    reply = (request.POST.get('reply') or '').strip()[:500]
    if action not in ('accept', 'reject', 'cancel'):
        return _json_response_safe({'error': 'إجراء مش معروف.'}, 400)
    me = _me()
    with transaction.atomic():
        req = B2BPurchaseRequest.objects.select_for_update().select_related('buyer', 'seller').filter(pk=pk).first()
        owner = req and (req.buyer_id if action == 'cancel' else req.seller_id)
        if req is None or owner != me.pk:
            return _json_response_safe({'error': 'الطلب مش موجود.'}, 404)
        if req.status != 'pending':
            return _json_response_safe({'error': 'الطلب ده اترد عليه قبل كده.'}, 400)
        req.status = {'accept': 'accepted', 'reject': 'rejected', 'cancel': 'cancelled'}[action]
        req.seller_reply = reply if action != 'cancel' else req.seller_reply
        req.responded_at = timezone.now()
        req.save(update_fields=['status', 'seller_reply', 'responded_at'])
        if action == 'accept':
            buyer, seller = req.buyer, req.seller
            transaction.on_commit(lambda: _notify(
                buyer, 'التاجر وافق على طلب الشراء',
                f"{seller.name} وافق على {req.quantity} × {req.product_name or req.part_number}. "
                f"تواصل معاه: {seller.phone}" + (f" — {reply}" if reply else '')))
        elif action == 'reject':
            buyer, seller = req.buyer, req.seller
            transaction.on_commit(lambda: _notify(
                buyer, 'التاجر اعتذر عن طلب الشراء',
                f"{seller.name} اعتذر عن {req.part_number}" + (f": {reply}" if reply else '.')))
    return _json_response_safe({'status': 'success', 'new_status': req.status})
