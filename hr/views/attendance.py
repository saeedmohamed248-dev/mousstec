"""🕘 شاشات الموارد البشرية — الحضور والانصراف وبصمة الوجه."""

import json
import logging
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.contrib import messages as django_messages
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from ._common import _err_text, _json_response

logger = logging.getLogger('mouss_tec_core')


# =====================================================================
# 1. Attendance APIs (Mobile / PWA)
# =====================================================================

@login_required(login_url='/secure-portal/')
@require_POST
def api_clock_in(request):
    """
    POST /hr/api/clock-in/
    Body: { "latitude": 30.044, "longitude": 31.235, "face_verified": true }
    """
    from hr.models import Employee
    from hr.services.attendance_service import AttendanceService

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return _json_response({"error": "بيانات غير صالحة."}, 400)

    latitude = data.get('latitude')
    longitude = data.get('longitude')
    face_verified = data.get('face_verified', False)

    try:
        record = AttendanceService.clock_in(
            employee=employee,
            latitude=Decimal(str(latitude)) if latitude else None,
            longitude=Decimal(str(longitude)) if longitude else None,
            face_verified=bool(face_verified),
        )
        return _json_response({
            "success": True,
            "message": "تم تسجيل حضورك بنجاح.",
            "data": {
                "status": record.get_status_display(),
                # 🐛 [FIX]: كان بيرجع وقت UTC (٦:٤١ بدل ٩:٤١ بتوقيت المحل)
                "clock_in": timezone.localtime(record.clock_in).strftime('%H:%M:%S'),
                "late_minutes": record.late_minutes,
                "face_verified": record.face_verified,
                "location_verified": record.location_verified,
            },
        })
    except Exception as e:
        return _json_response({"error": _err_text(e)}, 400)


@login_required(login_url='/secure-portal/')
@require_POST
def api_clock_out(request):
    """
    POST /hr/api/clock-out/
    Body: { "latitude": 30.044, "longitude": 31.235 }
    """
    from hr.models import Employee
    from hr.services.attendance_service import AttendanceService

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return _json_response({"error": "بيانات غير صالحة."}, 400)

    latitude = data.get('latitude')
    longitude = data.get('longitude')

    try:
        record = AttendanceService.clock_out(
            employee=employee,
            latitude=Decimal(str(latitude)) if latitude else None,
            longitude=Decimal(str(longitude)) if longitude else None,
        )
        return _json_response({
            "success": True,
            "message": "تم تسجيل انصرافك بنجاح.",
            "data": {
                "clock_out": timezone.localtime(record.clock_out).strftime('%H:%M:%S'),
                "worked_hours": str(record.worked_hours),
                "overtime_hours": str(record.overtime_hours),
            },
        })
    except Exception as e:
        return _json_response({"error": _err_text(e)}, 400)


@login_required(login_url='/secure-portal/')
@require_GET
def api_my_attendance(request):
    """
    GET /hr/api/my-attendance/?month=6&year=2026
    Returns the logged-in employee's attendance summary for the month.
    """
    from hr.models import Employee, AttendanceRecord
    from hr.services.attendance_service import AttendanceService

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    now = timezone.now()
    try:
        month = int(request.GET.get('month', now.month))
        year = int(request.GET.get('year', now.year))
        if not (1 <= month <= 12) or not (2000 <= year <= 2100):
            raise ValueError
    except (ValueError, TypeError):
        return _json_response({"error": "شهر أو سنة غير صالحة."}, 400)

    summary = AttendanceService.get_monthly_summary(employee, month, year)

    records = AttendanceRecord.objects.filter(
        employee=employee, date__month=month, date__year=year,
    ).order_by('date').values(
        'date', 'status', 'clock_in', 'clock_out', 'late_minutes', 'worked_hours',
    )

    return _json_response({
        "employee": str(employee),
        "period": f"{month}/{year}",
        "summary": summary,
        "records": list(records),
    })


# =====================================================================
# 1b. Attendance Page (PWA) + Face Registration
# =====================================================================

