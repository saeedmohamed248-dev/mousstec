"""HR views — الموظفون، الحضور، الإجازات، السلف، الرواتب."""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
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
from ..permissions import HR_STAFF, PAYROLL, FINANCE, has_role
from .base import ReadOnlyViewSet, ListOnlyViewSet, RoleScopedMixin


def _me(request):
    """ملف HR للمستخدم الحالي (أو None)."""
    return Employee.objects.filter(user=request.user).first()


def _err(exc):
    msgs = getattr(exc, 'messages', None) or [str(exc)]
    return Response({'detail': '؛ '.join(str(m) for m in msgs)}, status=status.HTTP_400_BAD_REQUEST)


class EmployeeViewSet(ReadOnlyViewSet):
    # الملفات فيها المرتبات — للـ HR والإدارة والمحاسبة بس
    read_roles = PAYROLL
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


class _OwnRecordsMixin:
    """الموظف بيشوف سجلاته هو بس — الـ HR/الإدارة بيشوفوا الكل."""
    see_all_roles = HR_STAFF

    def own_scope(self, qs):
        if has_role(self.request.user, self.see_all_roles):
            return qs
        return qs.filter(employee__user=self.request.user)


class AttendanceViewSet(_OwnRecordsMixin, ListOnlyViewSet):
    serializer_class = AttendanceRecordSerializer

    def get_queryset(self):
        qs = self.own_scope(
            AttendanceRecord.objects.select_related('employee', 'employee__user').order_by('-date'))
        params = self.request.query_params
        if params.get('employee'):
            qs = qs.filter(employee_id=params['employee'])
        if params.get('date'):
            qs = qs.filter(date=params['date'])
        return qs


