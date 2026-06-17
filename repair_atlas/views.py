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
from django.http import JsonResponse, HttpResponseBadRequest, StreamingHttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET

from .models import (
    RepairSession, RepairQuery, RepairAnswer, TechPhoto, VerifiedKnowledge,
    RepairMode, AnswerSource, VerificationStatus, ConfidenceTier,
)
from .services.repair_coach import coach_reply, coach_reply_stream

logger = logging.getLogger('mouss_tec_core')

_SESSION_KEY = 'repair_atlas_session_id_v1'
_HISTORY_KEY = 'repair_atlas_history_v1'
_MAX_HISTORY = 12
_MAX_IMAGE_BYTES = 6 * 1024 * 1024  # 6 MB
_MAX_IMAGES = 4  # عدد الصور المسموح رفعها في رسالة واحدة


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
    # 🔧 الفني (default flow)
    request.session['is_customer_audience'] = False
    request.session.modified = True
    return _render_atlas_page(request, audience='shop')


@login_required
def repair_atlas_customer_page(request):
    """🚙 صاحب السيارة — نفس البوت بستايل بسيط أكثر."""
    request.session['is_customer_audience'] = True
    request.session.modified = True
    return _render_atlas_page(request, audience='customer')


def _render_atlas_page(request, *, audience: str):
    return render(request, 'repair_atlas/repair_atlas.html', {
        'brands': _brand_catalog(),
        'audience': audience,
        'audience_label': 'صاحب السيارة' if audience == 'customer' else 'الفني / الورشة',
        'past_turns_json': json.dumps(_load_past_turns(request, audience),
                                      ensure_ascii=False),
        'modes': [
            {'key': RepairMode.DISASSEMBLY, 'label': 'تفكيك', 'icon': '🔧'},
            {'key': RepairMode.INSTALL,     'label': 'تركيب', 'icon': '🔩'},
            {'key': RepairMode.WIRING,      'label': 'ضفائر', 'icon': '⚡'},
            {'key': RepairMode.LOCATE,      'label': 'مكان قطعة', 'icon': '📍'},
        ],
    })


def _load_past_turns(request, audience: str) -> list[dict]:
    """يرجّع رسايل المحادثة الحيّة عشان الشات يفضل كامل بعد إعادة فتح الصفحة."""
    try:
        from ai_rooms.models import AIRoomConversation
        sk = f'ai_rooms_conv_id__repair_atlas__{audience}'
        conv_id = request.session.get(sk)
        if not conv_id:
            return []
        conv = AIRoomConversation.objects.filter(
            id=conv_id, user=request.user,
        ).first()
        if not conv:
            return []
        out = []
        for t in (conv.turns or []):
            meta = t.get('meta') or {}
            out.append({
                'role': t.get('role', 'assistant'),
                'text': t.get('text', ''),
                'image_url': meta.get('image_url'),
                'tier': meta.get('tier'),
                'confidence': meta.get('confidence'),
            })
        return out
    except Exception:
        logger.debug('[REPAIR_ATLAS] could not load past turns', exc_info=True)
        return []


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

    # ⚡ المسار السريع: رد فوري، والمراجعة الذاتية تتعمل في الخلفية.
    result = coach_reply(question, mode, vehicle, history, verify=False)

    if not result.get('success'):
        return JsonResponse(result, status=200)

    q = RepairQuery.objects.create(
        session=sess, mode=mode,
        part_or_system=result.get('suggested_part', ''),
        question_text=question,
    )
    ans = _persist_answer(q, result)
    q.last_answer = ans
    q.save(update_fields=['last_answer'])

    if not sess.title:
        sess.title = question[:120]
        sess.save(update_fields=['title'])

    _push_history(request, 'user', question)
    _push_history(request, 'assistant', result['answer'])
    _persist_to_unified_hub(request, sess, question, result, answer_id=ans.id)

    pending = _enqueue_verification(request, ans, result)

    return JsonResponse({
        'success': True,
        'answer': result['answer'],
        'source': result['source'],
        'mode': mode,
        'query_id': q.id,
        'answer_id': ans.id,
        'session_id': sess.id,
        'confidence': result.get('confidence'),
        'tier': result.get('tier', 'pending'),
        'doubts': result.get('doubts', []),
        'auto_promoted': result.get('auto_promoted', False),
        'verification_pending': pending,
    })


