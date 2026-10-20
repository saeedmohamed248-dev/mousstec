"""
🏭 Production — proof approval link the customer opens (no login).

GET  /printing/proof/<uuid>/          → the proof + approve / request changes
POST /printing/proof/<uuid>/respond/  → {"action": "approve" | "reject", "comment": "..."}
"""
import json

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from erp_core.decorators import tenant_or_404


def proof_share_url(request, proof):
    return request.build_absolute_uri(f'/printing/proof/{proof.share_token}/')


def proof_whatsapp_url(request, proof):
    """رابط واتساب جاهز فيه رابط البروفة لرقم العميل (01xxxxxxxxx ← 201xxxxxxxxx)."""
    from urllib.parse import quote as _urlquote
    customer = proof.job.order.customer
    number = ''.join(ch for ch in (customer.whatsapp or customer.phone or '') if ch.isdigit())
    if number.startswith('0'):
        number = '2' + number
    msg = (f'بروفة «{proof.job.description[:80]}» — طلب #{proof.job.order.order_number}\n'
           f'راجعها ووافق أو اطلب تعديل من هنا:\n{proof_share_url(request, proof)}')
    return f'https://wa.me/{number}?text={_urlquote(msg)}'


@tenant_or_404
def proof_public_view(request, share_token):
    from printing.models import PrintProof
    proof = get_object_or_404(PrintProof.objects.select_related('job__order__customer'), share_token=share_token)
    return render(request, 'printing/proof_public.html', {
        'proof': proof,
        'job': proof.job,
        'order': proof.job.order,
        'is_latest': proof.is_latest,
    })


@csrf_exempt
@require_POST
@tenant_or_404
def proof_respond(request, share_token):
    from printing.models import PrintProof
    try:
        data = json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON غير صالح'}, status=400)
    action = data.get('action')
    if action not in ('approve', 'reject'):
        return JsonResponse({'error': 'إجراء غير صالح'}, status=400)
    comment = (data.get('comment') or '').strip()
    if action == 'reject' and not comment:
        return JsonResponse({'error': 'اكتب التعديل المطلوب عشان المصمم يعرف يشتغل عليه.'}, status=400)

    proof = get_object_or_404(PrintProof, share_token=share_token)
    try:
        proof.respond(action, comment)
    except ValidationError as exc:
        return JsonResponse({'error': '؛ '.join(exc.messages)}, status=400)
    return JsonResponse({
        'success': True,
        'status': proof.status,
        'message': ('شكراً! تم تسجيل موافقتك — الشغل هيدخل الطباعة.' if action == 'approve'
                    else 'وصلنا طلب التعديل — هنبعتلك بروفة جديدة قريب.'),
    })
