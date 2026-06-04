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

from django.core.files.base import ContentFile
from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import CustomerFeedback


def _client_ip(request) -> str | None:
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR'))


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

    return JsonResponse({
        'ok': True,
        'message': 'تم استلام تقييمك. شكراً لك! 🙏',
        'rating': feedback.rating,
    })
