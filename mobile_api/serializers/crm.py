"""CRM serializers — العملاء، المركبات، العقود، التنبيهات، التقييمات."""
from rest_framework import serializers

from inventory.models import (
    Customer,
    Vehicle,
    MaintenanceContract,
    ServiceNudge,
    CustomerFeedback,
    VehicleTelemetryLog,
)


class VehicleSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)

    class Meta:
        model = Vehicle
        fields = (
            'id', 'customer', 'customer_name', 'chassis_number', 'car_plate',
            'brand', 'model_name', 'color', 'transmission', 'last_mileage',
            'estimated_next_visit', 'ai_health_score', 'predicted_failure_notes',
        )


class MaintenanceContractSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)

    class Meta:
        model = MaintenanceContract
        fields = (
            'id', 'customer', 'customer_name', 'contract_code',
            'start_date', 'end_date', 'total_value', 'is_active',
        )


class CustomerListSerializer(serializers.ModelSerializer):
    vip_tier = serializers.CharField(read_only=True)

    class Meta:
        model = Customer
        fields = ('id', 'name', 'phone', 'is_b2b_company', 'balance', 'loyalty_points', 'vip_tier')


class CustomerDetailSerializer(CustomerListSerializer):
    vehicles = VehicleSerializer(many=True, read_only=True)

    class Meta(CustomerListSerializer.Meta):
        fields = CustomerListSerializer.Meta.fields + ('tax_id', 'date_added', 'vehicles')


class CustomerWriteSerializer(serializers.ModelSerializer):
    """إنشاء / تعديل عميل من التطبيق."""

    class Meta:
        model = Customer
        fields = ('id', 'name', 'phone', 'is_b2b_company', 'tax_id')


class VehicleWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vehicle
        fields = (
            'id', 'customer', 'chassis_number', 'car_plate', 'brand',
            'model_name', 'color', 'transmission', 'last_mileage', 'estimated_next_visit',
        )


class MaintenanceContractWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = MaintenanceContract
        fields = ('id', 'customer', 'contract_code', 'start_date', 'end_date', 'total_value', 'is_active')


class ServiceNudgeSerializer(serializers.ModelSerializer):
    vehicle_plate = serializers.CharField(source='vehicle.car_plate', read_only=True)
    rule_name = serializers.CharField(source='rule.name', read_only=True)

    class Meta:
        model = ServiceNudge
        fields = (
            'id', 'vehicle', 'vehicle_plate', 'rule', 'rule_name',
            'due_at', 'due_at_mileage', 'reason', 'urgency', 'status',
        )


class CustomerFeedbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerFeedback
        fields = (
            'id', 'sale_invoice', 'rating', 'comment',
            'received_in_good_condition', 'responded_at',
        )


class VehicleTelemetrySerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleTelemetryLog
        fields = (
            'id', 'vehicle', 'dtc_codes_found', 'battery_voltage',
            'requires_immediate_attention', 'timestamp',
        )
