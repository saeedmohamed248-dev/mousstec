"""Purchasing serializers — فواتير الشراء وبنودها."""
from rest_framework import serializers

from inventory.models.invoices import PurchaseInvoice, PurchaseInvoiceItem


class PurchaseInvoiceItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    part_number = serializers.CharField(source='product.part_number', read_only=True)
    total_price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = PurchaseInvoiceItem
        fields = ('id', 'product', 'product_name', 'part_number', 'quantity', 'cost_price', 'total_price')


class PurchaseInvoiceListSerializer(serializers.ModelSerializer):
    vendor_name = serializers.CharField(source='vendor.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = PurchaseInvoice
        fields = (
            'id', 'vendor', 'vendor_name', 'branch', 'branch_name',
            'total_amount', 'paid_amount', 'status', 'status_display', 'date_created',
        )


class PurchaseInvoiceDetailSerializer(PurchaseInvoiceListSerializer):
    items = PurchaseInvoiceItemSerializer(many=True, read_only=True)

    class Meta(PurchaseInvoiceListSerializer.Meta):
        fields = PurchaseInvoiceListSerializer.Meta.fields + ('bidding_ref', 'items')
