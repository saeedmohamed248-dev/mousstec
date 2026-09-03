"""
📱 Mobile API — Serializers

طبقة تحويل الموديلات إلى JSON مُبسّط ومناسب لشاشات الموبايل. نتجنّب تسريب
الحقول الحسّاسة (التكلفة، الأرباح، العمولات) ونعرض فقط ما يحتاجه الفنّي/الكاشير
على الجهاز.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from rest_framework import serializers

from inventory.models import (
    Product,
    Inventory,
    StockAlert,
    Customer,
    Vehicle,
)
from inventory.models.invoices import (
    SaleInvoice,
    SaleInvoiceItem,
    SaleInvoiceServiceItem,
)
from inventory.models.organization import Branch


# ---------------------------------------------------------------------------
# 👤 المستخدم
# ---------------------------------------------------------------------------
class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'username', 'full_name', 'email', 'is_staff', 'is_superuser')

    def get_full_name(self, obj: User) -> str:
        return obj.get_full_name() or obj.username


# ---------------------------------------------------------------------------
# 🏢 الفرع
# ---------------------------------------------------------------------------
class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = ('id', 'name', 'location', 'phone')


# ---------------------------------------------------------------------------
# 📦 المخزون / قطع الغيار
# ---------------------------------------------------------------------------
class ProductListSerializer(serializers.ModelSerializer):
    """نسخة خفيفة لقوائم المخزون."""

    total_quantity = serializers.SerializerMethodField()
    is_low_stock = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = (
            'id', 'name', 'part_number', 'brand', 'condition',
            'car_model', 'retail_price', 'total_quantity',
            'min_stock_level', 'is_low_stock', 'is_active',
        )

    def get_total_quantity(self, obj: Product) -> int:
        # نعتمد على قيمة مُحسبة مسبقاً من الـ view (annotate) إن وُجدت لتجنّب N+1.
        annotated = getattr(obj, 'annotated_qty', None)
        if annotated is not None:
            return int(annotated)
        return int(obj.total_inventory_qty)

    def get_is_low_stock(self, obj: Product) -> bool:
        return self.get_total_quantity(obj) <= (obj.min_stock_level or 0)


class InventoryLocationSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = Inventory
        fields = ('id', 'branch', 'branch_name', 'quantity', 'shelf_location')


class ProductDetailSerializer(serializers.ModelSerializer):
    """تفاصيل كاملة لقطعة غيار واحدة مع توزيعها على الفروع."""

    total_quantity = serializers.SerializerMethodField()
    is_low_stock = serializers.SerializerMethodField()
    stock_by_branch = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = (
            'id', 'name', 'part_number', 'brand', 'condition',
            'engine_code', 'car_model', 'car_year', 'barcode',
            'retail_price', 'b2b_wholesale_price', 'warranty_months',
            'min_stock_level', 'total_quantity', 'is_low_stock',
            'is_active', 'stock_by_branch',
        )

    def get_total_quantity(self, obj: Product) -> int:
        return int(obj.total_inventory_qty)

    def get_is_low_stock(self, obj: Product) -> bool:
        return self.get_total_quantity(obj) <= (obj.min_stock_level or 0)

    def get_stock_by_branch(self, obj: Product):
        qs = obj.inventory_set.select_related('branch').all()
        return InventoryLocationSerializer(qs, many=True).data


class StockAlertSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    part_number = serializers.CharField(source='product.part_number', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    alert_type_display = serializers.CharField(source='get_alert_type_display', read_only=True)

    class Meta:
        model = StockAlert
        fields = (
            'id', 'product', 'product_name', 'part_number',
            'branch', 'branch_name', 'alert_type', 'alert_type_display',
            'current_quantity', 'min_stock_level', 'is_resolved', 'created_at',
        )


# ---------------------------------------------------------------------------
# 👥 العملاء والمركبات
# ---------------------------------------------------------------------------
class VehicleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vehicle
        fields = (
            'id', 'chassis_number', 'car_plate', 'brand', 'model_name',
            'color', 'transmission', 'last_mileage', 'ai_health_score',
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


# ---------------------------------------------------------------------------
# 🔧 أوامر شغل الورشة / الصيانة (SaleInvoice)
# ---------------------------------------------------------------------------
class WorkOrderItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    part_number = serializers.CharField(source='product.part_number', read_only=True)
    total_price = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

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
    due_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

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


class WorkOrderStatusUpdateSerializer(serializers.Serializer):
    """تحديث الحالة التشغيلية لأمر الشغل مع التحقق من صحّة القيمة."""

    status = serializers.ChoiceField(choices=[c[0] for c in SaleInvoice.STATUS_CHOICES])

    def validate_status(self, value: str) -> str:
        valid = {c[0] for c in SaleInvoice.STATUS_CHOICES}
        if value not in valid:
            raise serializers.ValidationError('حالة غير معروفة.')
        return value
