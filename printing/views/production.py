"""
🏭 Production — proof approval link the customer opens (no login).

GET  /printing/proof/<uuid>/          → the proof + approve / request changes
POST /printing/proof/<uuid>/respond/  → {"action": "approve" | "reject", "comment": "..."}
"""
import json

from django.contrib.auth.decorators import login_required as _login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.utils.translation import gettext as _
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
        return JsonResponse({'error': _('JSON غير صالح')}, status=400)
    action = data.get('action')
    if action not in ('approve', 'reject'):
        return JsonResponse({'error': _('إجراء غير صالح')}, status=400)
    comment = (data.get('comment') or '').strip()
    if action == 'reject' and not comment:
        return JsonResponse({'error': _('اكتب التعديل المطلوب عشان المصمم يعرف يشتغل عليه.')}, status=400)

    proof = get_object_or_404(PrintProof, share_token=share_token)
    try:
        proof.respond(action, comment)
    except ValidationError as exc:
        return JsonResponse({'error': '؛ '.join(exc.messages)}, status=400)
    return JsonResponse({
        'success': True,
        'status': proof.status,
        'message': (_('شكراً! تم تسجيل موافقتك — الشغل هيدخل الطباعة.') if action == 'approve'
                    else _('وصلنا طلب التعديل — هنبعتلك بروفة جديدة قريب.')),
    })


# =====================================================================
# 🏭 لوحة الإنتاج (Kanban) — كل مهمة شغالة في عمود مرحلتها، وزرار «المرحلة الجاية»
# =====================================================================
# قبل كده المراحل (تصميم ← بروفة ← طباعة ← تشطيب) كانت بتتغيّر من الأدمن بس.

BOARD_DONE_DAYS = 3  # المهام اللي خلصت بتفضل ظاهرة في عمود «خلصت» الأيام دي بس


def _board_jobs():
    from datetime import timedelta
    from django.db.models import F, Q
    from django.utils import timezone
    from printing.models import PrintJob
    recent = timezone.now() - timedelta(days=BOARD_DONE_DAYS)
    return (PrintJob.objects
            .exclude(order__status__in=('draft', 'cancelled', 'delivered'))
            .filter(Q(is_complete=False) | Q(completed_at__gte=recent))
            .select_related('order', 'order__customer', 'machine', 'designer')
            .order_by(F('order__date_due').asc(nulls_last=True), 'stage_changed_at', 'id'))


@_login_required
@tenant_or_404
def production_board(request):
    from printing.models import PrintJob
    if not request.user.has_perm('printing.view_printjob'):
        return HttpResponseForbidden(_("لا تملك صلاحية مشاهدة مهام الطباعة."))
    machine_id = request.GET.get('machine') or ''
    jobs = _board_jobs()
    if machine_id.isdigit():
        jobs = jobs.filter(machine_id=int(machine_id))
    columns = {code: [] for code in PrintJob.STAGE_FLOW}
    for job in jobs:
        nxt = job.next_stage
        if nxt == 'proof' and job.proof_waived:
            nxt = 'printing'
        # ⏳ الطباعة مقفولة لحد ما العميل يوافق على البروفة (نفس شرط PrintJob.advance_stage)
        job.board_blocked = bool(nxt) and nxt in PrintJob.PRODUCTION_STAGES and not job.can_go_to_production
        columns.setdefault(job.stage, []).append(job)
    from printing.models import MachineProfile
    return render(request, 'printing/production_board.html', {
        'title': _("لوحة الإنتاج"),
        'columns': [(code, label, columns.get(code, [])) for code, label in PrintJob.STAGE_CHOICES],
        'machines': MachineProfile.objects.all(),
        'machine_id': machine_id,
        'can_move': request.user.has_perm('printing.change_printjob'),
    })


@_login_required
@tenant_or_404
@require_POST
def production_job_advance(request, job_id):
    from django.contrib import messages as _messages
    from printing.models import PrintJob
    if not request.user.has_perm('printing.change_printjob'):
        return HttpResponseForbidden(_("لا تملك صلاحية تعديل مهام الطباعة."))
    job = get_object_or_404(PrintJob, pk=job_id)
    try:
        nxt = job.advance_stage()
    except ValidationError as exc:
        _messages.error(request, '؛ '.join(exc.messages))
    else:
        _messages.success(request, _("«%(job)s» اتنقلت لـ «%(stage)s».")
                          % {'job': job.description[:60], 'stage': dict(PrintJob.STAGE_CHOICES)[nxt]})
    url = reverse('printing:production_board')
    machine = request.POST.get('machine', '')
    return redirect(f"{url}?machine={machine}" if machine.isdigit() else url)
