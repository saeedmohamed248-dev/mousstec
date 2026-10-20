"""Tech Workspace — Pillar 3 of the unified DMS.

Mobile-first dashboard for technicians and engineers:
  - Live list of open Job Cards assigned to / available for them
  - Start / pause / resume / complete a RepairLog timer
  - Upload before/after photos
  - Flag "needs extra parts" so cashier sees it instantly
  - GPS check-in button (POSTs to /system/api/attendance/checkin/)
"""
from __future__ import annotations

import json
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import (
    AttendanceCheckIn,
    EmployeeProfile,
    RepairLog,
    RepairLogMedia,
    SaleInvoice,
)
from .views import role_required, tenant_required

ACTIVE_JOB_STATUSES = ('quotation', 'in_progress', 'quality_check', 'ready')


def _json_ok(**kw):
    return JsonResponse({"ok": True, **kw})


def _json_err(msg, status=400, **kw):
    return JsonResponse({"ok": False, "error": msg, **kw}, status=status)


def _profile(request) -> EmployeeProfile | None:
    return getattr(request.user, 'employee_profile', None)


# ---------------------------------------------------------------------------
# Workspace page
# ---------------------------------------------------------------------------

@login_required(login_url='/login/')
@tenant_required
@role_required('tech', 'engineer')
def tech_workspace(request):
    profile = _profile(request)
    # 🐛 [FIX]: كان تاريخ UTC — من ١٢ لـ ٣ الفجر بتوقيت المحل «النهارده» كان
    #    امبارح، و`occurred_at__date`/`ended_at__date` بيقارنوا بالتاريخ المحلي.
    today = timezone.localdate()

    # Job cards in flight at this branch (engineer can grab any; tech sees all open work too)
    job_qs = (SaleInvoice.objects
              .filter(invoice_type='maintenance', status__in=ACTIVE_JOB_STATUSES)
              .select_related('customer', 'vehicle', 'branch')
              .order_by('-date_created')[:25])

    my_open_logs = (RepairLog.objects
                    .filter(technician=profile, status__in=['open', 'paused', 'blocked'])
                    .select_related('job_card', 'job_card__customer', 'job_card__vehicle')
                    .order_by('-started_at'))

    completed_today = (RepairLog.objects
                       .filter(technician=profile, status='done',
                               ended_at__date=today)
                       .count())

    last_checkin = (AttendanceCheckIn.objects
                    .filter(employee=profile)
                    .order_by('-occurred_at').first())

    return render(request, 'inventory/tech_workspace.html', {
        'profile': profile,
        'job_cards': job_qs,
        'my_open_logs': my_open_logs,
        'completed_today': completed_today,
        'last_checkin': last_checkin,
    })


# ---------------------------------------------------------------------------
# RepairLog APIs
# ---------------------------------------------------------------------------

@login_required(login_url='/login/')
@tenant_required
@role_required('tech', 'engineer')
@require_POST
def repair_log_start(request):
    """POST {job_card_id, task_title} → creates RepairLog with status='open'."""
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return _json_err("invalid_json")

    job_card_id = payload.get('job_card_id')
    task_title = (payload.get('task_title') or '').strip()
    if not job_card_id or not task_title:
        return _json_err("missing_fields")

    job_card = SaleInvoice.objects.filter(pk=job_card_id,
                                          invoice_type='maintenance').first()
    if job_card is None:
        return _json_err("job_card_not_found", status=404)
    if job_card.status not in ACTIVE_JOB_STATUSES:
        return _json_err("job_card_closed")

    log = RepairLog.objects.create(
        job_card=job_card,
        technician=_profile(request),
        task_title=task_title[:160],
        tech_notes=(payload.get('tech_notes') or '')[:2000],
        status='open',
    )
    # Flip the Job Card to 'in_progress' once first technician starts
    if job_card.status == 'quotation':
        job_card.status = 'in_progress'
        job_card.save(update_fields=['status'])

    return _json_ok(log_id=log.id, started_at=log.started_at.isoformat())


