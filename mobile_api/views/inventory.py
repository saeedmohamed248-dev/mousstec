"""Inventory views — القطع، المخزون، التحويلات، الحركات، الموردون، الخدمات، التفكيك."""
from django.db.models import Sum, Q, F
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from inventory.models import (
    Product,
    StockAlert,
    StockTransfer,
    InventoryMovement,
    Vendor,
    ServiceCatalog,
    ScrapDismantlingJob,
)

from ..serializers import (
    ProductListSerializer,
    ProductDetailSerializer,
    ProductWriteSerializer,
    StockAlertSerializer,
    StockTransferSerializer,
    InventoryMovementSerializer,
    VendorSerializer,
    ServiceCatalogSerializer,
    ScrapJobSerializer,
)
from .base import ReadOnlyViewSet, ListOnlyViewSet, FullCrudViewSet


class ProductViewSet(FullCrudViewSet):
    def get_queryset(self):
        qs = Product.objects.annotate(annotated_qty=Sum('inventory__quantity')).order_by('name')
        params = self.request.query_params
        if params.get('active_only', 'true').lower() != 'false':
            qs = qs.filter(is_active=True)
        search = params.get('search')
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(part_number__icontains=search)
                | Q(barcode__icontains=search)
                | Q(car_model__icontains=search)
                | Q(brand__icontains=search)
            )
        return qs

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return ProductWriteSerializer
        return ProductDetailSerializer if self.action == 'retrieve' else ProductListSerializer

    @action(detail=False, methods=['get'], url_path='low-stock')
    def low_stock(self, request):
        qs = (
            Product.objects.filter(is_active=True)
            .annotate(annotated_qty=Sum('inventory__quantity'))
            .filter(Q(annotated_qty__lte=F('min_stock_level')) | Q(annotated_qty__isnull=True))
            .order_by('name')
        )
        page = self.paginate_queryset(qs)
        serializer = ProductListSerializer(page if page is not None else qs, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


class StockAlertViewSet(ListOnlyViewSet):
    serializer_class = StockAlertSerializer

    def get_queryset(self):
        qs = StockAlert.objects.select_related('product', 'branch').order_by('-created_at')
        if self.request.query_params.get('resolved', 'false').lower() != 'true':
            qs = qs.filter(is_resolved=False)
        return qs


class StockTransferViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = StockTransferSerializer

    def get_queryset(self):
        return StockTransfer.objects.select_related(
            'product', 'from_branch', 'to_branch'
        ).order_by('-date_transferred')


class InventoryMovementViewSet(ListOnlyViewSet):
    serializer_class = InventoryMovementSerializer

    def get_queryset(self):
        qs = InventoryMovement.objects.select_related('product', 'branch').order_by('-created_at')
        if self.request.query_params.get('product'):
            qs = qs.filter(product_id=self.request.query_params['product'])
        return qs


class VendorViewSet(FullCrudViewSet):
    serializer_class = VendorSerializer

    def get_queryset(self):
        qs = Vendor.objects.all().order_by('name')
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(phone__icontains=search))
        return qs


class ServiceCatalogViewSet(FullCrudViewSet):
    serializer_class = ServiceCatalogSerializer

    def get_queryset(self):
        qs = ServiceCatalog.objects.all().order_by('name')
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(name__icontains=search)
        return qs


class ScrapJobViewSet(ReadOnlyViewSet):
    serializer_class = ScrapJobSerializer
    queryset = ScrapDismantlingJob.objects.select_related('branch').order_by('-date_dismantled')
