"""
🔧 Repair Atlas — Views (HTML + JSON API)

Endpoints:
    GET  /repair-atlas/                  → HTML page (3 modes, brand selector)
    POST /repair-atlas/api/ask/          → text question → answer
    POST /repair-atlas/api/photo/        → image (base64) + caption → Vision answer
    POST /repair-atlas/api/reset/        → clear session history
    GET  /repair-atlas/superadmin/review/ → SuperAdmin review queue (HTML)
    POST /repair-atlas/superadmin/review/<answer_id>/  → approve/correct/reject
"""
from __future__ import annotations

import base64
import json
import logging

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    RepairSession, RepairQuery, RepairAnswer, TechPhoto, VerifiedKnowledge,
    RepairMode, AnswerSource, VerificationStatus,
)
from .services.repair_coach import coach_reply

logger = logging.getLogger('mouss_tec_core')

_SESSION_KEY = 'repair_atlas_session_id_v1'
_HISTORY_KEY = 'repair_atlas_history_v1'
_MAX_HISTORY = 12
_MAX_IMAGE_BYTES = 6 * 1024 * 1024  # 6 MB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _brand_catalog():
    """Reuse the diagnostics brand catalog so UX is consistent."""
    from erp_core.ai.diagnostic_catalog import DIAGNOSTIC_BRANDS
    return [
        {'key': key, 'label': b['label'], 'emoji': b.get('emoji', '🚗'),
         'color': b.get('color', '#0099ff')}
        for key, b in DIAGNOSTIC_BRANDS.items()
    ]


def _get_or_create_session(request, vehicle: dict) -> RepairSession:
    sid = request.session.get(_SESSION_KEY)
    if sid:
        sess = RepairSession.objects.filter(id=sid, user=request.user).first()
        if sess:
            # update vehicle if user changed brand/model in UI
            dirty = False
            for field in ('brand', 'model_name', 'engine_code', 'vin'):
                val = vehicle.get(field, '')
                if val and getattr(sess, field) != val:
                    setattr(sess, field, val)
                    dirty = True
            year = vehicle.get('year')
            if year and sess.year != year:
                sess.year = year
                dirty = True
            if dirty:
                sess.save()
            return sess

    sess = RepairSession.objects.create(
        user=request.user,
        brand=vehicle.get('brand', ''),
        model_name=vehicle.get('model_name', ''),
        year=vehicle.get('year') or None,
        engine_code=vehicle.get('engine_code', ''),
        vin=vehicle.get('vin', ''),
    )
    request.session[_SESSION_KEY] = sess.id
    request.session[_HISTORY_KEY] = []
    request.session.modified = True
    return sess


def _vehicle_from_payload(payload: dict) -> dict:
    year = payload.get('year')
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    return {
        'brand': (payload.get('brand') or '').strip(),
        'model_name': (payload.get('model') or '').strip(),
        'year': year,
        'engine_code': (payload.get('engine') or '').strip(),
        'vin': (payload.get('vin') or '').strip().upper(),
    }


def _push_history(request, role: str, text: str) -> None:
    hist = request.session.get(_HISTORY_KEY, [])
    hist.append({'role': role, 'text': text})
    request.session[_HISTORY_KEY] = hist[-_MAX_HISTORY:]
    request.session.modified = True


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------
@login_required
def repair_atlas_page(request):
    return render(request, 'repair_atlas/repair_atlas.html', {
        'brands': _brand_catalog(),
        'modes': [
            {'key': RepairMode.DISASSEMBLY, 'label': 'تفكيك', 'icon': '🔧'},
            {'key': RepairMode.INSTALL,     'label': 'تركيب', 'icon': '🔩'},
            {'key': RepairMode.WIRING,      'label': 'ضفائر', 'icon': '⚡'},
            {'key': RepairMode.LOCATE,      'label': 'مكان قطعة', 'icon': '📍'},
        ],
    })


# ---------------------------------------------------------------------------
# Chat API — text only
# ---------------------------------------------------------------------------
@login_required
@require_POST
def repair_atlas_ask(request):
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'invalid_json'}, status=400)

    question = (body.get('question') or '').strip()
    mode = (body.get('mode') or 'disassembly').strip()
    if mode not in dict(RepairMode.choices):
        mode = 'disassembly'

    if not question:
        return JsonResponse({'success': False, 'answer': 'اكتب سؤال.'})
    if len(question) > 2000:
        return JsonResponse({
            'success': False, 'answer': 'السؤال طويل أوي — اختصره.',
        }, status=400)

    vehicle = _vehicle_from_payload(body)
    sess = _get_or_create_session(request, vehicle)
    history = request.session.get(_HISTORY_KEY, [])

    result = coach_reply(question, mode, vehicle, history)

    if not result.get('success'):
        return JsonResponse(result, status=200)

    # Persist query + answer
    q = RepairQuery.objects.create(
        session=sess, mode=mode,
        part_or_system=result.get('suggested_part', ''),
        question_text=question,
    )
    ans = RepairAnswer.objects.create(
        query=q,
        body_markdown=result['answer'],
        source=AnswerSource.VERIFIED if result['source'] == 'verified'
               else AnswerSource.LLM,
        verified_kb_id=result.get('used_verified_kb_id'),
        review_status=VerificationStatus.APPROVED
                       if result['source'] == 'verified'
                       else VerificationStatus.PENDING,
    )
    q.last_answer = ans
    q.save(update_fields=['last_answer'])

    if not sess.title:
        sess.title = question[:120]
        sess.save(update_fields=['title'])

    _push_history(request, 'user', question)
    _push_history(request, 'assistant', result['answer'])

    return JsonResponse({
        'success': True,
        'answer': result['answer'],
        'source': result['source'],
        'mode': mode,
        'query_id': q.id,
        'answer_id': ans.id,
        'session_id': sess.id,
    })


