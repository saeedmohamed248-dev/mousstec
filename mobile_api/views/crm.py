"""CRM views — العملاء، المركبات، العقود، التنبيهات، التقييمات."""
from django.db.models import Q
from rest_framework.decorators import action
from rest_framework.response import Response

from inventory.models import (
    Customer,
    Vehicle,
    MaintenanceContract,
    ServiceNudge,
    CustomerFeedback,
)

from ..serializers import (
    CustomerListSerializer,
    CustomerDetailSerializer,
    CustomerWriteSerializer,
    VehicleSerializer,
    VehicleWriteSerializer,
    MaintenanceContractSerializer,
    MaintenanceContractWriteSerializer,
    ServiceNudgeSerializer,
    CustomerFeedbackSerializer,
)
from .base import ReadOnlyViewSet, ListOnlyViewSet, FullCrudViewSet


class CustomerViewSet(FullCrudViewSet):
    def get_queryset(self):
        qs = Customer.objects.all().order_by('name')
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(phone__icontains=search))
        return qs

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return CustomerWriteSerializer
        if self.action == 'retrieve':
            return CustomerDetailSerializer
        return CustomerListSerializer

    @action(detail=True, methods=['get'])
    def vehicles(self, request, pk=None):
        customer = self.get_object()
        data = VehicleSerializer(customer.vehicles.all(), many=True).data
        return Response(data)


class VehicleViewSet(FullCrudViewSet):
    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return VehicleWriteSerializer
        return VehicleSerializer

    def get_queryset(self):
        qs = Vehicle.objects.select_related('customer').order_by('-id')
        params = self.request.query_params
        if params.get('customer'):
            qs = qs.filter(customer_id=params['customer'])
        search = params.get('search')
        if search:
            qs = qs.filter(
                Q(car_plate__icontains=search)
                | Q(chassis_number__icontains=search)
                | Q(model_name__icontains=search)
            )
        return qs


class MaintenanceContractViewSet(FullCrudViewSet):
    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return MaintenanceContractWriteSerializer
        return MaintenanceContractSerializer

    def get_queryset(self):
        qs = MaintenanceContract.objects.select_related('customer').order_by('-start_date')
        if self.request.query_params.get('active') == 'true':
            qs = qs.filter(is_active=True)
        return qs


class ServiceNudgeViewSet(ListOnlyViewSet):
    serializer_class = ServiceNudgeSerializer

    def get_queryset(self):
        return ServiceNudge.objects.select_related('vehicle', 'rule').order_by('due_at')


class CustomerFeedbackViewSet(ListOnlyViewSet):
    serializer_class = CustomerFeedbackSerializer
    queryset = CustomerFeedback.objects.all().order_by('-responded_at')
