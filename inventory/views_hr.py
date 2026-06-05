"""HR Workspace — Pillar 1 mirror of the Tech Workspace.

HR & admins see a live attendance roster pulled from AttendanceCheckIn,
filterable by date, employee, and event type, with a Google Maps link
generated from the stored lat/lng for each row (payroll spot-check).
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import AttendanceCheckIn, EmployeeProfile
from .views import role_required, tenant_required


@login_required(login_url='/login/')
@tenant_required
@role_required('hr', 'admin', 'manager')
def hr_workspace(request):
    """List today's (or any selected day's) check-ins with map links."""
    # ── Filters ────────────────────────────────────────────────────────────
    today = timezone.now().date()
    date_str = request.GET.get('date') or ''
    selected_date = parse_date(date_str) or today

    event_type = (request.GET.get('event') or '').strip()
    employee_id = (request.GET.get('employee') or '').strip()

    qs = (AttendanceCheckIn.objects
          .filter(occurred_at__date=selected_date)
          .select_related('employee__user', 'employee__branch')
          .order_by('-occurred_at'))

    if event_type in {'in', 'out'}:
        qs = qs.filter(event_type=event_type)
    if employee_id.isdigit():
        qs = qs.filter(employee_id=int(employee_id))

    # ── Roster snapshot (top of page) ─────────────────────────────────────
    employees = (EmployeeProfile.objects
                 .select_related('user', 'branch')
                 .order_by('user__first_name', 'user__username'))

    # Today's headline counts
    today_counts = (AttendanceCheckIn.objects
                    .filter(occurred_at__date=selected_date)
                    .values('event_type')
                    .annotate(n=Count('id')))
    head = {row['event_type']: row['n'] for row in today_counts}

    # Currently "in" today = distinct employees with last event_type='in'
    last_per_employee = {}
    for rec in (AttendanceCheckIn.objects
                .filter(occurred_at__date=selected_date)
                .order_by('employee_id', '-occurred_at')):
        last_per_employee.setdefault(rec.employee_id, rec)
    currently_in = sum(1 for r in last_per_employee.values() if r.event_type == 'in')

    return render(request, 'inventory/hr_workspace.html', {
        'records': qs[:300],
        'employees': employees,
        'selected_date': selected_date,
        'today': today,
        'event_type': event_type,
        'employee_id': employee_id,
        'head': {
            'in':   head.get('in', 0),
            'out':  head.get('out', 0),
            'in_now': currently_in,
            'total_employees': employees.count(),
        },
    })
