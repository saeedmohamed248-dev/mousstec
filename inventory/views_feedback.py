"""Public Customer Feedback — Pillar 4, last mile of the DMS loop.

The cashier shares a UUID-keyed URL after posting an invoice. The customer
opens it on any device (no auth) and submits a 1–5 star rating, a comment,
the 'received in good condition' checkbox, and an optional drawn signature.

Token is unguessable (UUID4) and the form can only be submitted once unless
the cashier explicitly resets it from the admin.
"""
from __future__ import annotations

import base64
import json
import uuid
from io import BytesIO
from urllib.parse import quote

from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import AuditLog, CustomerFeedback


def _client_ip(request) -> str | None:
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR'))


def _audit(request, *, action: str, model_name: str, object_id, object_repr: str,
           event: str, extra: dict | None = None) -> None:
    """Pillar-4 compliance rows in the existing AuditLog. Never breaks the view."""
    try:
        user = getattr(request, 'user', None)
        AuditLog.objects.create(
            user=user if (user and user.is_authenticated) else None,
            action=action,
            model_name=model_name,
            object_id=str(object_id),
            object_repr=object_repr[:255],
            changes_json={'event': event, **(extra or {})},
            ip_address=_client_ip(request),
        )
    except Exception:
        pass


def customer_feedback_page(request, public_token):
    """Public landing page — shows invoice summary + the rating form."""
    try:
        token = uuid.UUID(str(public_token))
    except (ValueError, TypeError):
        raise Http404("invalid_token")

    feedback = get_object_or_404(
        CustomerFeedback.objects
            .select_related('sale_invoice__customer', 'sale_invoice__vehicle',
                            'sale_invoice__branch')
            .prefetch_related('sale_invoice__items__product',
                              'sale_invoice__service_items__service'),
        public_token=token,
    )
    return render(request, 'inventory/customer_feedback.html', {
        'feedback': feedback,
        'invoice': feedback.sale_invoice,
        'already_responded': feedback.responded_at is not None,
    })


@csrf_exempt  # public form — token IS the auth; we still validate it strictly
@require_POST
def customer_feedback_submit(request, public_token):
    try:
        token = uuid.UUID(str(public_token))
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'invalid_token'}, status=400)

    feedback = CustomerFeedback.objects.filter(public_token=token).first()
    if feedback is None:
        return JsonResponse({'ok': False, 'error': 'not_found'}, status=404)
    if feedback.responded_at is not None:
        return JsonResponse({'ok': False, 'error': 'already_responded'}, status=409)

    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'invalid_json'}, status=400)

    # Rating
    try:
        rating = int(payload.get('rating') or 0)
    except (TypeError, ValueError):
        return JsonResponse({'ok': False, 'error': 'invalid_rating'}, status=400)
    if not (1 <= rating <= 5):
        return JsonResponse({'ok': False, 'error': 'rating_out_of_range'}, status=400)

    feedback.rating = rating
    feedback.comment = (payload.get('comment') or '')[:2000]
    feedback.received_in_good_condition = bool(payload.get('received_in_good_condition'))
    feedback.ip_address = _client_ip(request)

    # Optional drawn signature — data URL "data:image/png;base64,..."
    sig_data_url = payload.get('signature_data_url') or ''
    if sig_data_url.startswith('data:image/'):
        try:
            header, b64 = sig_data_url.split(',', 1)
            ext = 'png' if 'png' in header else 'jpg'
            raw = base64.b64decode(b64)
            if len(raw) > 1_500_000:  # cap at ~1.5 MB
                return JsonResponse({'ok': False, 'error': 'signature_too_large'}, status=413)
            feedback.signature_image.save(
                f"sig_{feedback.public_token}.{ext}",
                ContentFile(raw),
                save=False,
            )
        except Exception:
            return JsonResponse({'ok': False, 'error': 'bad_signature_image'}, status=400)

    feedback.responded_at = timezone.now()
    feedback.save()

    _audit(request, action='update', model_name='CustomerFeedback',
           object_id=feedback.pk,
           object_repr=f'Feedback INV#{feedback.sale_invoice_id}',
           event='FEEDBACK_SUBMITTED',
           extra={
               'rating': feedback.rating,
               'received_in_good_condition': feedback.received_in_good_condition,
               'user_agent': request.META.get('HTTP_USER_AGENT', '')[:200],
           })

    return JsonResponse({
        'ok': True,
        'message': 'تم استلام تقييمك. شكراً لك! 🙏',
        'rating': feedback.rating,
    })


# ---------------------------------------------------------------------------
# Backlog #3 — regenerate a lost feedback link + WhatsApp resend
# ---------------------------------------------------------------------------

@login_required(login_url='/login/')
@require_POST
def feedback_resend(request, invoice_id: int):
    """Rotate the public token of an invoice's feedback link and hand back a
    prefilled ``wa.me`` deep link so the cashier can resend it in one tap.

    Locked once the customer has responded (``responded_at`` set) — rotation
    after a response would break the audit chain between the signature and
    the URL it was collected on.
    """
    profile = getattr(request.user, 'employee_profile', None)
    role = getattr(profile, 'role', None)
    if not (request.user.is_superuser or role in {'admin', 'manager', 'cashier'}):
        return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

    feedback = (CustomerFeedback.objects
                .select_related('sale_invoice__customer')
                .filter(sale_invoice_id=invoice_id).first())
    if feedback is None:
        return JsonResponse({'ok': False, 'error': 'no_feedback_record'}, status=404)
    if feedback.responded_at is not None:
        return JsonResponse(
            {'ok': False, 'error': 'already_responded',
             'message': 'العميل قيّم بالفعل — لا يمكن تغيير الرابط بعد الرد.'},
            status=409)

    old_token = feedback.public_token
    feedback.public_token = uuid.uuid4()
    feedback.sent_at = timezone.now()
    feedback.save(update_fields=['public_token', 'sent_at'])

    link = request.build_absolute_uri(feedback.public_url)
    customer = feedback.sale_invoice.customer
    phone = (getattr(customer, 'phone', '') or '').lstrip('+')
    message = f'أهلاً {customer.name}، قيّم خدمة الصيانة من هنا: {link}'
    wa_link = f'https://wa.me/{phone}?text={quote(message)}' if phone else ''

    _audit(request, action='update', model_name='CustomerFeedback',
           object_id=feedback.pk,
           object_repr=f'Feedback INV#{feedback.sale_invoice_id}',
           event='FEEDBACK_LINK_ROTATED',
           extra={'old_token': str(old_token), 'new_token': str(feedback.public_token)})

    return JsonResponse({
        'ok': True,
        'message': 'تم توليد رابط جديد. الرابط القديم لم يعد صالحاً.',
        'feedback_url': link,
        'whatsapp_url': wa_link,
    })
