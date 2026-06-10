"""
💬 Live Chat views — Smart routing based on business hours.

GET  /chat/status/                       → JSON {is_open, cairo_time, ...}
POST /chat/open/                          → JSON: live (session_id) أو offline (show_form)
GET  /chat/<id>/messages/                 → الرسائل (للزائر — polling)
POST /chat/<id>/send/                     → إرسال رسالة من الزائر
POST /chat/<id>/close/                    → غلق الجلسة

(للسوبر أدمن)
GET  /superadmin/chat/                    → inbox الجلسات الحية
POST /superadmin/chat/<id>/reply/         → رد الموظف
"""
import json
import logging

from django.contrib import messages as flash
from django.http import JsonResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST, require_GET
from django_tenants.utils import schema_context

from clients.models import Client, ChatSession, ChatMessage
from clients.permissions import widget_required
from clients.services.business_hours import (
    is_business_hours, get_offline_message, get_status_payload,
)
from clients.views.saas_admin_views import saas_admin_required

logger = logging.getLogger('mouss_tec_core')


def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR')


# ─────────────────────────────────────────────────────────────────────
# 🌐 Public endpoints (للزوار من كل tenant)
# ─────────────────────────────────────────────────────────────────────
@require_GET
def chat_status(request):
    return JsonResponse(get_status_payload())


@csrf_protect
@require_POST
def chat_open(request):
    """يبدأ/يستأنف جلسة شات للزائر بناءً على الـ session key."""
    # 🚪 خارج أوقات العمل → نطلب فورم
    if not is_business_hours():
        return JsonResponse({
            'mode': 'offline',
            'message': get_offline_message(),
            'show_form': True,
        })

    if not request.session.session_key:
        request.session.save()
    vkey = request.session.session_key

    name = (request.POST.get('name') or '').strip()[:120]
    email = (request.POST.get('email') or '').strip()[:200]

    tenant = getattr(request, 'tenant', None)
    tenant_obj = tenant if isinstance(tenant, Client) else None

    with schema_context('public'):
        sess = ChatSession.objects.filter(
            visitor_session_key=vkey, status__in=['waiting', 'active']
        ).first()
        if not sess:
            sess = ChatSession.objects.create(
                tenant=tenant_obj,
                visitor_session_key=vkey,
                visitor_name=name, visitor_email=email,
                ip_address=_client_ip(request),
            )
            ChatMessage.objects.create(
                session=sess, sender='bot',
                body=f"أهلاً {name or 'بك'} 👋 موظف الدعم هيرد عليك خلال لحظات."
            )
        else:
            if name and not sess.visitor_name:
                sess.visitor_name = name
                sess.save(update_fields=['visitor_name'])

    return JsonResponse({
        'mode': 'live',
        'session_id': sess.id,
        'poll_interval_ms': 4000,
    })


@require_GET
def chat_messages(request, session_id):
    """polling endpoint — يرجّع الرسائل من after_id لو موجود."""
    after = int(request.GET.get('after', 0) or 0)
    with schema_context('public'):
        sess = get_object_or_404(ChatSession, pk=session_id)
        if request.session.session_key != sess.visitor_session_key:
            return JsonResponse({'error': 'forbidden'}, status=403)
        msgs = list(sess.messages.filter(id__gt=after).values('id', 'sender', 'body', 'created_at'))
    return JsonResponse({
        'session_id': session_id,
        'status': sess.status,
        'messages': [
            {**m, 'created_at': m['created_at'].strftime('%H:%M')} for m in msgs
        ],
    })


@csrf_protect
@require_POST
def chat_send(request, session_id):
    body = (request.POST.get('body') or '').strip()
    if not body:
        return JsonResponse({'ok': False, 'error': 'فارغة'}, status=400)
    if len(body) > 2000:
        return JsonResponse({'ok': False, 'error': 'طويلة جداً'}, status=400)

    with schema_context('public'):
        sess = get_object_or_404(ChatSession, pk=session_id)
        if request.session.session_key != sess.visitor_session_key:
            return JsonResponse({'error': 'forbidden'}, status=403)
        if sess.status == 'closed':
            return JsonResponse({'ok': False, 'error': 'الجلسة مغلقة'}, status=400)
        msg = ChatMessage.objects.create(session=sess, sender='visitor', body=body)
        ChatSession.objects.filter(pk=sess.pk).update(last_activity_at=timezone.now())

    return JsonResponse({'ok': True, 'id': msg.id})


@require_POST
def chat_close(request, session_id):
    with schema_context('public'):
        sess = get_object_or_404(ChatSession, pk=session_id)
        if request.session.session_key != sess.visitor_session_key:
            return JsonResponse({'error': 'forbidden'}, status=403)
        sess.status = 'closed'
        sess.closed_at = timezone.now()
        sess.save(update_fields=['status', 'closed_at'])
    return JsonResponse({'ok': True})


# ─────────────────────────────────────────────────────────────────────
# 🎛️ Super Admin endpoints
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('chat')
def chat_inbox(request):
    qs = ChatSession.objects.exclude(status='closed').order_by('-last_activity_at')
    return render(request, 'clients/saas_admin/chat_inbox.html', {
        'sessions': qs[:100],
        'count_waiting': ChatSession.objects.filter(status='waiting').count(),
        'count_active': ChatSession.objects.filter(status='active').count(),
        'is_open': is_business_hours(),
    })


@saas_admin_required
@widget_required('chat')
def chat_session_detail(request, session_id):
    sess = get_object_or_404(ChatSession, pk=session_id)
    if request.method == 'POST':
        body = (request.POST.get('body') or '').strip()
        if body:
            ChatMessage.objects.create(session=sess, sender='agent', body=body[:2000])
            sess.status = 'active'
            if not sess.agent_id:
                sess.agent = request.user
            sess.last_activity_at = timezone.now()
            sess.save(update_fields=['status', 'agent', 'last_activity_at'])
            # علم رسائل الزائر كمقروءة
            sess.messages.filter(sender='visitor', is_read=False).update(is_read=True)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'ok': True})
        return redirect('saas_chat_session_detail', session_id=sess.id)

    # علم القراءة عند الفتح
    sess.messages.filter(sender='visitor', is_read=False).update(is_read=True)
    return render(request, 'clients/saas_admin/chat_session_detail.html', {
        'session': sess,
        'messages': sess.messages.all(),
    })


@saas_admin_required
@widget_required('chat')
@require_POST
def chat_admin_close(request, session_id):
    sess = get_object_or_404(ChatSession, pk=session_id)
    sess.status = 'closed'
    sess.closed_at = timezone.now()
    sess.save(update_fields=['status', 'closed_at'])
    ChatMessage.objects.create(session=sess, sender='system', body='تم إغلاق الجلسة بواسطة موظف الدعم.')
    flash.success(request, 'تم إغلاق الجلسة.')
    return redirect('saas_chat_inbox')