@login_required
@require_POST
def repair_atlas_ask_stream(request):
    """⚡ نسخة streaming من /api/ask — الرد بيظهر للفني وهو بيتكتب (SSE).

    بيبعت أحداث ``data: {json}\\n\\n``:
      • {"type":"delta","text":"..."}  → قطعة من الرد
      • {"type":"done", ...meta...}    → answer_id + tier + verification_pending
      • {"type":"error","answer":"..."} → فشل
    وبعد ما الرد يكتمل بنحفظه + نـ enqueue المراجعة الذاتية في الخلفية.
    """
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
        return JsonResponse({'success': False,
                             'answer': 'السؤال طويل أوي — اختصره.'}, status=400)

    vehicle = _vehicle_from_payload(body)
    sess = _get_or_create_session(request, vehicle)
    history = request.session.get(_HISTORY_KEY, [])

    # نثبّت اسم الـ schema دلوقتي (جوّه الـ request) عشان لو الـ streaming
    # generator اشتغل على connection تاني وقت الإرسال، الحفظ يفضل في schema
    # التينانت الصح مش public.
    from django.db import connection
    schema_name = getattr(connection, 'schema_name', None) or 'public'

    def _sse(payload: dict) -> str:
        return 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'

    def event_stream():
        from django_tenants.utils import schema_context
        final = None
        try:
            for ev in coach_reply_stream(question, mode, vehicle, history):
                kind = ev.get('type')
                if kind == 'delta':
                    yield _sse({'type': 'delta', 'text': ev['text']})
                elif kind in ('done', 'kb'):
                    final = ev['result']
                    if kind == 'kb':
                        yield _sse({'type': 'delta', 'text': final['answer']})
                elif kind == 'error':
                    yield _sse({'type': 'error',
                                'answer': ev['result'].get('answer', '⚠️ خطأ')})
                    return
        except Exception:
            logger.exception('[REPAIR_ATLAS] stream crashed')
            yield _sse({'type': 'error', 'answer': '⚠️ حصل عطل، جرّب تاني.'})
            return

        if not final or not final.get('success'):
            yield _sse({'type': 'error', 'answer': '⚠️ مفيش رد، جرّب تاني.'})
            return

        # حفظ الرد + المراجعة في الخلفية (نفس منطق /api/ask)
        try:
            with schema_context(schema_name):
                q = RepairQuery.objects.create(
                    session=sess, mode=mode,
                    part_or_system=final.get('suggested_part', ''),
                    question_text=question,
                )
                ans = _persist_answer(q, final)
                q.last_answer = ans
                q.save(update_fields=['last_answer'])
                if not sess.title:
                    sess.title = question[:120]
                    sess.save(update_fields=['title'])
                _push_history(request, 'user', question)
                _push_history(request, 'assistant', final['answer'])
                _persist_to_unified_hub(request, sess, question, final,
                                        answer_id=ans.id)
                pending = _enqueue_verification(request, ans, final)
            yield _sse({
                'type': 'done', 'answer_id': ans.id, 'query_id': q.id,
                'session_id': sess.id, 'source': final['source'],
                'tier': final.get('tier', 'pending'),
                'confidence': final.get('confidence'),
                'doubts': final.get('doubts', []),
                'verification_pending': pending,
            })
        except Exception:
            logger.exception('[REPAIR_ATLAS] stream persist failed')
            # الرد اتعرض للفني بالفعل — منكسرش الـ stream
            yield _sse({'type': 'done', 'verification_pending': False})

    resp = StreamingHttpResponse(event_stream(),
                                 content_type='text/event-stream')
    resp['Cache-Control'] = 'no-cache'
    resp['X-Accel-Buffering'] = 'no'   # يوقف buffering بتاع Nginx للـ SSE
    return resp


