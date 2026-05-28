"""
Admin: HR Module — Full admin interface for managing HR operations.
"""

from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.contrib import messages

from hr.models import (
    HRSettings, Employee, WorkShift, EmployeeShiftAssignment,
    AttendanceRecord, LeaveRequest, Advance, AdvanceInstallment,
    PayrollRun, PayrollEntry, DesignSubmission,
)


# =====================================================================
# 1. HR Settings — Singleton
# =====================================================================

@admin.register(HRSettings)
class HRSettingsAdmin(admin.ModelAdmin):
    list_display = (
        'geofence_radius_meters', 'grace_minutes',
        'absence_deduction_days', 'working_days_per_month',
        'max_advance_percentage', 'max_installments',
    )
    fieldsets = (
        (_('النطاق الجغرافي (Geofencing)'), {
            'fields': ('geofence_latitude', 'geofence_longitude', 'geofence_radius_meters'),
        }),
        (_('سياسات الحضور والخصم'), {
            'fields': (
                'grace_minutes', 'late_deduction_per_minute',
                'late_deduction_percentage', 'absence_deduction_days',
                'working_days_per_month',
            ),
        }),
        (_('سياسات السلف'), {
            'fields': ('max_advance_percentage', 'max_installments'),
        }),
    )

    def has_add_permission(self, request):
        # Singleton — only one record
        if HRSettings.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


# =====================================================================
# 2. Employee
# =====================================================================

class ShiftAssignmentInline(admin.TabularInline):
    model = EmployeeShiftAssignment
    extra = 0
    fields = ('shift', 'effective_from', 'effective_to')


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        'employee_id', 'get_name', 'department', 'job_title',
        'base_salary', 'is_hr_manager', 'auto_approve_designs', 'is_active',
    )
    list_filter = ('department', 'contract_type', 'is_hr_manager', 'is_active', 'auto_approve_designs')
    search_fields = ('employee_id', 'user__first_name', 'user__last_name', 'user__username', 'job_title')
    list_editable = ('is_active',)
    raw_id_fields = ('user', 'supervisor')
    inlines = [ShiftAssignmentInline]

    fieldsets = (
        (_('الحساب'), {
            'fields': ('user', 'employee_id'),
        }),
        (_('البيانات الوظيفية'), {
            'fields': ('department', 'job_title', 'contract_type', 'hire_date'),
        }),
        (_('الهيكل الإداري'), {
            'fields': ('supervisor', 'is_hr_manager'),
        }),
        (_('المالي'), {
            'fields': ('base_salary', 'daily_rate'),
        }),
        (_('بصمة الوجه'), {
            'fields': ('face_photo', 'face_encoding'),
            'classes': ('collapse',),
        }),
        (_('تفويض التصميم'), {
            'fields': ('auto_approve_designs',),
            'description': _('تفعيل هذا الخيار يعني أن تصميمات الموظف تُعتمد فوراً بدون مراجعة المدير.'),
        }),
        (_('أخرى'), {
            'fields': ('is_active', 'notes'),
        }),
    )

    @admin.display(description=_("الاسم"), ordering='user__first_name')
    def get_name(self, obj):
        return obj.user.get_full_name() or obj.user.username


# =====================================================================
# 3. Work Shifts
# =====================================================================

@admin.register(WorkShift)
class WorkShiftAdmin(admin.ModelAdmin):
    list_display = ('name', 'start_time', 'end_time', 'get_days', 'is_active')
    list_filter = ('is_active',)
    list_editable = ('is_active',)

    @admin.display(description=_("أيام العمل"))
    def get_days(self, obj):
        day_names = {
            'sat': 'سبت', 'sun': 'أحد', 'mon': 'اثنين', 'tue': 'ثلاثاء',
            'wed': 'أربعاء', 'thu': 'خميس', 'fri': 'جمعة',
        }
        if obj.days_of_week:
            return ' · '.join(day_names.get(d, d) for d in obj.days_of_week)
        return '—'


# =====================================================================
# 4. Attendance Records
# =====================================================================

@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = (
        'employee', 'date', 'status_badge', 'clock_in', 'clock_out',
        'late_minutes', 'worked_hours', 'face_verified', 'location_verified',
    )
    list_filter = ('status', 'date', 'face_verified', 'location_verified')
    search_fields = ('employee__user__first_name', 'employee__user__last_name', 'employee__employee_id')
    date_hierarchy = 'date'
    list_per_page = 50

    @admin.display(description=_("الحالة"))
    def status_badge(self, obj):
        colors = {
            'present': '#28a745', 'late': '#ffc107',
            'absent': '#dc3545', 'excused': '#17a2b8', 'holiday': '#6c757d',
        }
        color = colors.get(obj.status, '#6c757d')
        return format_html(
            '<span style="background:{}; color:white; padding:3px 10px; border-radius:12px; font-size:12px;">{}</span>',
            color, obj.get_status_display(),
        )

    actions = ['action_mark_absent_today']

    @admin.action(description=_("تسجيل الغياب لليوم (للموظفين بدون حضور)"))
    def action_mark_absent_today(self, request, queryset):
        from hr.services.attendance_service import AttendanceService
        count = AttendanceService.mark_absent_employees()
        self.message_user(request, f"تم تسجيل {count} غياب.", messages.SUCCESS)


