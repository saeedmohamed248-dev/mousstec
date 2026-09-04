"""Finance views — الخزائن، الحركات المالية، فئات المصروفات."""
from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated

from inventory.models import Treasury, FinancialTransaction, ExpenseCategory

from ..serializers import (
    TreasurySerializer,
    TreasuryWriteSerializer,
    FinancialTransactionSerializer,
    FinancialTransactionCreateSerializer,
    ExpenseCategorySerializer,
)
from .base import ListOnlyViewSet, FullCrudViewSet


class TreasuryViewSet(FullCrudViewSet):
    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return TreasuryWriteSerializer
        return TreasurySerializer

    def get_queryset(self):
        qs = Treasury.objects.select_related('branch').order_by('name')
        if self.request.query_params.get('active') == 'true':
            qs = qs.filter(is_active=True)
        return qs


class FinancialTransactionViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = FinancialTransaction.objects.select_related('treasury', 'category').order_by('-date')
        params = self.request.query_params
        if params.get('treasury'):
            qs = qs.filter(treasury_id=params['treasury'])
        if params.get('type'):
            qs = qs.filter(transaction_type=params['type'])
        return qs

    def get_serializer_class(self):
        if self.action == 'create':
            return FinancialTransactionCreateSerializer
        return FinancialTransactionSerializer


class ExpenseCategoryViewSet(FullCrudViewSet):
    serializer_class = ExpenseCategorySerializer
    queryset = ExpenseCategory.objects.all().order_by('name')
