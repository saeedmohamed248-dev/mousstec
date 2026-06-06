"""
🚪 Attendance Gate — force tech / engineer roles to clock in before
they can interact with the workshop UI.

How it works:
    1. Runs only on tenant schemas (public/admin paths are untouched).
    2. Lets unauthenticated requests through — auth middleware handles login.
    3. Lets non-tech/engineer roles through (cashier, sales, admin, etc.).
    4. For tech/engineer: if there's no `AttendanceRecord` for today with
       `clock_in IS NOT NULL` AND `clock_out IS NULL`, redirect to the
       attendance page (preserving the original URL via `?next=`).

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
from django.utils import timezone

_GATED_ROLES = {'tech', 'engineer'}

# Anything that would either be needed to *perform* the clock-in, or that
# would break the system if blocked.
_BYPASS = re.compile(
    r'^/(hr/attendance|hr/api/|static|media|login|logout|account|auth|'
    r'connect|sw\.js|manifest\.json|offline|i18n)(/|$)'
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
        nxt = request.get_full_path()
        return redirect(f'{attendance_url}?next={nxt}&gated=1')

    @staticmethod
    def _has_open_attendance(user) -> bool:
        """True if there's an AttendanceRecord for today with clock_in set
        and clock_out still null. Falls back to the legacy `EmployeeShift`
        model for tenants that haven't migrated yet."""
        today = timezone.localdate()

        # Modern path: hr.AttendanceRecord
        try:
            from hr.models import AttendanceRecord, Employee
            emp = Employee.objects.filter(user=user).first()
            if emp is not None:
                return AttendanceRecord.objects.filter(
                    employee=emp,
                    date=today,
                    clock_in__isnull=False,
                    clock_out__isnull=True,
                ).exists()
        except Exception:
            pass

        # Legacy path: inventory.EmployeeShift
        try:
            from inventory.models import EmployeeShift, EmployeeProfile
            profile = EmployeeProfile.objects.filter(user=user).first()
            if profile is not None:
                return EmployeeShift.objects.filter(
                    employee=profile,
                    clock_in__date=today,
                    clock_out__isnull=True,
                ).exists()
        except Exception:
            pass

        return False
