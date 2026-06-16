"""AI Rooms Hub + history views."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max
from django.http import Http404
from django.shortcuts import render, get_object_or_404

from .models import AIRoomConversation, RoomKind, Audience


ROOM_META = {
    RoomKind.DIAGNOSTIC_ROOM: {
        'title': 'غرفة التشخيص',
        'subtitle': 'OBD + Web Bluetooth + Live Data',
        'emoji': '🩺',
        'gradient': 'linear-gradient(135deg, #00d4ff, #0099ff)',
        'url_name': 'smart_diagnostics:diagnostics-room',
    },
    RoomKind.AUTO_DIAGNOSTIC: {
        'title': 'خبير التشخيص متعدد الماركات',
        'subtitle': 'BMW / Mercedes / Toyota / Hyundai ...',
        'emoji': '🚗',
        'gradient': 'linear-gradient(135deg, #6366f1, #8b5cf6)',
        'url_name': 'diagnostic_shop',
    },
    RoomKind.REPAIR_ATLAS: {
        'title': 'أطلس الإصلاح',
        'subtitle': 'تفكيك، تركيب، ضفائر — Vision Coach',
        'emoji': '🔧',
        'gradient': 'linear-gradient(135deg, #ff9500, #ff6b6b)',
        'url_name': 'repair_atlas:page',
    },
}


def _audience_for(request) -> str:
    """تحديد الجمهور: لو الـ session بتاع Marketplace customer مفعّل → customer."""
    if request.session.get('is_customer_audience'):
        return Audience.CUSTOMER
    return Audience.SHOP


@login_required
def hub(request):
    audience = _audience_for(request)
    recent = (AIRoomConversation.objects
              .filter(user=request.user, audience=audience)
              .order_by('-updated_at')[:15])
    by_room_counts = dict(AIRoomConversation.objects
                          .filter(user=request.user, audience=audience)
                          .values_list('room')
                          .annotate(c=Count('id'))
                          .values_list('room', 'c'))
    rooms = []
    for kind, meta in ROOM_META.items():
        rooms.append({
            'kind': kind, 'count': by_room_counts.get(kind, 0), **meta,
        })
    return render(request, 'ai_rooms/hub.html', {
        'rooms': rooms,
        'recent': recent,
        'audience': audience,
        'room_meta': ROOM_META,
    })


@login_required
def history(request):
    audience = _audience_for(request)
    room_filter = request.GET.get('room', '')
    qs = AIRoomConversation.objects.filter(user=request.user, audience=audience)
    if room_filter in dict(RoomKind.choices):
        qs = qs.filter(room=room_filter)
    qs = qs.order_by('-updated_at')[:100]
    return render(request, 'ai_rooms/history.html', {
        'conversations': qs,
        'audience': audience,
        'room_filter': room_filter,
        'rooms': RoomKind.choices,
        'room_meta': ROOM_META,
    })


@login_required
def conversation_detail(request, conv_id: int):
    conv = get_object_or_404(AIRoomConversation, id=conv_id, user=request.user)
    return render(request, 'ai_rooms/conversation_detail.html', {
        'conv': conv,
        'meta': ROOM_META.get(conv.room, {}),
    })
