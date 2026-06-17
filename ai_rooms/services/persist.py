"""
🔌 Persistence helper — تستدعى من أي غرفة عشان تكتب turn في الـ unified backbone.

Pattern:
    from ai_rooms.services.persist import persist_turn

    conv = persist_turn(
        request, room='auto_diagnostic',
        audience='shop', user_text='شكواي كذا',
        assistant_text='التشخيص كذا',
        vehicle={'brand': 'BMW', 'model_name': 'E90'},
        meta={'tier': 'high', 'confidence': 92},
    )

كل غرفة بتمسك session_key واحد جوه request.session عشان تتفادى إنشاء
محادثة جديدة لكل turn.
"""
from __future__ import annotations

import logging
from typing import Any

from django.db import DatabaseError
from django.http import HttpRequest

from ..models import AIRoomConversation, RoomKind, Audience

logger = logging.getLogger('mouss_tec_core')


_SESSION_KEY_PREFIX = 'ai_rooms_conv_id'


def _session_key(room: str, audience: str) -> str:
    return f'{_SESSION_KEY_PREFIX}__{room}__{audience}'


def get_or_open_conversation(
    request: HttpRequest, *, room: str, audience: str = 'shop',
    vehicle: dict[str, Any] | None = None,
    external_session_id: int | None = None,
) -> AIRoomConversation | None:
    """Return the live conversation for this (user, room, audience). Creates if missing."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return None

    if room not in dict(RoomKind.choices):
        return None
    if audience not in dict(Audience.choices):
        audience = 'shop'

    sk = _session_key(room, audience)
    conv_id = request.session.get(sk)
    if conv_id:
        try:
            conv = AIRoomConversation.objects.filter(id=conv_id, user=user).first()
            if conv:
                if vehicle:
                    _patch_vehicle(conv, vehicle)
                return conv
        except DatabaseError:
            logger.debug('[AI_ROOMS] table missing — skip persistence')
            return None

    try:
        conv = AIRoomConversation.objects.create(
            user=user, room=room, audience=audience,
            brand=(vehicle or {}).get('brand', '') or '',
            model_name=(vehicle or {}).get('model_name', '') or '',
            year=(vehicle or {}).get('year') or None,
            vin=(vehicle or {}).get('vin', '') or '',
            external_session_id=external_session_id,
        )
    except DatabaseError:
        logger.debug('[AI_ROOMS] table missing — cannot create conversation')
        return None

    request.session[sk] = conv.id
    request.session.modified = True
    return conv


def persist_turn(
    request: HttpRequest, *, room: str, audience: str = 'shop',
    user_text: str = '', assistant_text: str = '',
    vehicle: dict[str, Any] | None = None,
    external_session_id: int | None = None,
    meta: dict[str, Any] | None = None,
    user_meta: dict[str, Any] | None = None,
) -> AIRoomConversation | None:
    """Append a user + assistant turn to the unified conversation.

    ``user_meta`` يتعلّق برسالة الفني (مثلاً رابط صورة رفعها)، و``meta``
    يتعلّق برد المساعد (tier / confidence / mode).
    """
    conv = get_or_open_conversation(
        request, room=room, audience=audience,
        vehicle=vehicle, external_session_id=external_session_id,
    )
    if not conv:
        return None
    try:
        if user_text or user_meta:
            conv.append_turn('user', user_text, meta=user_meta)
        if assistant_text:
            conv.append_turn('assistant', assistant_text, meta=meta)
    except DatabaseError:
        logger.debug('[AI_ROOMS] turn append failed')
    return conv


def close_conversation(request: HttpRequest, *, room: str, audience: str = 'shop') -> None:
    sk = _session_key(room, audience)
    conv_id = request.session.pop(sk, None)
    request.session.modified = True
    if not conv_id:
        return
    try:
        from django.utils import timezone
        AIRoomConversation.objects.filter(
            id=conv_id, user=request.user,
        ).update(closed_at=timezone.now())
    except DatabaseError:
        pass


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _patch_vehicle(conv: AIRoomConversation, vehicle: dict) -> None:
    dirty = False
    for field, key in (('brand', 'brand'), ('model_name', 'model_name'),
                        ('vin', 'vin')):
        val = (vehicle.get(key) or '').strip()
        if val and getattr(conv, field) != val:
            setattr(conv, field, val)
            dirty = True
    year = vehicle.get('year')
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    if year and conv.year != year:
        conv.year = year
        dirty = True
    if dirty:
        try:
            conv.save(update_fields=['brand', 'model_name', 'year', 'vin'])
        except DatabaseError:
            pass
