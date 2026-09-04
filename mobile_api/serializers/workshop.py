"""Workshop serializers — أوامر الشغل (SaleInvoice)، سجلات الإصلاح، تقارير التشخيص."""
from rest_framework import serializers

from inventory.models.invoices import (
    SaleInvoice,
    SaleInvoiceItem,
    SaleInvoiceServiceItem,
)
from inventory.models import RepairLog, VehicleDiagnosticReport

from .crm import VehicleSerializer


class WorkOrderItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    part_number = serializers.CharField(source='product.part_number', read_only=True)
    total_price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = SaleInvoiceItem
        fields = ('id', 'product', 'product_name', 'part_number', 'quantity', 'unit_price', 'total_price')


class WorkOrderServiceSerializer(serializers.ModelSerializer):
    service_name = serializers.CharField(source='service.name', read_only=True)

    class Meta:
        model = SaleInvoiceServiceItem
        fields = ('id', 'service', 'service_name', 'price', 'actual_hours')


class WorkOrderListSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    customer_phone = serializers.CharField(source='customer.phone', read_only=True)
    vehicle_plate = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    due_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = SaleInvoice
        fields = (
            'id', 'invoice_type', 'status', 'status_display',
            'customer_name', 'customer_phone', 'vehicle_plate',
            'branch_name', 'total_amount', 'paid_amount', 'due_amount',
            'date_created',
        )

    def get_vehicle_plate(self, obj: SaleInvoice):
        return obj.vehicle.car_plate if obj.vehicle_id and obj.vehicle else None


class WorkOrderDetailSerializer(WorkOrderListSerializer):
    vehicle = VehicleSerializer(read_only=True)
    items = WorkOrderItemSerializer(many=True, read_only=True)
    service_items = WorkOrderServiceSerializer(many=True, read_only=True)

    class Meta(WorkOrderListSerializer.Meta):
        fields = WorkOrderListSerializer.Meta.fields + (
            'vehicle', 'mileage', 'notes', 'labor_cost_manual',
            'discount', 'tax_percentage', 'items', 'service_items',
        )


class WorkOrderCreateSerializer(serializers.ModelSerializer):
    """إنشاء أمر شغل صيانة جديد من التطبيق (بيانات أساسية)."""

    class Meta:
        model = SaleInvoice
        fields = ('id', 'customer', 'vehicle', 'branch', 'mileage', 'notes')

    def create(self, validated_data):
        validated_data['invoice_type'] = 'maintenance'
        validated_data.setdefault('status', 'quotation')
        return super().create(validated_data)


class WorkOrderStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[c[0] for c in SaleInvoice.STATUS_CHOICES])


class RepairLogSerializer(serializers.ModelSerializer):
    technician_name = serializers.SerializerMethodField()

    class Meta:
        model = RepairLog
        fields = (
            'id', 'job_card', 'technician', 'technician_name', 'task_title',
            'tech_notes', 'status', 'started_at', 'ended_at',
            'needs_extra_parts', 'extra_parts_note',
        )

    def get_technician_name(self, obj):
        tech = getattr(obj, 'technician', None)
        if tech and getattr(tech, 'user', None):
            return tech.user.get_full_name() or tech.user.username
        return None


class DiagnosticReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleDiagnosticReport
        fields = (
            'id', 'job_card', 'vehicle', 'scan_type', 'scanned_at',
            'fault_codes', 'device_id', 'severity_score', 'source',
            'vin_snapshot', 'ai_summary',
        )