# =====================================================================
# 5. Leave Requests
# =====================================================================

@admin.register(LeaveRequest)
class LeaveRequestAdmin(admin.ModelAdmin):
    list_display = ('employee', 'leave_type', 'from_date', 'to_date', 'total_days', 'status', 'reviewed_by')
    list_filter = ('status', 'leave_type')
    search_fields = ('employee__user__first_name', 'employee__user__last_name')
    date_hierarchy = 'from_date'
    raw_id_fields = ('employee', 'reviewed_by')

    actions = ['approve_leaves', 'reject_leaves']

    @admin.action(description=_("الموافقة على الإجازات المحددة"))
    def approve_leaves(self, request, queryset):
        hr_employee = self._get_hr_employee(request)
        if not hr_employee:
            self.message_user(request, "ليس لديك ملف موظف HR.", messages.ERROR)
            return
        count = 0
        for leave in queryset.filter(status='pending'):
            leave.status = 'approved'
            leave.reviewed_by = hr_employee
            leave.reviewed_at = timezone.now()
            leave.save()
            count += 1
        self.message_user(request, f"تمت الموافقة على {count} إجازة.", messages.SUCCESS)

    @admin.action(description=_("رفض الإجازات المحددة"))
    def reject_leaves(self, request, queryset):
        hr_employee = self._get_hr_employee(request)
        if not hr_employee:
            self.message_user(request, "ليس لديك ملف موظف HR.", messages.ERROR)
            return
        count = queryset.filter(status='pending').update(
            status='rejected', reviewed_by=hr_employee, reviewed_at=timezone.now(),
        )
        self.message_user(request, f"تم رفض {count} إجازة.", messages.WARNING)

    @staticmethod
    def _get_hr_employee(request):
        from hr.models import Employee
        try:
            return Employee.objects.get(user=request.user)
        except Employee.DoesNotExist:
            return None


# =====================================================================
# 6. Advances & Installments
# =====================================================================

class AdvanceInstallmentInline(admin.TabularInline):
    model = AdvanceInstallment
    extra = 0
    readonly_fields = ('installment_number', 'amount', 'due_month', 'status', 'deducted_in_payroll')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Advance)
class AdvanceAdmin(admin.ModelAdmin):
    list_display = (
        'employee', 'amount', 'installments_count', 'remaining_amount',
        'status', 'approved_by', 'requested_at',
    )
    list_filter = ('status',)
    search_fields = ('employee__user__first_name', 'employee__user__last_name', 'employee__employee_id')
    raw_id_fields = ('employee', 'approved_by')
    readonly_fields = ('remaining_amount', 'approved_at', 'requested_at')
    inlines = [AdvanceInstallmentInline]

    actions = ['approve_advances', 'reject_advances']

    @admin.action(description=_("الموافقة على السلف المحددة"))
    def approve_advances(self, request, queryset):
        from hr.services.advance_service import AdvanceService
        hr_employee = LeaveRequestAdmin._get_hr_employee(request)
        if not hr_employee:
            self.message_user(request, "ليس لديك ملف موظف HR.", messages.ERROR)
            return
        count = 0
        errors = []
        for adv in queryset.filter(status='pending'):
            try:
                AdvanceService.approve_advance(adv.pk, hr_employee)
                count += 1
            except Exception as e:
                errors.append(f"سلفة #{adv.pk}: {e}")
        msg = f"تمت الموافقة على {count} سلفة."
        if errors:
            msg += f" أخطاء: {'; '.join(errors)}"
        self.message_user(request, msg, messages.SUCCESS if not errors else messages.WARNING)

    @admin.action(description=_("رفض السلف المحددة"))
    def reject_advances(self, request, queryset):
        from hr.services.advance_service import AdvanceService
        hr_employee = LeaveRequestAdmin._get_hr_employee(request)
        if not hr_employee:
            self.message_user(request, "ليس لديك ملف موظف HR.", messages.ERROR)
            return
        count = 0
        for adv in queryset.filter(status='pending'):
            try:
                AdvanceService.reject_advance(adv.pk, hr_employee, 'رفض جماعي من الأدمن')
                count += 1
            except Exception:
                pass
        self.message_user(request, f"تم رفض {count} سلفة.", messages.WARNING)


# =====================================================================
# 7. Payroll
# =====================================================================