def _persist_to_unified_hub(request, sess, user_text, result,
                            image_url=None, answer_id=None):
    try:
        from ai_rooms.services.persist import persist_turn
        audience = 'customer' if request.session.get('is_customer_audience') else 'shop'
        persist_turn(
            request, room='repair_atlas', audience=audience,
            user_text=user_text, assistant_text=result.get('answer', ''),
            vehicle={'brand': sess.brand, 'model_name': sess.model_name,
                     'year': sess.year, 'vin': sess.vin},
            external_session_id=sess.id,
            user_meta={'image_url': image_url} if image_url else None,
            meta={
                'tier': result.get('tier'),
                'confidence': result.get('confidence'),
                'mode': result.get('mode'),
                'auto_promoted': result.get('auto_promoted'),
                'answer_id': answer_id,
            },
        )
    except Exception:
        logger.debug('[REPAIR_ATLAS] ai_rooms persist skipped', exc_info=True)


def _enqueue_verification(request, ans, result) -> bool:
    """يبعت المراجعة الذاتية للـ Celery (في الخلفية). يرجّع True لو اتبعتت
    عشان الواجهة تستنى النتيجة، أو False لو الـ broker مش متاح (الرد يفضل
    شغّال زي ما هو من غير badge)."""
    if not result.get('verification_pending'):
        return False
    try:
        from django.db import connection
        from .tasks import verify_repair_answer
        schema_name = getattr(connection, 'schema_name', None) or 'public'
        verify_repair_answer.delay(schema_name, ans.id)
        return True
    except Exception:
        logger.warning('[REPAIR_ATLAS] could not enqueue verification', exc_info=True)
        return False


@login_required
@require_GET
def repair_atlas_verdict(request, answer_id: int):
    """polling endpoint: ترجّع نتيجة المراجعة الذاتية لرد معيّن.

    ``ready=True`` لما المراجعة في الخلفية تخلص (الـ tier بقى high/medium/low).
    """
    sess_ids = list(RepairSession.objects.filter(
        user=request.user).values_list('id', flat=True))
    ans = (RepairAnswer.objects
           .select_related('query')
           .filter(id=answer_id, query__session_id__in=sess_ids).first())
    if not ans:
        return JsonResponse({'ready': False, 'error': 'not_found'}, status=404)

    ready = ans.confidence_tier in (
        ConfidenceTier.HIGH, ConfidenceTier.MEDIUM, ConfidenceTier.LOW)
    return JsonResponse({
        'ready': ready,
        'tier': ans.confidence_tier if ready else 'pending',
        'confidence': ans.confidence_score if ready else None,
        'doubts': ans.verifier_doubts or [],
        'auto_promoted': ans.auto_promoted,
    })


# ---------------------------------------------------------------------------
# Persistence helper — wires Verifier results onto RepairAnswer + auto-promotes
# ---------------------------------------------------------------------------
_TIER_TO_CHOICE = {
    'high': ConfidenceTier.HIGH,
    'medium': ConfidenceTier.MEDIUM,
    'low': ConfidenceTier.LOW,
}


def _persist_answer(query: RepairQuery, result: dict) -> RepairAnswer:
    source = result.get('source', 'llm')
    if source == 'verified':
        ans_source = AnswerSource.VERIFIED
        review_status = VerificationStatus.APPROVED
    elif source == 'ai_auto_verified':
        ans_source = AnswerSource.AI_AUTO_VERIFIED
        review_status = VerificationStatus.APPROVED
    else:
        ans_source = AnswerSource.LLM
        review_status = VerificationStatus.PENDING

    ans = RepairAnswer.objects.create(
        query=query,
        body_markdown=result['answer'],
        source=ans_source,
        verified_kb_id=result.get('used_verified_kb_id'),
        review_status=review_status,
        confidence_score=result.get('confidence', 0),
        confidence_tier=_TIER_TO_CHOICE.get(result.get('tier', ''),
                                             ConfidenceTier.UNKNOWN),
        verifier_doubts=result.get('doubts', []),
        auto_promoted=result.get('auto_promoted', False),
        revision_count=result.get('revisions', 0),
    )

    # 🤖 Auto-promote to VerifiedKnowledge — لا تدخل بشري
    if ans.auto_promoted and source != 'verified':
        _promote_to_kb_auto(ans)

    return ans