@login_required(login_url='/login/')
@tenant_required
@role_required('tech', 'engineer')
@require_POST
def repair_log_pause(request, log_id: int):
    log = get_object_or_404(RepairLog, pk=log_id, technician=_profile(request))
    if log.status != 'open':
        return _json_err("not_open")
    log.status = 'paused'
    log.last_paused_at = timezone.now()
    log.save(update_fields=['status', 'last_paused_at'])
    return _json_ok(status=log.status)


@login_required(login_url='/login/')
@tenant_required
@role_required('tech', 'engineer')
@require_POST
def repair_log_resume(request, log_id: int):
    log = get_object_or_404(RepairLog, pk=log_id, technician=_profile(request))
    if log.status != 'paused':
        return _json_err("not_paused")
    if log.last_paused_at:
        delta = (timezone.now() - log.last_paused_at).total_seconds()
        log.paused_seconds = (log.paused_seconds or 0) + int(max(delta, 0))
    log.status = 'open'
    log.last_paused_at = None
    log.save(update_fields=['status', 'paused_seconds', 'last_paused_at'])
    return _json_ok(status=log.status, paused_seconds=log.paused_seconds)


@login_required(login_url='/login/')
@tenant_required
@role_required('tech', 'engineer')
@require_POST
def repair_log_complete(request, log_id: int):
    log = get_object_or_404(RepairLog, pk=log_id, technician=_profile(request))
    if log.status == 'done':
        return _json_err("already_done")
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        payload = {}
    final_notes = (payload.get('tech_notes') or '').strip()
    if final_notes:
        log.tech_notes = (log.tech_notes + "\n" + final_notes)[:4000] if log.tech_notes else final_notes
    # If paused at completion, close out paused time
    if log.status == 'paused' and log.last_paused_at:
        delta = (timezone.now() - log.last_paused_at).total_seconds()
        log.paused_seconds = (log.paused_seconds or 0) + int(max(delta, 0))
        log.last_paused_at = None
    log.status = 'done'
    log.ended_at = timezone.now()
    log.save(update_fields=['status', 'ended_at', 'paused_seconds',
                            'last_paused_at', 'tech_notes'])
    return _json_ok(
        status=log.status,
        ended_at=log.ended_at.isoformat(),
        duration_minutes=log.duration_minutes,
    )


@login_required(login_url='/login/')
@tenant_required
@role_required('tech', 'engineer')
@require_POST
def repair_log_flag_extra_parts(request, log_id: int):
    log = get_object_or_404(RepairLog, pk=log_id, technician=_profile(request))
    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        payload = {}
    log.needs_extra_parts = bool(payload.get('needs_extra_parts', True))
    log.extra_parts_note = (payload.get('extra_parts_note') or '')[:2000]
    if log.needs_extra_parts and log.status == 'open':
        log.status = 'blocked'
    elif not log.needs_extra_parts and log.status == 'blocked':
        log.status = 'open'
    log.save(update_fields=['needs_extra_parts', 'extra_parts_note', 'status'])
    return _json_ok(
        status=log.status,
        needs_extra_parts=log.needs_extra_parts,
        extra_parts_note=log.extra_parts_note,
    )


@login_required(login_url='/login/')
@tenant_required
@role_required('tech', 'engineer')
@require_POST
def repair_log_upload_media(request, log_id: int):
    """multipart/form-data: kind, caption, image=<file>"""
    log = get_object_or_404(RepairLog, pk=log_id, technician=_profile(request))
    image = request.FILES.get('image')
    if not image:
        return _json_err("image_required")
    kind = request.POST.get('kind', 'before')
    if kind not in {'before', 'after', 'issue'}:
        return _json_err("invalid_kind")
    media = RepairLogMedia.objects.create(
        log=log, kind=kind, image=image,
        caption=(request.POST.get('caption') or '')[:200],
    )
    return _json_ok(media_id=media.id, url=media.image.url, kind=media.kind)


