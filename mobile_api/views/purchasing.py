"""Purchasing views — فواتير الشراء."""
from django.db.models import Q

from inventory.models.invoices import PurchaseInvoice

from ..serializers import (
    PurchaseInvoiceListSerializer,
    PurchaseInvoiceDetailSerializer,
)
from .base import ReadOnlyViewSet


class PurchaseInvoiceViewSet(ReadOnlyViewSet):
    def get_queryset(self):
        qs = PurchaseInvoice.objects.select_related('vendor', 'branch').order_by('-date_created')
        params = self.request.query_params
        if params.get('status'):
            qs = qs.filter(status=params['status'])
        if params.get('vendor'):
            qs = qs.filter(vendor_id=params['vendor'])
        search = params.get('search')
        if search:
            qs = qs.filter(Q(vendor__name__icontains=search) | Q(id__icontains=search))
        return qs

    def get_serializer_class(self):
        return PurchaseInvoiceDetailSerializer if self.action == 'retrieve' else PurchaseInvoiceListSerializer