def _promote_to_kb_auto(answer: RepairAnswer) -> None:
    """نفس منطق _promote_to_kb لكن بدون reviewed_by (الـ Verifier هو اللي وافق)."""
    from .services.repair_coach import _normalize
    q = answer.query
    sess = q.session
    brand_norm = _normalize(sess.brand)
    if not brand_norm:
        return
    try:
        VerifiedKnowledge.objects.create(
            brand_norm=brand_norm,
            model_norm=_normalize(sess.model_name),
            year_from=sess.year,
            year_to=sess.year,
            mode=q.mode,
            part_or_system_norm=(_normalize(q.part_or_system)
                                  or _normalize(q.question_text[:80])),
            question_pattern=q.question_text,
            answer_markdown=answer.body_markdown,
            oem_source=f'AI auto-verified (confidence={answer.confidence_score})',
        )
    except Exception:
        logger.exception('[REPAIR_ATLAS] auto-promote to KB failed')


# ---------------------------------------------------------------------------
# Photo API — Vision feedback loop
# ---------------------------------------------------------------------------
@login_required
@require_POST
def repair_atlas_photo(request):
    """Receives multipart form: image (one or more files), caption, mode, vehicle fields."""
    files = request.FILES.getlist('image')
    if not files:
        return JsonResponse({'success': False, 'error': 'no_image'}, status=400)
    if len(files) > _MAX_IMAGES:
        files = files[:_MAX_IMAGES]
    for f in files:
        if f.size > _MAX_IMAGE_BYTES:
            return JsonResponse({'success': False,
                                  'answer': f'صورة كبيرة ({f.name}) — الحد 6 MB لكل صورة.'},
                                 status=400)

    caption = (request.POST.get('caption') or '').strip()
    mode = request.POST.get('mode') or 'disassembly'
    if mode not in dict(RepairMode.choices):
        mode = 'disassembly'
    vehicle = _vehicle_from_payload(request.POST)
    sess = _get_or_create_session(request, vehicle)

    images_b64 = []
    for f in files:
        images_b64.append(base64.b64encode(f.read()).decode('ascii'))
        f.seek(0)
    history = request.session.get(_HISTORY_KEY, [])

    result = coach_reply(
        question=caption or 'حلّل الصور وقولي إذا كنت ماشي صح ولا لا.',
        mode=mode, vehicle=vehicle, history=history, images_b64=images_b64,
        verify=False,
    )
    if not result.get('success'):
        return JsonResponse(result, status=200)

    q = RepairQuery.objects.create(
        session=sess, mode=mode,
        part_or_system=result.get('suggested_part', ''),
        question_text=caption or '[صورة فقط]',
    )
    ans = _persist_answer(q, result)
    q.last_answer = ans
    q.save(update_fields=['last_answer'])

    verdict = _verdict_from_text(result['answer'])
    image_url = None
    for idx, f in enumerate(files):
        f.seek(0)
        photo = TechPhoto.objects.create(
            query=q, image=f, caption=caption,
            ai_feedback=result['answer'][:2000] if idx == 0 else '',
            ai_verdict=verdict if idx == 0 else '',
        )
        if idx == 0:
            try:
                image_url = photo.image.url
            except Exception:
                image_url = None

    label = f'[صورة] {caption}'.strip() if len(files) == 1 else f'[{len(files)} صور] {caption}'.strip()
    _push_history(request, 'user', label)
    _push_history(request, 'assistant', result['answer'])
    _persist_to_unified_hub(request, sess, label, result,
                            image_url=image_url, answer_id=ans.id)

    pending = _enqueue_verification(request, ans, result)

    return JsonResponse({
        'success': True,
        'answer': result['answer'],
        'verdict': verdict,
        'mode': mode,
        'query_id': q.id,
        'answer_id': ans.id,
        'confidence': result.get('confidence'),
        'tier': result.get('tier', 'pending'),
        'doubts': result.get('doubts', []),
        'auto_promoted': result.get('auto_promoted', False),
        'verification_pending': pending,
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