# ---------------------------------------------------------------------------
# GPS attendance — Pillar 1
# ---------------------------------------------------------------------------

# 📍 دقة GPS أسوأ من كده = مانقدرش نحكم إن الموظف جوه الورشة
GEOFENCE_MAX_ACCURACY_M = 100


def _geofence_verdict(lat, lng, accuracy):
    """(جوه النطاق؟, سبب التنبيه) — حسب موقع الشركة ونطاقها في إعدادات HR.

    🐛 [FIX]: is_inside_geofence/flagged_reason كانوا أعمدة ميتة (دايماً «برّه»
    ومن غير سبب) — فالحضور من البيت شكله زي الحضور من الورشة. الحركة بتتسجّل
    دايماً (مابنمنعهاش) والـ HR بيشوف التنبيه ويقرّر.
    """
    try:
        from hr.models import HRSettings
        from hr.services.attendance_service import AttendanceService
        hs = HRSettings.objects.first()
        default_lat = HRSettings._meta.get_field('geofence_latitude').default
        default_lng = HRSettings._meta.get_field('geofence_longitude').default
        if hs is None or (hs.geofence_latitude == default_lat and hs.geofence_longitude == default_lng):
            return False, 'موقع الشركة مش متضبط في إعدادات HR'
        inside, distance = AttendanceService.validate_geolocation(lat, lng)
    except Exception:  # noqa: BLE001 — التحقق مايوقفش تسجيل الحضور
        return False, 'تعذّر التحقق من الموقع'
    if accuracy is not None and accuracy > GEOFENCE_MAX_ACCURACY_M:
        return inside, f'دقة GPS ضعيفة ({accuracy:.0f} م)'
    if not inside:
        return False, f'خارج نطاق الورشة ({distance:.0f} م)'
    return True, ''


@login_required(login_url='/login/')
@tenant_required
@require_POST
def attendance_checkin_api(request):
    """POST {event_type, lat, lng, accuracy} from navigator.geolocation."""
    profile = _profile(request)
    if profile is None:
        return _json_err("no_employee_profile", status=403)

    try:
        payload = json.loads(request.body or b'{}')
    except ValueError:
        return _json_err("invalid_json")

    try:
        lat = Decimal(str(payload['lat'])).quantize(Decimal('0.000001'))
        lng = Decimal(str(payload['lng'])).quantize(Decimal('0.000001'))
    except (KeyError, ValueError, TypeError, ArithmeticError):   # «abc»/NaN كانوا 500
        return _json_err("coords_required")
    if not (lat.is_finite() and lng.is_finite()) or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return _json_err("coords_out_of_range")

    event_type = payload.get('event_type', 'in')
    if event_type not in {'in', 'out'}:
        return _json_err("invalid_event_type")

    try:
        accuracy = float(payload.get('accuracy')) if payload.get('accuracy') not in (None, '') else None
    except (TypeError, ValueError):
        accuracy = None
    inside, reason = _geofence_verdict(lat, lng, accuracy)
    rec = AttendanceCheckIn.objects.create(
        employee=profile,
        event_type=event_type,
        lat=lat, lng=lng,
        accuracy_m=accuracy,
        ip_address=request.META.get('REMOTE_ADDR'),
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:255],
        is_inside_geofence=inside,
        flagged_reason=reason,
    )
    EmployeeProfile.objects.filter(pk=profile.pk).update(
        last_checkin_at=rec.occurred_at, last_lat=rec.lat, last_lng=rec.lng,
    )
    # نفس الحركة في سجل الحضور اللي المرتبات بتتحسب منه (لو ليه ملف HR)
    from hr.services.attendance_service import AttendanceService
    hr_rec, hr_warning = AttendanceService.sync_workshop_event(request.user, event_type, lat, lng)
    return _json_ok(id=rec.id, event_type=rec.event_type,
                    occurred_at=rec.occurred_at.isoformat(),
                    hr_recorded=hr_rec is not None, hr_warning=hr_warning)
