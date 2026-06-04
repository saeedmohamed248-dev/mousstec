"""
💬 Conversational Design Builder — HTTP Endpoints (Phase N.3)
=====================================================================
5 endpoints. الـ orchestrator (design_chat_message) هو الـ brain — يقرأ
رسالة المستخدم، يستدعي الـ intent classifier، ويـ route لإحدى مسارات:

  • chat     → generate_chat_reply (LLM-only, no image)
  • generate → compose_mega_prompt → generate_design_image → composite logo
  • refine   → _gen_via_flux_kontext (delta edit) → composite logo

undo و finalize buttons في الـ UI بتـ POST مع explicit intent
override (لا يمرّوا بالـ classifier — قرار صريح من العميل).

Feature flag: DESIGN_CHAT_ENABLED — لو False، كل الـ endpoints بترجع 404.
Advisory lock: 60s lock per turn يمنع double-tap race.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from clients.models import (
    CustomerDesign,
    DesignConversation,
    DesignConversationTurn,
)
from erp_core.ai.design_chat import (
    apply_context_patch,
    classify_chat_intent,
    generate_chat_reply,
)

from ._shared import _marketplace_auth

logger = logging.getLogger('mouss_tec_core')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Feature flag gate — returns 404 (not 503) when disabled to leak nothing.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _ensure_enabled():
    """Raise Http404 (not 503) when the feature is off — looks identical to
    a non-existent URL to outside probes. No `enabled: false` signal leaks."""
    if not bool(getattr(settings, 'DESIGN_CHAT_ENABLED', False)):
        raise Http404('Feature not enabled.')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Conversation lookup — scoped to the authenticated customer.
# 404 on ownership mismatch (not 403) — avoids leaking existence to attackers.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _resolve_conversation(customer, conversation_code) -> DesignConversation | None:
    try:
        return DesignConversation.objects.select_related(
            'customer', 'current_design',
        ).get(
            conversation_code=conversation_code,
            customer=customer,
        )
    except (DesignConversation.DoesNotExist, ValueError):
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Advisory lock helpers — atomic acquire & release.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _acquire_lock(conv: DesignConversation) -> bool:
    """Atomically set locked_until = now + LOCK_TIMEOUT only if no live lock.
    Returns True if we got the lock, False if another turn is in flight."""
    timeout = int(getattr(settings, 'DESIGN_CHAT_LOCK_TIMEOUT_SECONDS', 60))
    now = timezone.now()
    new_lock = now + timedelta(seconds=timeout)
    # WHERE clause: lock is null OR expired. Atomic via DB.
    from django.db.models import Q
    updated = DesignConversation.objects.filter(
        pk=conv.pk,
    ).filter(
        Q(locked_until__isnull=True) | Q(locked_until__lte=now)
    ).update(locked_until=new_lock)
    if updated:
        conv.locked_until = new_lock
        return True
    return False


def _release_lock(conv: DesignConversation) -> None:
    DesignConversation.objects.filter(pk=conv.pk).update(locked_until=None)
    conv.locked_until = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Brand snapshot — captured at conversation start.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _snapshot_brand(customer) -> dict:
    try:
        bp = getattr(customer, 'brand_profile', None)
        if bp and bp.is_active:
            ctx = bp.as_brand_context()
            if bp.auto_inject_logo and bp.has_logo:
                ctx['logo_described'] = True
                try:
                    ctx['logo_url'] = bp.logo_image.url
                except Exception:
                    pass
            return ctx
    except Exception as e:
        logger.warning(f'[DESIGN CHAT] brand snapshot failed: {e}')
    return {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Recent-turn context for the classifier
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _recent_turns(conv: DesignConversation, n: int = 3) -> list[dict[str, str]]:
    return list(
        conv.turns.exclude(role='system')
        .order_by('-created_at')[:n * 2]  # *2: each turn ≈ user + assistant
        .values('role', 'content')
    )[::-1]  # reverse to chronological


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Intent path executors — each returns
#   {'reply', 'image_url'|None, 'design_id'|None, 'engine_used',
#    'image_cost', 'error'|None}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _exec_chat(conv: DesignConversation, user_message: str) -> dict[str, Any]:
    """LLM-only response — no image."""
    reply_result = generate_chat_reply(
        user_message,
        accumulated_context=conv.accumulated_context,
        recent_turns=_recent_turns(conv),
    )
    return {
        'reply': reply_result['reply'],
        'suggested_next': reply_result.get('suggested_next'),
        'image_url': None,
        'design_id': None,
        'engine_used': 'llm_only',
        'image_cost': 0.0,
        'error': reply_result.get('error') if not reply_result.get('success') else None,
    }


def _exec_generate(
    conv: DesignConversation,
    customer,
    user_message: str,
) -> dict[str, Any]:
    """Full-pipeline generation: compose_mega_prompt → smart router → composite logo."""
    from erp_core.ai.design_engine import compose_mega_prompt
    from erp_core.ai.printing_copilot import generate_design_image
    from erp_core.ai.logo_overlay import composite_logo_on_image_url

    ctx = conv.accumulated_context or {}
    raw_idea = ctx.get('raw_idea') or user_message
    selections = ctx.get('selections') or {}
    brand_ctx = None if ctx.get('brand_disabled') else (conv.brand_profile_snapshot or None)

    mega = compose_mega_prompt(
        raw_idea=raw_idea,
        domain=ctx.get('domain', ''),
        selections=selections,
        brand_context=brand_ctx,
        presentation_category=ctx.get('presentation_category'),
        subtype=ctx.get('subtype'),
    )
    if not mega.get('success'):
        return {
            'reply': 'ما قدرتش أصيغ التصميم — جرب توصف فكرتك أكتر.',
            'suggested_next': None,
            'image_url': None, 'design_id': None,
            'engine_used': 'flux', 'image_cost': 0.0,
            'error': mega.get('error', 'mega_compose_failed'),
        }

    img = generate_design_image(
        prompt=mega['mega_prompt'],
        size=mega.get('recommended_size', '1024x1024'),
        negative_prompt=mega.get('negative_prompt', ''),
        category=mega.get('presentation_category'),
        has_text_content=bool(mega.get('text_overlay')),
    )
    if not img.get('success') or not img.get('url'):
        return {
            'reply': 'حصل خطأ أثناء التوليد. جرب تاني.',
            'suggested_next': None,
            'image_url': None, 'design_id': None,
            'engine_used': img.get('engine', 'flux'),
            'image_cost': 0.0,
            'error': img.get('error', 'image_gen_failed'),
        }

    image_url = img['url']
    engine = img.get('engine', 'flux')

    # 🎨 Composite brand logo (FLUX only, skip for Ideogram-rendered designs)
    logo_url = (conv.brand_profile_snapshot or {}).get('logo_url')
    if logo_url and engine != 'ideogram':
        comp = composite_logo_on_image_url(
            image_url=image_url,
            logo_source=logo_url,
            category=mega.get('presentation_category') or '',
        )
        if comp.get('success'):
            image_url = comp['url']

    # Persist as CustomerDesign — this is the source of truth for the image
    design = CustomerDesign.objects.create(
        customer=customer,
        title=raw_idea[:200] or 'Conversational design',
        description=user_message[:1000],
        category='other',
        raw_input=user_message,
        engineered_prompt=mega['mega_prompt'],
        negative_prompt=mega.get('negative_prompt', ''),
        image_url=image_url,
        model_used=engine,
        is_free_trial=False,
    )

    return {
        'reply': 'تم التوليد ✅ — لو حابب تعدّل، اكتب التعديل (مثال: "خليه أزرق").',
        'suggested_next': 'جرب تعديل بسيط أو اضغط Finalize لو عجبك.',
        'image_url': image_url,
        'design_id': design.pk,
        'engine_used': engine,
        'image_cost': _estimate_image_cost(engine),
        'error': None,
    }


def _exec_refine(
    conv: DesignConversation,
    customer,
    user_message: str,
    extracted_changes: dict,
) -> dict[str, Any]:
    """Image-to-image refine via FLUX-Kontext on the current_design."""
    from erp_core.ai.printing_copilot import _gen_via_flux_kontext
    from erp_core.ai.logo_overlay import composite_logo_on_image_url

    if conv.current_design is None or not conv.current_design.image_url:
        # Shouldn't happen — classifier downgrades refine→generate when
        # has_current_design=False — but be defensive.
        return _exec_generate(conv, customer, user_message)

    # Build a concise English edit instruction from extracted_changes + raw msg.
    edit_parts = []
    if extracted_changes.get('color'):
        edit_parts.append(f"change the primary color to {extracted_changes['color']}")
    if extracted_changes.get('position_change'):
        edit_parts.append(f"move the main element to {extracted_changes['position_change']}")
    if extracted_changes.get('style_change'):
        edit_parts.append(f"make the style more {extracted_changes['style_change']}")
    if extracted_changes.get('size_change'):
        edit_parts.append(f"adjust size: {extracted_changes['size_change']}")
    for rm in (extracted_changes.get('remove_elements') or []):
        edit_parts.append(f"remove the {rm}")
    for add in (extracted_changes.get('add_elements') or []):
        edit_parts.append(f"add a {add}")
    if not edit_parts:
        # Pass the user message itself as the edit hint (Kontext can handle
        # natural-language English; Arabic gets a quick fallback wrapper).
        edit_parts.append(f"apply the following edit: {user_message[:200]}")
    edit_instruction = '; '.join(edit_parts)[:600]

    result = _gen_via_flux_kontext(
        image_url=conv.current_design.image_url,
        edit_instruction=edit_instruction,
    )
    if not result.get('success') or not result.get('url'):
        return {
            'reply': 'ما قدرتش أعمل التعديل ده — جرب توضّح أكتر.',
            'suggested_next': None,
            'image_url': None, 'design_id': None,
            'engine_used': 'kontext',
            'image_cost': 0.0,
            'error': result.get('error', 'kontext_failed'),
        }

    image_url = result['url']
    logo_url = (conv.brand_profile_snapshot or {}).get('logo_url')
    if logo_url:
        comp = composite_logo_on_image_url(
            image_url=image_url,
            logo_source=logo_url,
            category=(conv.accumulated_context or {}).get('presentation_category') or '',
        )
        if comp.get('success'):
            image_url = comp['url']

    # NEW CustomerDesign row — prior survives in turn snapshots for undo.
    design = CustomerDesign.objects.create(
        customer=customer,
        title=conv.current_design.title,
        description=f'Refined: {user_message[:500]}',
        category=conv.current_design.category,
        raw_input=user_message,
        engineered_prompt=edit_instruction,
        image_url=image_url,
        model_used='kontext',
        is_free_trial=False,
    )

    return {
        'reply': 'تم التعديل ✅',
        'suggested_next': 'استمر في التعديل أو اضغط Finalize.',
        'image_url': image_url,
        'design_id': design.pk,
        'engine_used': 'kontext',
        'image_cost': _estimate_image_cost('kontext'),
        'error': None,
    }


def _estimate_image_cost(engine: str) -> float:
    """Rough per-image cost in USD — used to track per-session totals."""
    return {
        'flux':     0.025,
        'ideogram': 0.008,
        'kontext':  0.030,
    }.get(engine, 0.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Endpoint 1 — Start a new conversation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@csrf_exempt
@require_http_methods(['POST'])
def design_chat_start(request):
    """POST /marketplace/design-chat/start/ — creates a new conversation."""
    _ensure_enabled()
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'auth_required'}, status=401)

    initial_message = ''
    try:
        body = json.loads(request.body or b'{}')
        initial_message = (body.get('initial_message') or '').strip()[:1000]
    except (json.JSONDecodeError, ValueError):
        pass

    brand_snapshot = _snapshot_brand(customer)
    conv = DesignConversation.objects.create(
        customer=customer,
        stage='planning',
        accumulated_context={
            'raw_idea': initial_message,
            'selections': {},
            'history': [],
            'brand_disabled': False,
        },
        brand_profile_snapshot=brand_snapshot,
    )

    if initial_message:
        DesignConversationTurn.objects.create(
            conversation=conv, turn_index=1, role='user',
            content=initial_message,
            intent='unknown', intent_confidence=0.0,
        )
        conv.turn_count = 1
        conv.save(update_fields=['turn_count'])

    return JsonResponse({
        'conversation_code': str(conv.conversation_code),
        'stage': conv.stage,
        'brand_applied': bool(brand_snapshot),
        'turn_count': conv.turn_count,
    }, status=201)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Endpoint 2 — Send a message (the orchestrator brain)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@csrf_exempt
@require_http_methods(['POST'])
def design_chat_message(request, conversation_code):
    """POST /marketplace/design-chat/<code>/message/ — the turn loop."""
    _ensure_enabled()
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'auth_required'}, status=401)

    conv = _resolve_conversation(customer, conversation_code)
    if conv is None:
        return JsonResponse({'error': 'not_found'}, status=404)

    try:
        body = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'invalid_json'}, status=400)

    user_message = (body.get('message') or '').strip()
    if not user_message:
        return JsonResponse({'error': 'empty_message'}, status=400)
    explicit_intent = (body.get('intent') or '').strip().lower() or None

    # ── Pre-flight: limits + lock ───────────────────────────────
    allowed, reason = conv.can_send_another_turn()
    if not allowed:
        status_map = {
            'max_turns_reached':  (429, 'turn_limit_reached'),
            'max_images_reached': (429, 'image_limit_reached'),
            'in_flight':          (409, 'turn_in_flight'),
            'closed':             (410, 'conversation_closed'),
        }
        status, code = status_map.get(reason, (400, reason))
        return JsonResponse({
            'error': code,
            'detail': reason,
            'turn_count': conv.turn_count,
            'image_count': conv.image_count,
        }, status=status)

    # ── Acquire advisory lock ───────────────────────────────────
    if not _acquire_lock(conv):
        return JsonResponse({
            'error': 'turn_in_flight',
            'retry_after_seconds': int(
                getattr(settings, 'DESIGN_CHAT_LOCK_TIMEOUT_SECONDS', 60)
            ),
        }, status=409)

    try:
        return _process_turn(
            request=request, conv=conv, customer=customer,
            user_message=user_message, explicit_intent=explicit_intent,
        )
    finally:
        _release_lock(conv)


def _process_turn(request, conv, customer, user_message, explicit_intent):
    """The actual turn processing — wrapped so we ALWAYS release the lock."""
    turn_index = conv.turn_count + 1

    # ── Classify (or accept explicit_intent override) ───────────
    if explicit_intent in ('chat', 'generate', 'refine'):
        intent_result = {
            'intent': explicit_intent,
            'confidence': 1.0,
            'extracted_changes': {},
            'reasoning_brief': 'explicit override',
            'raw_intent': explicit_intent,
            'downgraded': False,
            'fallback_reason': None,
            'cost_usd': 0.0,
            'success': True,
        }
    else:
        intent_result = classify_chat_intent(
            user_message,
            has_current_design=conv.current_design is not None,
            recent_turns=_recent_turns(conv),
        )

    intent = intent_result['intent']
    extracted = intent_result['extracted_changes'] or {}

    # ── Apply patch to accumulated_context (always, even for chat) ─
    new_ctx, applied_patch = apply_context_patch(
        conv.accumulated_context or {}, extracted, turn_index,
    )

    # ── Route by intent ─────────────────────────────────────────
    if intent == 'chat':
        exec_result = _exec_chat(conv, user_message)
    elif intent == 'generate':
        exec_result = _exec_generate(conv, customer, user_message)
    elif intent == 'refine':
        exec_result = _exec_refine(conv, customer, user_message, extracted)
    else:
        # Defensive — classify_chat_intent should never return anything else
        exec_result = _exec_chat(conv, user_message)
        intent = 'chat'

    # ── Persist: 2 turns (user + assistant) + conv update ───────
    with transaction.atomic():
        DesignConversationTurn.objects.create(
            conversation=conv, turn_index=turn_index,
            role='user', content=user_message,
            intent=intent, intent_confidence=intent_result['confidence'],
            token_cost_credits=Decimal(str(intent_result['cost_usd'])),
            engine_used='llm_only',
            context_patch=applied_patch,
        )
        new_design = None
        if exec_result.get('design_id'):
            try:
                new_design = CustomerDesign.objects.get(pk=exec_result['design_id'])
            except CustomerDesign.DoesNotExist:
                new_design = None

        DesignConversationTurn.objects.create(
            conversation=conv, turn_index=turn_index,
            role='assistant', content=exec_result['reply'],
            intent=intent, intent_confidence=intent_result['confidence'],
            design_snapshot=new_design,
            token_cost_credits=Decimal('0'),
            image_cost_credits=Decimal(str(exec_result['image_cost'])),
            engine_used=exec_result.get('engine_used', ''),
            error_code=(exec_result.get('error') or '')[:50],
        )

        conv.accumulated_context = new_ctx
        conv.turn_count = turn_index
        conv.total_cost_credits = (
            conv.total_cost_credits
            + Decimal(str(intent_result['cost_usd']))
            + Decimal(str(exec_result['image_cost']))
        )
        if new_design is not None:
            conv.current_design = new_design
            conv.image_count += 1
            if intent == 'generate':
                conv.stage = 'generated'
            elif intent == 'refine':
                conv.stage = 'refining'
        conv.save()

    return JsonResponse({
        'intent': intent,
        'raw_intent': intent_result['raw_intent'],
        'confidence': intent_result['confidence'],
        'downgraded': intent_result['downgraded'],
        'fallback_reason': intent_result['fallback_reason'],
        'reply': exec_result['reply'],
        'suggested_next': exec_result.get('suggested_next'),
        'image_url': exec_result.get('image_url'),
        'design_id': exec_result.get('design_id'),
        'engine_used': exec_result.get('engine_used'),
        'turn_index': turn_index,
        'turn_count': conv.turn_count,
        'image_count': conv.image_count,
        'stage': conv.stage,
        'error': exec_result.get('error'),
        'can_undo': conv.turns.filter(
            role='assistant', design_snapshot__isnull=False,
        ).count() > 1,
    }, status=200)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Endpoint 3 — Undo (explicit button — bypasses classifier)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@csrf_exempt
@require_http_methods(['POST'])
def design_chat_undo(request, conversation_code):
    """POST /marketplace/design-chat/<code>/undo/ — revert current_design
    to the previous turn's snapshot."""
    _ensure_enabled()
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'auth_required'}, status=401)

    conv = _resolve_conversation(customer, conversation_code)
    if conv is None:
        return JsonResponse({'error': 'not_found'}, status=404)

    if conv.stage in ('finalized', 'abandoned'):
        return JsonResponse({'error': 'conversation_closed'}, status=410)

    if not _acquire_lock(conv):
        return JsonResponse({'error': 'turn_in_flight'}, status=409)

    try:
        # Walk back: find the 2nd-most-recent assistant turn with a snapshot.
        # The MOST recent IS the current_design; we want the one before it.
        snapshots = list(
            conv.turns.filter(role='assistant', design_snapshot__isnull=False)
            .order_by('-turn_index', '-created_at')[:2]
        )
        if len(snapshots) < 2:
            return JsonResponse({
                'error': 'nothing_to_undo',
                'detail': 'no prior design snapshot in this conversation',
            }, status=400)

        prior = snapshots[1].design_snapshot
        with transaction.atomic():
            conv.current_design = prior
            # Append a system turn recording the undo for the audit trail
            turn_index = conv.turn_count + 1
            DesignConversationTurn.objects.create(
                conversation=conv, turn_index=turn_index,
                role='system', content='تم التراجع للتصميم السابق',
                intent='undo', intent_confidence=1.0,
                design_snapshot=prior,
                engine_used='undo',
            )
            conv.turn_count = turn_index
            conv.stage = 'refining' if conv.stage == 'refining' else 'generated'
            conv.save(update_fields=['current_design', 'turn_count', 'stage'])

        return JsonResponse({
            'image_url': prior.image_url,
            'design_id': prior.pk,
            'turn_count': conv.turn_count,
            'stage': conv.stage,
            'can_undo': conv.turns.filter(
                role='assistant', design_snapshot__isnull=False,
            ).count() > 1,
        }, status=200)
    finally:
        _release_lock(conv)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Endpoint 4 — Finalize (explicit button — bypasses classifier)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@csrf_exempt