class PayrollEntryInline(admin.TabularInline):
    model = PayrollEntry
    extra = 0
    readonly_fields = (
        'employee', 'base_salary', 'days_present', 'days_absent', 'days_late',
        'days_excused', 'total_late_minutes', 'late_deduction', 'absence_deduction',
        'advance_deduction', 'other_deductions', 'bonuses', 'overtime_pay',
        'total_deductions', 'total_additions', 'net_salary',
    )
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PayrollRun)
class PayrollRunAdmin(admin.ModelAdmin):
    list_display = (
        'get_period', 'status', 'total_employees',
        'total_gross', 'total_deductions', 'total_net',
        'created_by', 'paid_at',
    )
    list_filter = ('status', 'period_year')
    readonly_fields = ('total_gross', 'total_deductions', 'total_net', 'total_employees', 'paid_at')
    inlines = [PayrollEntryInline]

    @admin.display(description=_("الفترة"))
    def get_period(self, obj):
        return f"{obj.period_month}/{obj.period_year}"

    actions = ['generate_payroll_action', 'approve_payroll_action', 'disburse_payroll_action']

    @admin.action(description=_("إنشاء / إعادة حساب الرواتب"))
    def generate_payroll_action(self, request, queryset):
        from hr.services.payroll_service import PayrollService
        for run in queryset.filter(status='draft'):
            try:
                PayrollService.generate_payroll(
                    run.period_month, run.period_year, created_by=request.user,
                )
                self.message_user(
                    request,
                    f"تم حساب رواتب {run.period_month}/{run.period_year}.",
                    messages.SUCCESS,
                )
            except Exception as e:
                self.message_user(request, f"خطأ: {e}", messages.ERROR)

    @admin.action(description=_("اعتماد الرواتب"))
    def approve_payroll_action(self, request, queryset):
        from hr.services.payroll_service import PayrollService
        for run in queryset.filter(status='calculated'):
            try:
                PayrollService.approve_payroll(run.pk, approved_by=request.user)
                self.message_user(request, f"تم اعتماد رواتب {run.period_month}/{run.period_year}.", messages.SUCCESS)
            except Exception as e:
                self.message_user(request, f"خطأ: {e}", messages.ERROR)

    @admin.action(description=_("صرف الرواتب من الخزينة"))
    def disburse_payroll_action(self, request, queryset):
        from hr.services.payroll_service import PayrollService
        for run in queryset.filter(status='approved'):
            try:
                PayrollService.disburse_payroll(run.pk)
                self.message_user(request, f"تم صرف رواتب {run.period_month}/{run.period_year}.", messages.SUCCESS)
            except Exception as e:
                self.message_user(request, f"خطأ: {e}", messages.ERROR)


# =====================================================================
# 8. Design Submissions
# =====================================================================

@admin.register(DesignSubmission)
class DesignSubmissionAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'designer', 'execution_type', 'status_badge',
        'auto_approved', 'reviewer', 'created_at',
    )
    list_filter = ('status', 'execution_type', 'auto_approved')
    search_fields = ('title', 'designer__user__first_name', 'designer__user__last_name')
    raw_id_fields = ('designer', 'reviewer')
    readonly_fields = ('auto_approved', 'reviewed_at', 'created_at', 'updated_at')
    date_hierarchy = 'created_at'

    @admin.display(description=_("الحالة"))
    def status_badge(self, obj):
        colors = {
            'pending': '#ffc107', 'approved': '#28a745',
            'rejected': '#dc3545', 'revision_requested': '#17a2b8',
        }
        color = colors.get(obj.status, '#6c757d')
        return format_html(
            '<span style="background:{}; color:white; padding:3px 10px; border-radius:12px; font-size:12px;">{}</span>',
            color, obj.get_status_display(),
        )

    actions = ['approve_designs', 'reject_designs']

    @admin.action(description=_("اعتماد التصاميم المحددة"))
    def approve_designs(self, request, queryset):
        from hr.services.design_workflow_service import DesignWorkflowService
        hr_employee = LeaveRequestAdmin._get_hr_employee(request)
        if not hr_employee:
            self.message_user(request, "ليس لديك ملف موظف HR.", messages.ERROR)
            return
        count = 0
        for sub in queryset.filter(status='pending'):
            try:
                DesignWorkflowService.approve_design(sub.pk, hr_employee, 'اعتماد جماعي من الأدمن')
                count += 1
            except Exception as e:
                self.message_user(request, f"خطأ في تصميم #{sub.pk}: {e}", messages.ERROR)
        if count:
            self.message_user(request, f"تم اعتماد {count} تصميم.", messages.SUCCESS)

    @admin.action(description=_("رفض التصاميم المحددة"))
    def reject_designs(self, request, queryset):
        from hr.services.design_workflow_service import DesignWorkflowService
        hr_employee = LeaveRequestAdmin._get_hr_employee(request)
        if not hr_employee:
            self.message_user(request, "ليس لديك ملف موظف HR.", messages.ERROR)
            return
        count = 0
        for sub in queryset.filter(status='pending'):
            try:
                DesignWorkflowService.reject_design(sub.pk, hr_employee, 'رفض جماعي من الأدمن')
                count += 1
            except Exception:
                pass
        if count:
            self.message_user(request, f"تم رفض {count} تصميم.", messages.WARNING)
