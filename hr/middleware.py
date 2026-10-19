"""
🚪 Attendance Gate — force tech / engineer roles to clock in before
they can interact with the workshop UI.

How it works:
    1. Runs only on tenant schemas (public/admin paths are untouched).
    2. Lets unauthenticated requests through — auth middleware handles login.
    3. Lets non-tech/engineer roles through (cashier, sales, admin, etc.).
    4. For tech/engineer with an active HR employee record: if there's no
       open `AttendanceRecord` (today, or last night's overnight shift),
       redirect to the attendance page (preserving the original URL via
       `?next=`). No HR record = nothing to clock in with = not gated.

Bypass list:
    The attendance page itself, its APIs, static assets, logout, and the
    auth-redirect are never gated — otherwise the user would be trapped
    in a redirect loop.
"""
from __future__ import annotations

import re
from django.db import connection
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import urlencode

_GATED_ROLES = {'tech', 'engineer'}

# Anything that would either be needed to *perform* the clock-in, or that
# would break the system if blocked.
# 🐛 [FIX]: كانت «hr/api/» + (/|$) — يعني /hr/api//… بس، فـ /hr/api/clock-in/
#    نفسه كان بيتقفل عليه بالبوابة: الفني يتحوّل لصفحة الحضور ويدوس «تسجيل حضور»
#    والطلب يرجع redirect لنفس الصفحة — عمره ما يعرف يسجّل حضور ولا يدخل السيستم.
_BYPASS = re.compile(
    r'^/(hr/attendance|hr/api|static|media|login|logout|account|auth|'
    r'connect|sw\.js|manifest\.json|offline|i18n|system/switch-branch)(/|$)'
)


class AttendanceGateMiddleware:
    """Tech & Engineer roles cannot use the workshop until they clock in."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 1. Public schema → never gated (no tenant context yet).
        if connection.schema_name == 'public':
            return self.get_response(request)

        # 2. Anonymous → auth middleware will redirect to /login/.
        if not getattr(request, 'user', None) or not request.user.is_authenticated:
            return self.get_response(request)

        # 3. Always allow the bypass list (the gate itself + statics).
        if _BYPASS.match(request.path_info):
            return self.get_response(request)

        # 4. Resolve the EmployeeProfile (inventory app — soft import to
        #    avoid circular dependencies at startup).
        try:
            profile = getattr(request.user, 'employee_profile', None)
        except Exception:
            profile = None
        if profile is None or profile.role not in _GATED_ROLES:
            return self.get_response(request)

        # Superuser is never gated — operator override.
        if request.user.is_superuser:
            return self.get_response(request)

        # 5. Check for an open attendance record today.
        if self._has_open_attendance(request.user):
            return self.get_response(request)

        # 6. Gate fires — redirect to the attendance page with ?next=
        attendance_url = reverse('hr:attendance_page')
        # ⚠️ لازم urlencode: الـ full path ساعات بيبقى فيه query string بتاعه،
        # ولو لزقناه خام الـ & بتاعه بيقطع الـ next ويضيع وجهة المستخدم.
        nxt = urlencode({'next': request.get_full_path(), 'gated': '1'})
        return redirect(f'{attendance_url}?{nxt}')

    @staticmethod
    def _has_open_attendance(user) -> bool:
        """True لو الموظف يقدر يشتغل: عنده سجل حضور مفتوح النهارده (أو وردية
        ليلية من امبارح لسه شغّالة) — أو أصلاً مالوش ملف HR يسجّل بيه حضور.

        🐛 [FIX]: فني/مهندس مضاف من «الموظفون والصلاحيات» (من غير ملف HR) كان
        بيتحوّل لصفحة الحضور، والصفحة ترجّعه للرئيسية لأنه مالوش ملف،
        والرئيسية ترجّعه للبوابة… «redirected too many times» والموظف مقفول
        عليه السيستم كله. البوابة بتقفل بس على اللي يقدر يسجّل حضور فعلاً.
        وأي خطأ غير متوقع بيعدّي (مانقفلش السيستم على حد بسبب عطل).
        """
        try:
            from hr.models import Employee
            from hr.services.attendance_service import AttendanceService
            emp = Employee.objects.filter(user=user, is_active=True).first()
            if emp is None:
                return True
            return AttendanceService.open_record(emp) is not None
        except Exception:
            return True