class LeaveRequestViewSet(
    _OwnRecordsMixin, RoleScopedMixin,
    mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    serializer_class = LeaveRequestSerializer
    write_roles = None   # أي موظف يطلب إجازة لنفسه
    action_roles = {'approve': None, 'reject': None}   # الخدمة بتتحقق (HR/المدير المباشر)

    def own_scope(self, qs):
        # المدير المباشر بيشوف طلبات فريقه عشان يوافق عليها، ومدير الـ HR بيشوف الكل
        if has_role(self.request.user, self.see_all_roles):
            return qs
        me = _me(self.request)
        if me is not None and me.is_hr_manager:
            return qs
        return qs.filter(Q(employee__user=self.request.user)
                         | Q(employee__supervisor__user=self.request.user))

    def get_queryset(self):
        qs = self.own_scope(
            LeaveRequest.objects.select_related('employee', 'employee__user').order_by('-created_at'))
        params = self.request.query_params
        if params.get('employee'):
            qs = qs.filter(employee_id=params['employee'])
        if params.get('status'):
            qs = qs.filter(status=params['status'])
        return qs

    def perform_create(self, serializer):
        # 🐛 [FIX]: الطلب كان بيتعمل باسم أي موظف (employee من الطلب نفسه)
        me = _me(self.request)
        target = serializer.validated_data.get('employee')
        if not has_role(self.request.user, HR_STAFF) or target is None:
            if me is None:
                raise serializers.ValidationError({'employee': 'ليس لديك ملف موظف مفعّل.'})
            target = me
        if serializer.validated_data['to_date'] < serializer.validated_data['from_date']:
            raise serializers.ValidationError({'to_date': 'تاريخ النهاية قبل البداية.'})
        serializer.save(employee=target, status='pending')

    def _review(self, request, pk, fn):
        leave = self.get_object()
        try:
            fn(leave.pk, reviewer=_me(request), by_user=request.user,
               notes=request.data.get('notes', '') or '')
        except DjangoValidationError as exc:
            return _err(exc)
        leave.refresh_from_db()
        return Response(LeaveRequestSerializer(leave).data)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        # 🐛 [FIX]: كانت بتوافق من غير أي فحص (الموظف يوافق على إجازته بنفسه)
        from hr.services.leave_service import approve_leave
        return self._review(request, pk, approve_leave)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        from hr.services.leave_service import reject_leave
        return self._review(request, pk, reject_leave)


class AdvanceViewSet(
    _OwnRecordsMixin, RoleScopedMixin,
    mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    serializer_class = AdvanceSerializer
    see_all_roles = PAYROLL
    write_roles = None   # أي موظف يطلب سلفة لنفسه
    action_roles = {'approve': HR_STAFF, 'reject': HR_STAFF, 'disburse': FINANCE + ('hr',)}

    def get_queryset(self):
        qs = self.own_scope(
            Advance.objects.select_related('employee', 'employee__user').order_by('-requested_at'))
        if self.request.query_params.get('employee'):
            qs = qs.filter(employee_id=self.request.query_params['employee'])
        return qs

    def create(self, request, *args, **kwargs):
        # 🐛 [FIX]: الإنشاء كان بيتخطّى سياسة السلف (الحد الأقصى من المرتب، عدد
        #    الأقساط، سلفة قائمة) — دلوقتي بيمر على AdvanceService زي البوابة.
        from hr.services.advance_service import AdvanceService
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        me = _me(request)
        if me is None:
            return Response({'detail': 'ليس لديك ملف موظف مفعّل.'}, status=status.HTTP_403_FORBIDDEN)
        data = serializer.validated_data
        try:
            advance = AdvanceService.request_advance(
                me, data['amount'], installments_count=data.get('installments_count') or 1,
                reason=data.get('reason') or '')
        except DjangoValidationError as exc:
            return _err(exc)
        return Response(AdvanceSerializer(advance).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        # 🐛 [FIX]: الموافقة كانت بتغيّر الحالة بس — من غير أقساط (فالسلفة عمرها ما
        #    تتخصم من المرتب) ومن غير أي فحص صلاحية.
        from hr.services.advance_service import AdvanceService
        advance = self.get_object()
        try:
            AdvanceService.approve_advance(advance.pk, _me(request), by_user=request.user)
        except DjangoValidationError as exc:
            return _err(exc)
        advance.refresh_from_db()
        return Response(AdvanceSerializer(advance).data)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        from hr.services.advance_service import AdvanceService
        advance = self.get_object()
        try:
            AdvanceService.reject_advance(advance.pk, _me(request), request.data.get('notes', '') or '',
                                          by_user=request.user)
        except DjangoValidationError as exc:
            return _err(exc)
        advance.refresh_from_db()
        return Response(AdvanceSerializer(advance).data)

    @action(detail=True, methods=['post'])
    def disburse(self, request, pk=None):
        """💵 صرف السلفة المعتمدة من الخزنة (treasury_id اختياري)."""
        from hr.services.advance_service import AdvanceService
        advance = self.get_object()
        try:
            AdvanceService.disburse_advance(advance.pk, request.data.get('treasury_id') or None,
                                            by_user=request.user)
        except DjangoValidationError as exc:
            return _err(exc)
        advance.refresh_from_db()
        return Response(AdvanceSerializer(advance).data)


class _PayrollPlanGate:
    """🔒 المرتبات ميزة Gold/Pro وأعلى (hr_payroll) — نفس قفل لوحة الشركة."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        from django.db import connection
        from rest_framework.exceptions import PermissionDenied
        from tenancy.services.entitlements import EntitlementService
        if not EntitlementService.allows(getattr(connection, 'tenant', None), 'hr_payroll'):
            raise PermissionDenied('كشوف المرتبات مش ضمن باقتك — متاحة في Gold/Pro وأعلى.')


class PayrollRunViewSet(_PayrollPlanGate, ReadOnlyViewSet):
    read_roles = PAYROLL
    serializer_class = PayrollRunSerializer
    queryset = PayrollRun.objects.all().order_by('-period_year', '-period_month')


class PayrollEntryViewSet(_PayrollPlanGate, _OwnRecordsMixin, ListOnlyViewSet):
    """كشف المرتب: الموظف بيشوف بتاعه بس — الـ HR/المحاسبة بيشوفوا الكل."""
    serializer_class = PayrollEntrySerializer
    see_all_roles = PAYROLL

    def get_queryset(self):
        qs = self.own_scope(
            PayrollEntry.objects.select_related('employee', 'employee__user').order_by('-id'))
        if self.request.query_params.get('run'):
            qs = qs.filter(payroll_run_id=self.request.query_params['run'])
        return qs
