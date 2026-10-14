"""Workshop views — أوامر الشغل، سجلات الإصلاح، تقارير التشخيص."""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from inventory.models.invoices import SaleInvoice
from inventory.models import RepairLog, VehicleDiagnosticReport

from ..serializers import (
    WorkOrderListSerializer,
    WorkOrderDetailSerializer,
    WorkOrderCreateSerializer,
    WorkOrderStatusUpdateSerializer,
    RepairLogSerializer,
    DiagnosticReportSerializer,
)
from ..permissions import FRONT_DESK, can_edit_branch
from .base import ListOnlyViewSet, RoleScopedMixin

# خط سير أمر الشغل — للأمام بس (الاعتماد/التسليم مابيرجعش)
STATUS_FLOW = ['quotation', 'in_progress', 'quality_check', 'ready', 'posted']


class WorkOrderViewSet(
    RoleScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    write_roles = FRONT_DESK
    action_roles = {'update_status': FRONT_DESK}

    def perform_create(self, serializer):
        branch = serializer.validated_data.get('branch')
        if branch is None or not can_edit_branch(self.request.user, branch.pk):
            raise serializers.ValidationError({'branch': 'صلاحيتك على الفرع ده عرض فقط.'})
        vehicle = serializer.validated_data.get('vehicle')
        customer = serializer.validated_data.get('customer')
        if vehicle is not None and customer is not None and vehicle.customer_id != customer.pk:
            raise serializers.ValidationError({'vehicle': 'العربية دي مش بتاعة العميل ده.'})
        serializer.save()

    def get_queryset(self):
        qs = self.scope_branches(
            SaleInvoice.objects.filter(invoice_type='maintenance')
            .select_related('customer', 'vehicle', 'branch')
            .order_by('-date_created')
        )
        params = self.request.query_params
        status_param = params.get('status')
        if status_param == 'open':
            qs = qs.exclude(status='posted')
        elif status_param:
            qs = qs.filter(status=status_param)
        if params.get('branch'):
            qs = qs.filter(branch_id=params['branch'])
        search = params.get('search')
        if search:
            qs = qs.filter(
                Q(customer__name__icontains=search)
                | Q(customer__phone__icontains=search)
                | Q(vehicle__car_plate__icontains=search)
                | Q(id__icontains=search)
            )
        return qs

    def get_serializer_class(self):
        if self.action == 'create':
            return WorkOrderCreateSerializer
        if self.action == 'retrieve':
            return WorkOrderDetailSerializer
        return WorkOrderListSerializer

    @action(detail=True, methods=['post'], url_path='status')
    def update_status(self, request, pk=None):
        """تقديم حالة أمر الشغل.

        🐛 [FIX]: أي حالة كانت مسموحة — فاتورة معتمدة كانت بترجع «عرض سعر»
        (وتختفي من التقارير) من غير ما يتعكس مخزونها ولا فلوسها، ورصيد مخزون
        مش كفاية كان بيطلّع 500. دلوقتي للأمام بس، والاعتماد بيمر على نفس
        مسار التسليم في البوابة.
        """
        order = self.get_object()
        serializer = WorkOrderStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new = serializer.validated_data['status']
        if not can_edit_branch(request.user, order.branch_id):
            return Response({'detail': 'صلاحيتك على فرع أمر الشغل عرض فقط.'},
                            status=status.HTTP_403_FORBIDDEN)
        backwards = Response({'detail': 'الحالة دي بترجّع أمر الشغل لورا — مش مسموح.'},
                             status=status.HTTP_400_BAD_REQUEST)
        try:
            with transaction.atomic():
                locked = SaleInvoice.objects.select_for_update().get(pk=order.pk)
                # الفحص على الحالة بعد القفل (طلبين في نفس الوقت مايرجّعوش الحالة لورا)
                if locked.status == new:
                    return Response(WorkOrderDetailSerializer(locked).data, status=status.HTTP_200_OK)
                if (locked.status not in STATUS_FLOW or new not in STATUS_FLOW
                        or STATUS_FLOW.index(new) < STATUS_FLOW.index(locked.status)):
                    return backwards
                locked.status = new
                # مطبّق (المخزون والفلوس اتسجّلوا): الحالة بس — غير كده الاعتماد بيشغّل execute_sale
                locked.save(update_fields=['status'] if (locked.is_applied or new != 'posted') else None)
        except DjangoValidationError as exc:
            return Response({'detail': '؛ '.join(exc.messages)}, status=status.HTTP_409_CONFLICT)
        order.refresh_from_db()
        return Response(WorkOrderDetailSerializer(order).data, status=status.HTTP_200_OK)


class RepairLogViewSet(ListOnlyViewSet):
    serializer_class = RepairLogSerializer

    def get_queryset(self):
        qs = self.scope_branches(RepairLog.objects.select_related('technician').order_by('-started_at'),
                                 field='job_card__branch_id')
        if self.request.query_params.get('job_card'):
            qs = qs.filter(job_card_id=self.request.query_params['job_card'])
        return qs


class DiagnosticReportViewSet(ListOnlyViewSet):
    serializer_class = DiagnosticReportSerializer

    def get_queryset(self):
        qs = VehicleDiagnosticReport.objects.order_by('-scanned_at')
        params = self.request.query_params
        if params.get('vehicle'):
            qs = qs.filter(vehicle_id=params['vehicle'])
        if params.get('job_card'):
            qs = qs.filter(job_card_id=params['job_card'])
        return qs
