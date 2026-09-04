"""HR serializers — الموظفون، الحضور، الإجازات، السلف، الرواتب."""
from rest_framework import serializers

from hr.models import (
    Employee,
    AttendanceRecord,
    LeaveRequest,
    Advance,
    PayrollRun,
    PayrollEntry,
    WorkShift,
)


class EmployeeSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    username = serializers.CharField(source='user.username', read_only=True)

    class Meta:
        model = Employee
        fields = (
            'id', 'employee_id', 'full_name', 'username', 'department',
            'job_title', 'contract_type', 'hire_date', 'base_salary',
            'daily_rate', 'is_active',
        )

    def get_full_name(self, obj):
        if getattr(obj, 'user', None):
            return obj.user.get_full_name() or obj.user.username
        return obj.employee_id


class WorkShiftSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkShift
        fields = '__all__'


class AttendanceRecordSerializer(serializers.ModelSerializer):
    employee_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = AttendanceRecord
        fields = (
            'id', 'employee', 'employee_name', 'date', 'clock_in', 'clock_out',
            'status', 'status_display', 'late_minutes', 'worked_hours', 'overtime_hours',
        )

    def get_employee_name(self, obj):
        emp = getattr(obj, 'employee', None)
        if emp and getattr(emp, 'user', None):
            return emp.user.get_full_name() or emp.user.username
        return None


class LeaveRequestSerializer(serializers.ModelSerializer):
    employee_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    leave_type_display = serializers.CharField(source='get_leave_type_display', read_only=True)

    class Meta:
        model = LeaveRequest
        fields = (
            'id', 'employee', 'employee_name', 'leave_type', 'leave_type_display',
            'from_date', 'to_date', 'reason', 'status', 'status_display', 'created_at',
        )
        read_only_fields = ('status', 'created_at')

    def get_employee_name(self, obj):
        emp = getattr(obj, 'employee', None)
        if emp and getattr(emp, 'user', None):
            return emp.user.get_full_name() or emp.user.username
        return None


class AdvanceSerializer(serializers.ModelSerializer):
    employee_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Advance
        fields = (
            'id', 'employee', 'employee_name', 'amount', 'reason',
            'installments_count', 'status', 'status_display',
            'remaining_amount', 'requested_at',
        )
        read_only_fields = ('status', 'remaining_amount', 'requested_at')

    def get_employee_name(self, obj):
        emp = getattr(obj, 'employee', None)
        if emp and getattr(emp, 'user', None):
            return emp.user.get_full_name() or emp.user.username
        return None


class PayrollRunSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = PayrollRun
        fields = (
            'id', 'period_month', 'period_year', 'status', 'status_display',
            'total_gross', 'total_deductions', 'total_net', 'total_employees',
            'created_at', 'paid_at',
        )


class PayrollEntrySerializer(serializers.ModelSerializer):
    employee_name = serializers.SerializerMethodField()

    class Meta:
        model = PayrollEntry
        fields = (
            'id', 'payroll_run', 'employee', 'employee_name', 'base_salary',
            'days_present', 'days_absent', 'total_deductions', 'total_additions',
            'net_salary',
        )

    def get_employee_name(self, obj):
        emp = getattr(obj, 'employee', None)
        if emp and getattr(emp, 'user', None):
            return emp.user.get_full_name() or emp.user.username
        return None
