"""🎥 تسجيل شاشة جلسات الشركات (rrweb) — استقبال من المتصفح + مشاهدة لصاحب المنصة.

* الاستقبال (`screen_rec_ingest`) على دومين الشركة: موظف مسجّل دخول + الشركة مفعّل لها التسجيل.
  الأحداث بتتخزّن مضغوطة (zlib) في public عبر schema_context.
* المشاهدة لصاحب المنصة بس (platform_owner_required) — لايف (polling) أو إعادة كاملة.
"""
from __future__ import annotations

import json
import logging
import random
import uuid
import zlib
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, connection
from django.db.models import F
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django_tenants.utils import schema_context

from clients.permissions import platform_owner_required

logger = logging.getLogger(__name__)

MAX_CHUNK_BYTES = 2 * 1024 * 1024        # دفعة واحدة من المتصفح
MAX_SESSION_BYTES = 60 * 1024 * 1024     # بعد كده بنوقف تخزين الجلسة دي
LIVE_WINDOW = timedelta(seconds=30)      # آخر حدث خلال ٣٠ ثانية = لايف


def retention_days():
    return int(getattr(settings, 'SCREEN_RECORDING_RETENTION_DAYS', 7))


def purge_old_sessions():
    from clients.models import ScreenSession
    cutoff = timezone.now() - timedelta(days=retention_days())
    with schema_context('public'):
        n, _ = ScreenSession.objects.filter(last_event_at__lt=cutoff).delete()
    return n


def recording_enabled_for(request):
    """التسجيل شغّال للطلب ده؟ (دومين شركة + موظف داخل + الشركة مفعّلاه)."""
    schema = getattr(connection, 'schema_name', 'public')
    if schema == 'public':
        return False
    if not getattr(settings, 'SCREEN_RECORDING_ENABLED', True):
        return False
    user = getattr(request, 'user', None)
    if not (user and user.is_authenticated):
        return False
    session = getattr(request, 'session', None)
    if session is not None and session.get('platform_owner_entered'):
        return False
    tenant = getattr(request, 'tenant', None)
    return bool(tenant is not None and getattr(tenant, 'screen_recording_enabled', False))


def _same_origin(request):
    """بديل CSRF هنا: sendBeacon (آخر دفعة وقت قفل الصفحة) مابيقدرش يبعت هيدر التوكن.
    بنقبل بس لو Origin (أو Referer) هو نفس الدومين."""
    from urllib.parse import urlsplit
    src = request.META.get('HTTP_ORIGIN') or request.META.get('HTTP_REFERER') or ''
    return bool(src) and urlsplit(src).netloc == request.get_host()


@csrf_exempt
@require_POST
def screen_rec_ingest(request):
    if not _same_origin(request):
        return JsonResponse({'ok': False, 'error': 'origin'}, status=403)
    if not recording_enabled_for(request):
        return JsonResponse({'ok': False, 'disabled': True}, status=403)
    if len(request.body) > MAX_CHUNK_BYTES:
        return JsonResponse({'ok': False, 'error': 'too_large'}, status=413)
    try:
        payload = json.loads(request.body)
        sid = uuid.UUID(str(payload['sid']))
        seq = int(payload['seq'])
        events = payload['events']
        if not isinstance(events, list) or not events or seq < 0:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'ok': False, 'error': 'bad_payload'}, status=400)

    from clients.models import ScreenChunk, ScreenSession
    schema = connection.schema_name
    user = request.user
    path = str(payload.get('path') or request.META.get('HTTP_REFERER', ''))[:500]
    raw = json.dumps(events, separators=(',', ':')).encode()
    blob = zlib.compress(raw, 6)
    now = timezone.now()
    with schema_context('public'):
        session, created = ScreenSession.objects.get_or_create(
            session_key=sid,
            defaults={
                'tenant_schema': schema,
                'username': user.get_username()[:150],
                'user_display': (user.get_full_name() or user.get_username())[:200],
                'first_path': path, 'last_path': path,
                'user_agent': request.META.get('HTTP_USER_AGENT', '')[:500],
                'last_event_at': now,
            },
        )
        # 🛡️ جلسة شركة/موظف تاني بنفس الـ sid ⇒ مرفوض (مايتخلطش تسجيل حد على حد)
        if session.tenant_schema != schema or session.username != user.get_username()[:150]:
            return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)
        if session.bytes_total > MAX_SESSION_BYTES:
            return JsonResponse({'ok': True, 'stopped': True})
        try:
            ScreenChunk.objects.create(session=session, seq=seq, data=blob)
        except IntegrityError:
            return JsonResponse({'ok': True, 'duplicate': True})
        ScreenSession.objects.filter(pk=session.pk).update(
            last_event_at=now, last_path=path,
            chunks_count=F('chunks_count') + 1, bytes_total=F('bytes_total') + len(blob),
        )
    if random.random() < 0.01:   # تنظيف كسول للتسجيلات القديمة
        try:
            purge_old_sessions()
        except Exception:
            logger.exception('[SCREEN-REC] purge failed')
    return JsonResponse({'ok': True})


def sessions_for_tenant(schema_name, limit=20):
    from clients.models import ScreenSession
    now = timezone.now()
    rows = list(ScreenSession.objects.filter(tenant_schema=schema_name)
                .order_by('-last_event_at')[:limit])
    for s in rows:
        s.is_live = now - s.last_event_at <= LIVE_WINDOW
        s.duration_seconds = int((s.last_event_at - s.started_at).total_seconds())
    return rows


@login_required
@platform_owner_required
def screen_session_player(request, session_id):
    from clients.models import Client, ScreenSession
    session = get_object_or_404(ScreenSession, pk=session_id)
    tenant = Client.all_objects.filter(schema_name=session.tenant_schema).first()
    return render(request, 'clients/saas_admin/screen_player.html', {
        'session': session, 'tenant': tenant,
        'is_live': timezone.now() - session.last_event_at <= LIVE_WINDOW,
    })


@login_required
@platform_owner_required
@require_GET
def screen_session_events(request, session_id):
    """الأحداث بعد seq معيّن (للايف) أو كلها — JSON."""
    from clients.models import ScreenSession
    session = get_object_or_404(ScreenSession, pk=session_id)
    try:
        after = int(request.GET.get('after', -1))
    except (TypeError, ValueError):
        after = -1
    events, last_seq = [], after
    for chunk in session.chunks.filter(seq__gt=after).order_by('seq')[:200]:
        try:
            events.extend(json.loads(zlib.decompress(bytes(chunk.data))))
        except (zlib.error, ValueError):
            logger.warning('[SCREEN-REC] bad chunk %s', chunk.pk)
        last_seq = chunk.seq
    return JsonResponse({
        'events': events, 'last_seq': last_seq,
        'live': timezone.now() - session.last_event_at <= LIVE_WINDOW,
        'last_path': session.last_path,
    })


@login_required
@platform_owner_required
@require_POST
def screen_rec_toggle(request, tenant_id):
    """تشغيل/إيقاف تسجيل الشاشة لشركة — من صفحة Tenant 360."""
    from django.shortcuts import redirect
    from clients.models import Client
    tenant = get_object_or_404(Client, pk=tenant_id)
    tenant.screen_recording_enabled = not tenant.screen_recording_enabled
    tenant.save(update_fields=['screen_recording_enabled'])
    return redirect(f'/superadmin/tenant/{tenant.pk}/360/#screen-rec')
