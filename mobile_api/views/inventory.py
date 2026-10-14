"""Inventory views — القطع، المخزون، التحويلات، الحركات، الموردون، الخدمات، التفكيك."""
from django.db.models import Sum, Q, F
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
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
from ..permissions import (
    MANAGEMENT, PURCHASING, STOCK, STOCK_MOVES, can_edit_branch, can_see_costs,
)
from .base import ReadOnlyViewSet, ListOnlyViewSet, FullCrudViewSet, RoleScopedMixin

# حقول التكلفة — بتختفي عن اللي مالوش صلاحية يشوف التكلفة (زي البوابة)
COST_FIELDS = ('purchase_price', 'average_cost')


class ProductViewSet(FullCrudViewSet):
    write_roles = STOCK

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

    def finalize_response(self, request, response, *args, **kwargs):
        # 💰 التكلفة للي معاه صلاحية بس (البائع/الفني بيشوف سعر البيع)
        if (getattr(response, 'data', None) is not None and request.user.is_authenticated
                and not can_see_costs(request.user)):
            def _strip(row):
                if isinstance(row, dict):
                    for f in COST_FIELDS:
                        row.pop(f, None)
            data = response.data
            rows = data.get('results') if isinstance(data, dict) and 'results' in data else data
            if isinstance(rows, list):
                for row in rows:
                    _strip(row)
            else:
                _strip(rows)
        return super().finalize_response(request, response, *args, **kwargs)

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
        qs = self.scope_branches(
            StockAlert.objects.select_related('product', 'branch').order_by('-created_at'))
        if self.request.query_params.get('resolved', 'false').lower() != 'true':
            qs = qs.filter(is_resolved=False)
        return qs


class StockTransferViewSet(
    RoleScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    write_roles = STOCK_MOVES
    serializer_class = StockTransferSerializer

    def perform_create(self, serializer):
        data = serializer.validated_data
        if not can_edit_branch(self.request.user, data['from_branch'].pk):
            raise serializers.ValidationError({'from_branch': 'صلاحيتك على فرع المصدر عرض فقط.'})
        if data['from_branch'].pk == data['to_branch'].pk:
            raise serializers.ValidationError({'to_branch': 'فرع المصدر وفرع الوصول لازم يبقوا مختلفين.'})
        if (data.get('quantity') or 0) <= 0:
            raise serializers.ValidationError({'quantity': 'الكمية لازم تبقى أكبر من صفر.'})
        serializer.save()

    def get_queryset(self):
        from django.db.models import Q as _Q
        from ..permissions import branch_ids
        qs = StockTransfer.objects.select_related(
            'product', 'from_branch', 'to_branch'
        ).order_by('-date_transferred')
        ids = branch_ids(self.request.user)
        if ids is not None:
            qs = qs.filter(_Q(from_branch_id__in=ids) | _Q(to_branch_id__in=ids))
        return qs


class InventoryMovementViewSet(ListOnlyViewSet):
    serializer_class = InventoryMovementSerializer

    def get_queryset(self):
        qs = self.scope_branches(
            InventoryMovement.objects.select_related('product', 'branch').order_by('-created_at'))
        if self.request.query_params.get('product'):
            qs = qs.filter(product_id=self.request.query_params['product'])
        return qs


class VendorViewSet(FullCrudViewSet):
    read_roles = PURCHASING + ('stock',)
    write_roles = PURCHASING
    serializer_class = VendorSerializer

    def get_queryset(self):
        qs = Vendor.objects.all().order_by('name')
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(phone__icontains=search))
        return qs


class ServiceCatalogViewSet(FullCrudViewSet):
    write_roles = MANAGEMENT
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
