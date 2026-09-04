"""HR views — الموظفون، الحضور، الإجازات، السلف، الرواتب."""
from django.db.models import Q
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from hr.models import (
    Employee,
    AttendanceRecord,
    LeaveRequest,
    Advance,
    PayrollRun,
    PayrollEntry,
)

from ..serializers import (
    EmployeeSerializer,
    AttendanceRecordSerializer,
    LeaveRequestSerializer,
    AdvanceSerializer,
    PayrollRunSerializer,
    PayrollEntrySerializer,
)
from .base import ReadOnlyViewSet, ListOnlyViewSet


class EmployeeViewSet(ReadOnlyViewSet):
    serializer_class = EmployeeSerializer

    def get_queryset(self):
        qs = Employee.objects.select_related('user').order_by('employee_id')
        params = self.request.query_params
        if params.get('active') == 'true':
            qs = qs.filter(is_active=True)
        search = params.get('search')
        if search:
            qs = qs.filter(
                Q(employee_id__icontains=search)
                | Q(job_title__icontains=search)
                | Q(user__first_name__icontains=search)
                | Q(user__last_name__icontains=search)
                | Q(user__username__icontains=search)
            )
        return qs


class AttendanceViewSet(ListOnlyViewSet):
    serializer_class = AttendanceRecordSerializer

    def get_queryset(self):
        qs = AttendanceRecord.objects.select_related('employee', 'employee__user').order_by('-date')
        params = self.request.query_params
        if params.get('employee'):
            qs = qs.filter(employee_id=params['employee'])
        if params.get('date'):
            qs = qs.filter(date=params['date'])
        return qs


class LeaveRequestViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = LeaveRequestSerializer

    def get_queryset(self):
        qs = LeaveRequest.objects.select_related('employee', 'employee__user').order_by('-created_at')
        params = self.request.query_params
        if params.get('employee'):
            qs = qs.filter(employee_id=params['employee'])
        if params.get('status'):
            qs = qs.filter(status=params['status'])
        return qs

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        leave = self.get_object()
        leave.status = 'approved'
        leave.reviewed_at = timezone.now()
        leave.review_notes = request.data.get('notes', '') or leave.review_notes
        leave.save(update_fields=['status', 'reviewed_at', 'review_notes'])
        return Response(LeaveRequestSerializer(leave).data)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        leave = self.get_object()
        leave.status = 'rejected'
        leave.reviewed_at = timezone.now()
        leave.review_notes = request.data.get('notes', '') or leave.review_notes
        leave.save(update_fields=['status', 'reviewed_at', 'review_notes'])
        return Response(LeaveRequestSerializer(leave).data)


class AdvanceViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = AdvanceSerializer

    def get_queryset(self):
        qs = Advance.objects.select_related('employee', 'employee__user').order_by('-requested_at')
        if self.request.query_params.get('employee'):
            qs = qs.filter(employee_id=self.request.query_params['employee'])
        return qs

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        advance = self.get_object()
        advance.status = 'approved'
        advance.approved_at = timezone.now()
        advance.remaining_amount = advance.amount
        advance.save(update_fields=['status', 'approved_at', 'remaining_amount'])
        return Response(AdvanceSerializer(advance).data)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        advance = self.get_object()
        advance.status = 'rejected'
        advance.rejection_reason = request.data.get('notes', '') or advance.rejection_reason
        advance.save(update_fields=['status', 'rejection_reason'])
        return Response(AdvanceSerializer(advance).data)


class PayrollRunViewSet(ReadOnlyViewSet):
    serializer_class = PayrollRunSerializer
    queryset = PayrollRun.objects.all().order_by('-period_year', '-period_month')


class PayrollEntryViewSet(ListOnlyViewSet):
    serializer_class = PayrollEntrySerializer

    def get_queryset(self):
        qs = PayrollEntry.objects.select_related('employee', 'employee__user').order_by('-id')
        if self.request.query_params.get('run'):
            qs = qs.filter(payroll_run_id=self.request.query_params['run'])
        return qs
