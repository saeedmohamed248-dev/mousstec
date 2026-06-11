"""
Service: Smart Attendance — Geofencing + Face Verification + Lateness Calculation

Responsibilities:
- Validate employee geolocation against company geofence (Haversine)
- Process clock-in / clock-out events
- Calculate late minutes by comparing to assigned shift
- Compute worked hours and overtime
- Mark absent employees at end of day
"""

import logging
from datetime import datetime, timedelta, date
from decimal import Decimal
from math import radians, sin, cos, sqrt, atan2

from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')


class AttendanceService:
    """All attendance operations go through here."""

    # ------------------------------------------------------------------
    # Geofencing — Haversine distance validation
    # ------------------------------------------------------------------
    @staticmethod
    def validate_geolocation(latitude, longitude):
        """
        Check if given coordinates are within the company geofence.
        Returns (is_within, distance_meters).
        Uses the Haversine formula for great-circle distance.
        """
        from hr.models import HRSettings

        settings = HRSettings.get_settings()

        lat1 = radians(float(settings.geofence_latitude))
        lon1 = radians(float(settings.geofence_longitude))
        lat2 = radians(float(latitude))
        lon2 = radians(float(longitude))

        dlat = lat2 - lat1
        dlon = lon2 - lon1

        a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        earth_radius_meters = 6_371_000

        distance = earth_radius_meters * c
        is_within = distance <= settings.geofence_radius_meters

        return is_within, round(distance, 1)

    # ------------------------------------------------------------------
    # Clock In — register arrival
    # ------------------------------------------------------------------
    @staticmethod
    def clock_in(employee, latitude=None, longitude=None, face_verified=False):
        """
        Register employee clock-in.
        - Validates geolocation if provided
        - Records face verification flag
        - Calculates lateness compared to shift
        Returns the AttendanceRecord.
        Raises ValidationError if geolocation is outside range.
        """
        from hr.models import AttendanceRecord, HRSettings, EmployeeShiftAssignment

        now = timezone.now()
        today = now.date()
        hr_settings = HRSettings.get_settings()

        # Check if already clocked in today
        existing = AttendanceRecord.objects.filter(
            employee=employee, date=today
        ).first()
        if existing and existing.clock_in:
            raise ValidationError("تم تسجيل حضورك اليوم بالفعل.")

        # --- Enforce face verification if required ---
        if hr_settings.require_face_verification and not face_verified:
            raise ValidationError("بصمة الوجه مطلوبة لتسجيل الحضور. يرجى تفعيل الكاميرا والمحاولة مرة أخرى.")

        # --- Enforce location if required ---
        if hr_settings.require_location and (latitude is None or longitude is None):
            raise ValidationError("تحديد الموقع الجغرافي مطلوب لتسجيل الحضور. يرجى تفعيل GPS والمحاولة مرة أخرى.")

        # --- Geolocation validation ---
        location_verified = False
        if latitude is not None and longitude is not None:
            is_within, distance = AttendanceService.validate_geolocation(latitude, longitude)
            if not is_within:
                raise ValidationError(
                    f"أنت خارج نطاق الشركة ({distance:.0f} متر بعيد). "
                    f"النطاق المسموح: {hr_settings.geofence_radius_meters} متر."
                )
            location_verified = True

        # --- Find assigned shift ---
        day_map = {
            5: 'sat', 6: 'sun', 0: 'mon', 1: 'tue',
            2: 'wed', 3: 'thu', 4: 'fri',
        }
        today_day = day_map.get(today.weekday(), '')

        from django.db.models import Q
        assignment = EmployeeShiftAssignment.objects.filter(
            Q(effective_to__isnull=True) | Q(effective_to__gte=today),
            employee=employee,
            shift__is_active=True,
            effective_from__lte=today,
        ).select_related('shift').order_by('-effective_from').first()

        shift = assignment.shift if assignment else None

        # --- Calculate lateness ---
        late_minutes = 0
        status = 'present'

        if shift and today_day in (shift.days_of_week or []):
            shift_start_dt = timezone.make_aware(
                datetime.combine(today, shift.start_time),
                timezone.get_current_timezone(),
            )
            grace_deadline = shift_start_dt + timedelta(minutes=hr_settings.grace_minutes)

            if now > grace_deadline:
                late_minutes = int((now - shift_start_dt).total_seconds() / 60)
                status = 'late'

        # --- Create or update record ---
        with transaction.atomic():
            record, created = AttendanceRecord.objects.update_or_create(
                employee=employee,
                date=today,
                defaults={
                    'clock_in': now,
                    'face_verified': face_verified,
                    'check_in_latitude': latitude,
                    'check_in_longitude': longitude,
                    'location_verified': location_verified,
                    'late_minutes': late_minutes,
                    'status': status,
                    'shift': shift,
                },
            )

        logger.info(
            "[ATTENDANCE] Clock-in: %s at %s (status=%s, late=%s min, geo=%s, face=%s)",
            employee, now.strftime('%H:%M'), status, late_minutes,
            location_verified, face_verified,
        )

        return record

    # ------------------------------------------------------------------
    # Clock Out — register departure
    # ------------------------------------------------------------------
    @staticmethod
    def clock_out(employee, latitude=None, longitude=None):
        """
        Register employee clock-out.
        Calculates total worked hours and overtime.
        Returns the updated AttendanceRecord.
        """
        from hr.models import AttendanceRecord

        now = timezone.now()
        today = now.date()

        record = AttendanceRecord.objects.filter(
            employee=employee, date=today
        ).first()

        if not record or not record.clock_in:
            raise ValidationError("لم يتم تسجيل حضورك اليوم. سجّل حضورك أولاً.")
        if record.clock_out:
            raise ValidationError("تم تسجيل انصرافك اليوم بالفعل.")

        # --- Geolocation validation for clock-out (optional) ---
        if latitude is not None and longitude is not None:
            is_within, distance = AttendanceService.validate_geolocation(latitude, longitude)
            # Log but don't block clock-out
            if not is_within:
                record.notes += f"\n[تحذير] تسجيل الانصراف من خارج النطاق ({distance:.0f}م)"

        # --- Calculate worked hours ---
        duration = now - record.clock_in
        worked_hours = Decimal(str(max(duration.total_seconds() / 3600.0, 0))).quantize(Decimal('0.01'))

        # --- Calculate overtime ---
        overtime = Decimal('0.00')
        if record.shift:
            shift_start = datetime.combine(today, record.shift.start_time)
            shift_end = datetime.combine(today, record.shift.end_time)
            # Handle overnight shifts
            if shift_end <= shift_start:
                shift_end += timedelta(days=1)
            expected_hours = Decimal(str((shift_end - shift_start).total_seconds() / 3600.0))
            if worked_hours > expected_hours:
                overtime = (worked_hours - expected_hours).quantize(Decimal('0.01'))

        with transaction.atomic():
            record.clock_out = now
            record.check_out_latitude = latitude
            record.check_out_longitude = longitude
            record.worked_hours = worked_hours
            record.overtime_hours = overtime
            record.save(update_fields=[
                'clock_out', 'check_out_latitude', 'check_out_longitude',
                'worked_hours', 'overtime_hours', 'notes', 'updated_at',
            ])

        logger.info(
            "[ATTENDANCE] Clock-out: %s at %s (worked=%sh, overtime=%sh)",
            employee, now.strftime('%H:%M'), worked_hours, overtime,
        )

        return record

    # ------------------------------------------------------------------
    # Mark Absent — end-of-day batch (for Celery or manual trigger)
    # ------------------------------------------------------------------
    @staticmethod
    def mark_absent_employees(target_date=None):
        """
        Mark all employees without a clock-in record as 'absent' for the given date.
        Respects approved leave requests (marks as 'excused' instead).
        Returns count of marked records.
        """
        from hr.models import Employee, AttendanceRecord, LeaveRequest, EmployeeShiftAssignment
        from django.db.models import Q

        if target_date is None:
            target_date = timezone.now().date()

        day_map = {
            5: 'sat', 6: 'sun', 0: 'mon', 1: 'tue',
            2: 'wed', 3: 'thu', 4: 'fri',
        }
        today_day = day_map.get(target_date.weekday(), '')

        # Get all active employees
        active_employees = Employee.objects.filter(is_active=True)

        # Get employees who have a record for today
        recorded_ids = AttendanceRecord.objects.filter(
            date=target_date
        ).values_list('employee_id', flat=True)

        # Get approved leaves covering today
        on_leave_ids = LeaveRequest.objects.filter(
            status='approved',
            from_date__lte=target_date,
            to_date__gte=target_date,
        ).values_list('employee_id', flat=True)

        absent_count = 0
        records_to_create = []

        for emp in active_employees.exclude(pk__in=recorded_ids):
            # Check if employee has a shift for today
            has_shift = EmployeeShiftAssignment.objects.filter(
                Q(effective_to__isnull=True) | Q(effective_to__gte=target_date),
                employee=emp,
                shift__is_active=True,
                effective_from__lte=target_date,
                shift__days_of_week__contains=today_day,
            ).exists()

            if not has_shift:
                continue  # No shift today — skip (could be a day off)

            if emp.pk in on_leave_ids:
                status = 'excused'
            else:
                status = 'absent'
                absent_count += 1

            records_to_create.append(AttendanceRecord(
                employee=emp,
                date=target_date,
                status=status,
            ))

        if records_to_create:
            AttendanceRecord.objects.bulk_create(records_to_create, ignore_conflicts=True)

        logger.info(
            "[ATTENDANCE] Marked %s employees absent for %s (total processed: %s)",
            absent_count, target_date, len(records_to_create),
        )

        return absent_count

    # ------------------------------------------------------------------
    # Monthly Summary (used by PayrollService)
    # ------------------------------------------------------------------
    @staticmethod
    def get_monthly_summary(employee, month, year):
        """
        Summarize attendance for a given month.
        Returns dict with: present, late, absent, excused, total_late_minutes, total_worked_hours.
        """
        from hr.models import AttendanceRecord
        from django.db.models import Sum, Count, Q

        records = AttendanceRecord.objects.filter(
            employee=employee,
            date__month=month,
            date__year=year,
        )

        summary = records.aggregate(
            days_present=Count('id', filter=Q(status='present')),
            days_late=Count('id', filter=Q(status='late')),
            days_absent=Count('id', filter=Q(status='absent')),
            days_excused=Count('id', filter=Q(status__in=['excused', 'holiday'])),
            total_late_minutes=Sum('late_minutes'),
            total_worked_hours=Sum('worked_hours'),
            total_overtime_hours=Sum('overtime_hours'),
        )

        # None → 0 cleanup
        for key in summary:
            if summary[key] is None:
                summary[key] = 0

        # "late" also counts as "present" for payroll — they showed up
        summary['days_present'] += summary['days_late']

        return summary