@login_required(login_url='/secure-portal/')
def attendance_page(request):
    """
    GET /hr/attendance/
    صفحة الحضور والانصراف — PWA-ready.
    تعمل مع بصمة الوجه + GPS + بصمة الإصبع (WebAuthn).
    """
    from hr.models import Employee, AttendanceRecord, HRSettings

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        django_messages.error(request, "ليس لديك ملف موظف مفعّل.")
        return redirect('/')

    hr_settings = HRSettings.get_settings()
    # 🐛 [FIX]: تاريخ محلي مش UTC، والوردية الليلية بعد نص الليل بتشوف سجلها
    #    المفتوح (زرار الانصراف) بدل زرار حضور جديد.
    from hr.services.attendance_service import AttendanceService
    today = timezone.localdate()

    today_record = AttendanceService.open_record(employee) or AttendanceRecord.objects.filter(
        employee=employee, date=today,
    ).first()

    # آخر 7 أيام
    from datetime import timedelta
    week_ago = today - timedelta(days=7)
    recent_records = AttendanceRecord.objects.filter(
        employee=employee, date__gte=week_ago,
    ).order_by('-date')

    # هل الوجه مسجّل؟
    has_face = bool(employee.face_encoding)

    return render(request, 'hr/attendance.html', {
        'employee': employee,
        'hr_settings': hr_settings,
        'today_record': today_record,
        'recent_records': recent_records,
        'has_face': has_face,
        'require_face': hr_settings.require_face_verification,
        'require_location': hr_settings.require_location,
        'face_threshold': float(hr_settings.face_match_threshold),
        'geofence_lat': float(hr_settings.geofence_latitude),
        'geofence_lng': float(hr_settings.geofence_longitude),
        'geofence_radius': hr_settings.geofence_radius_meters,
    })


@login_required(login_url='/secure-portal/')
@require_POST
def api_register_face(request):
    """
    POST /hr/api/face/register/
    Body: { "face_encoding": [...128 floats...] }
    يسجّل بصمة وجه الموظف (face descriptor من face-api.js).
    """
    from hr.models import Employee

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return _json_response({"error": "بيانات غير صالحة."}, 400)

    face_encoding = data.get('face_encoding')
    if not face_encoding or not isinstance(face_encoding, list) or len(face_encoding) < 64:
        return _json_response({"error": "بيانات بصمة الوجه غير صالحة."}, 400)

    # 🛡️ [SECURITY FIX]: Require password re-entry when OVERWRITING existing face encoding
    if employee.face_encoding:
        password = data.get('password', '')
        if not password or not request.user.check_password(password):
            return _json_response({"error": "يجب إدخال كلمة المرور لتحديث بصمة الوجه."}, 403)

    employee.face_encoding = face_encoding
    employee.save(update_fields=['face_encoding', 'updated_at'])

    logger.info("[FACE] Registered face encoding for %s (%d dims)", employee, len(face_encoding))

    return _json_response({
        "success": True,
        "message": "تم تسجيل بصمة الوجه بنجاح! يمكنك الآن استخدامها للحضور.",
    })


@login_required(login_url='/secure-portal/')
@require_GET
def api_get_face_descriptor(request):
    """
    GET /hr/api/face/descriptor/
    يرجع الـ face_encoding المسجل للموظف الحالي (للمطابقة في المتصفح).
    """
    from hr.models import Employee

    try:
        employee = Employee.objects.get(user=request.user, is_active=True)
    except Employee.DoesNotExist:
        return _json_response({"error": "ليس لديك ملف موظف مفعّل."}, 403)

    if not employee.face_encoding:
        return _json_response({"error": "لم يتم تسجيل بصمة الوجه بعد.", "registered": False}, 404)

    return _json_response({
        "registered": True,
        "face_encoding": employee.face_encoding,
    })


@login_required(login_url='/secure-portal/')
@require_GET
def api_attendance_settings(request):
    """
    GET /hr/api/attendance/settings/
    يرجع إعدادات الحضور — يستخدمها الـ frontend.
    """
    from hr.models import HRSettings

    hr_settings = HRSettings.get_settings()

    return _json_response({
        "require_face": hr_settings.require_face_verification,
        "require_location": hr_settings.require_location,
        "face_threshold": float(hr_settings.face_match_threshold),
        "geofence": {
            "latitude": float(hr_settings.geofence_latitude),
            "longitude": float(hr_settings.geofence_longitude),
            "radius_meters": hr_settings.geofence_radius_meters,
        },
        "grace_minutes": hr_settings.grace_minutes,
    })