@require_http_methods(['POST'])
def design_chat_finalize(request, conversation_code):
    """POST /marketplace/design-chat/<code>/finalize/ — close the conversation
    and mark the current design as ready."""
    _ensure_enabled()
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'auth_required'}, status=401)

    conv = _resolve_conversation(customer, conversation_code)
    if conv is None:
        return JsonResponse({'error': 'not_found'}, status=404)

    if conv.stage in ('finalized', 'abandoned'):
        return JsonResponse({'error': 'already_closed', 'stage': conv.stage}, status=410)

    if conv.current_design is None:
        return JsonResponse({
            'error': 'no_design_to_finalize',
            'detail': 'generate at least one design before finalizing',
        }, status=400)

    conv.stage = 'finalized'
    conv.finalized_at = timezone.now()
    conv.save(update_fields=['stage', 'finalized_at'])

    return JsonResponse({
        'stage': 'finalized',
        'design_id': conv.current_design.pk,
        'design_code': str(conv.current_design.design_code),
        'image_url': conv.current_design.image_url,
        'turn_count': conv.turn_count,
        'total_cost_credits': str(conv.total_cost_credits),
    }, status=200)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Endpoint 5 — Read-only state fetch (for UI hydration / polling)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@csrf_exempt
@require_http_methods(['GET'])
def design_chat_state(request, conversation_code):
    """GET /marketplace/design-chat/<code>/ — current state + transcript."""
    _ensure_enabled()
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'auth_required'}, status=401)

    conv = _resolve_conversation(customer, conversation_code)
    if conv is None:
        return JsonResponse({'error': 'not_found'}, status=404)

    turns = [
        {
            'turn_index': t.turn_index,
            'role': t.role,
            'content': t.content,
            'intent': t.intent,
            'confidence': t.intent_confidence,
            'image_url': (
                t.design_snapshot.image_url if t.design_snapshot_id else None
            ),
            'engine_used': t.engine_used,
            'created_at': t.created_at.isoformat(),
        }
        for t in conv.turns.select_related('design_snapshot').order_by(
            'turn_index', 'created_at',
        )
    ]

    return JsonResponse({
        'conversation_code': str(conv.conversation_code),
        'stage': conv.stage,
        'turn_count': conv.turn_count,
        'image_count': conv.image_count,
        'total_cost_credits': str(conv.total_cost_credits),
        'current_design': {
            'id': conv.current_design.pk,
            'image_url': conv.current_design.image_url,
            'design_code': str(conv.current_design.design_code),
        } if conv.current_design else None,
        'brand_applied': bool(conv.brand_profile_snapshot),
        'turns': turns,
        'can_undo': conv.turns.filter(
            role='assistant', design_snapshot__isnull=False,
        ).count() > 1,
        'can_finalize': conv.current_design is not None and conv.stage not in ('finalized', 'abandoned'),
    }, status=200)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Endpoint 6 — Page renderer (HTML — the UI shell that consumes the 5 APIs)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def design_chat_page(request):
    """GET /marketplace/design-chat/ — renders the sidebar+canvas UI.

    Same 404-on-disabled-flag policy as the API endpoints. Unauthenticated
    users redirect to the marketplace entry (sector picker has the login
    modal — same pattern as brand_profile_page)."""
    from django.shortcuts import redirect, render
    _ensure_enabled()
    customer = _marketplace_auth(request)
    if not customer:
        return redirect(f'/marketplace/?next={request.path}')
    return render(request, 'clients/marketplace/design_chat.html', {
        'customer': customer,
    })
