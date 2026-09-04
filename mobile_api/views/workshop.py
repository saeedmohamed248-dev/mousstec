"""Workshop views — أوامر الشغل، سجلات الإصلاح، تقارير التشخيص."""
from django.db.models import Q
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
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
from .base import ListOnlyViewSet


class WorkOrderViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = (
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
        order = self.get_object()
        serializer = WorkOrderStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order.status = serializer.validated_data['status']
        order.save(update_fields=['status'])
        return Response(WorkOrderDetailSerializer(order).data, status=status.HTTP_200_OK)


class RepairLogViewSet(ListOnlyViewSet):
    serializer_class = RepairLogSerializer

    def get_queryset(self):
        qs = RepairLog.objects.select_related('technician').order_by('-started_at')
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