# ---------------------------------------------------------------------------
# Photo API — Vision feedback loop
# ---------------------------------------------------------------------------
@login_required
@require_POST
def repair_atlas_photo(request):
    """Receives multipart form: image (file), caption, mode, vehicle fields."""
    f = request.FILES.get('image')
    if not f:
        return JsonResponse({'success': False, 'error': 'no_image'}, status=400)
    if f.size > _MAX_IMAGE_BYTES:
        return JsonResponse({'success': False,
                              'answer': 'الصورة كبيرة (الحد 6 MB).'},
                             status=400)

    caption = (request.POST.get('caption') or '').strip()
    mode = request.POST.get('mode') or 'disassembly'
    if mode not in dict(RepairMode.choices):
        mode = 'disassembly'
    vehicle = _vehicle_from_payload(request.POST)
    sess = _get_or_create_session(request, vehicle)

    image_b64 = base64.b64encode(f.read()).decode('ascii')
    history = request.session.get(_HISTORY_KEY, [])

    result = coach_reply(
        question=caption or 'حلّل الصورة وقولي إذا كنت ماشي صح ولا لا.',
        mode=mode, vehicle=vehicle, history=history, image_b64=image_b64,
    )
    if not result.get('success'):
        return JsonResponse(result, status=200)

    # Persist
    q = RepairQuery.objects.create(
        session=sess, mode=mode,
        part_or_system=result.get('suggested_part', ''),
        question_text=caption or '[صورة فقط]',
    )
    ans = RepairAnswer.objects.create(
        query=q, body_markdown=result['answer'], source=AnswerSource.LLM,
        review_status=VerificationStatus.PENDING,
    )
    q.last_answer = ans
    q.save(update_fields=['last_answer'])

    # Save the photo too — rewind file pointer first
    f.seek(0)
    verdict = _verdict_from_text(result['answer'])
    TechPhoto.objects.create(
        query=q, image=f, caption=caption,
        ai_feedback=result['answer'][:2000], ai_verdict=verdict,
    )

    _push_history(request, 'user', f'[صورة] {caption}'.strip())
    _push_history(request, 'assistant', result['answer'])

    return JsonResponse({
        'success': True,
        'answer': result['answer'],
        'verdict': verdict,
        'mode': mode,
        'query_id': q.id,
        'answer_id': ans.id,
    })


def _verdict_from_text(text: str) -> str:
    head = text[:80]
    if '✅' in head or 'صح كمل' in head or 'تمام' in head:
        return 'correct'
    if '❌' in head or 'غلط' in head or 'ارجع' in head:
        return 'wrong'
    if '⚠️' in head or 'انتبه' in head or 'تحذير' in head:
        return 'warn'
    if '❓' in head or 'مش واضحة' in head or 'صور تاني' in head:
        return 'unclear'
    return ''


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------
@login_required
@require_POST
def repair_atlas_reset(request):
    request.session.pop(_SESSION_KEY, None)
    request.session.pop(_HISTORY_KEY, None)
    request.session.modified = True
    return JsonResponse({'success': True})


# ---------------------------------------------------------------------------
# SuperAdmin review queue
# ---------------------------------------------------------------------------
@staff_member_required
def review_queue(request):
    pending = RepairAnswer.objects.select_related(
        'query', 'query__session', 'query__session__user',
    ).filter(review_status=VerificationStatus.PENDING).order_by('-created_at')[:100]
    return render(request, 'repair_atlas/review_queue.html', {
        'pending': pending,
        'statuses': VerificationStatus.choices,
    })


@staff_member_required
@require_POST
def review_act(request, answer_id: int):
    ans = get_object_or_404(RepairAnswer, id=answer_id)
    action = request.POST.get('action', '')
    note = (request.POST.get('note') or '').strip()
    corrected = (request.POST.get('corrected_body') or '').strip()

    if action not in {'approve', 'correct', 'reject'}:
        return HttpResponseBadRequest('invalid action')

    if action == 'approve':
        ans.review_status = VerificationStatus.APPROVED
        ans.source = AnswerSource.LLM_VERIFIED
    elif action == 'correct':
        if not corrected:
            return HttpResponseBadRequest('corrected_body required')
        ans.body_markdown = corrected
        ans.review_status = VerificationStatus.CORRECTED
        ans.source = AnswerSource.LLM_VERIFIED
    else:
        ans.review_status = VerificationStatus.REJECTED

    ans.reviewed_by = request.user
    ans.reviewed_at = timezone.now()
    ans.review_note = note
    ans.save()

    # Promote to VerifiedKnowledge on approve/correct
    if action in {'approve', 'correct'}:
        _promote_to_kb(ans)

    return redirect('repair_atlas:review-queue')


def _promote_to_kb(answer: RepairAnswer) -> None:
    """Save approved answer as reusable VerifiedKnowledge entry."""
    from .services.repair_coach import _normalize
    q = answer.query
    sess = q.session
    brand_norm = _normalize(sess.brand)
    if not brand_norm:
        return
    VerifiedKnowledge.objects.create(
        brand_norm=brand_norm,
        model_norm=_normalize(sess.model_name),
        year_from=sess.year,
        year_to=sess.year,
        mode=q.mode,
        part_or_system_norm=_normalize(q.part_or_system) or _normalize(q.question_text[:80]),
        question_pattern=q.question_text,
        answer_markdown=answer.body_markdown,
        approved_by=answer.reviewed_by,
    )
