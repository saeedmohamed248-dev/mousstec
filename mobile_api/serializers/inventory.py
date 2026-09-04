"""Inventory serializers — القطع، المخزون، التحويلات، الحركات، الموردون، التفكيك."""
from rest_framework import serializers

from inventory.models import (
    Product,
    Inventory,
    StockAlert,
    StockTransfer,
    InventoryMovement,
    Vendor,
    ServiceCatalog,
    ScrapDismantlingJob,
)


class ProductListSerializer(serializers.ModelSerializer):
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
        annotated = getattr(obj, 'annotated_qty', None)
        return int(annotated) if annotated is not None else int(obj.total_inventory_qty)

    def get_is_low_stock(self, obj: Product) -> bool:
        return self.get_total_quantity(obj) <= (obj.min_stock_level or 0)


class InventoryLocationSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = Inventory
        fields = ('id', 'branch', 'branch_name', 'quantity', 'shelf_location')


class ProductDetailSerializer(serializers.ModelSerializer):
    total_quantity = serializers.SerializerMethodField()
    is_low_stock = serializers.SerializerMethodField()
    stock_by_branch = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = (
            'id', 'name', 'part_number', 'brand', 'condition',
            'engine_code', 'car_model', 'car_year', 'barcode',
            'retail_price', 'b2b_wholesale_price', 'purchase_price',
            'warranty_months', 'min_stock_level', 'total_quantity',
            'is_low_stock', 'is_active', 'stock_by_branch',
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


class StockTransferSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    from_branch_name = serializers.CharField(source='from_branch.name', read_only=True)
    to_branch_name = serializers.CharField(source='to_branch.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = StockTransfer
        fields = (
            'id', 'product', 'product_name', 'from_branch', 'from_branch_name',
            'to_branch', 'to_branch_name', 'quantity', 'status', 'status_display',
            'date_transferred',
        )
        read_only_fields = ('status', 'date_transferred')


class InventoryMovementSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = InventoryMovement
        fields = (
            'id', 'product', 'product_name', 'branch', 'branch_name',
            'reason', 'quantity_change', 'quantity_before', 'quantity_after',
            'note', 'created_at',
        )


class ProductWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = (
            'id', 'name', 'part_number', 'brand', 'condition', 'engine_code',
            'car_model', 'car_year', 'barcode', 'retail_price', 'b2b_wholesale_price',
            'purchase_price', 'min_stock_level', 'warranty_months', 'is_active',
        )


class VendorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = ('id', 'name', 'phone', 'tax_id', 'company_details', 'balance')
        read_only_fields = ('balance',)


class ServiceCatalogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceCatalog
        fields = ('id', 'name', 'labor_price', 'estimated_hours', 'tech_commission_percent')


class ScrapJobSerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = ScrapDismantlingJob
        fields = (
            'id', 'job_ref', 'car_model', 'chassis_number', 'branch', 'branch_name',
            'total_purchase_cost', 'engine_serial', 'date_dismantled', 'is_completed',
        )
