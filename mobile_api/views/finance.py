"""Finance views — الخزائن، الحركات المالية، فئات المصروفات."""
from django.db import transaction
from rest_framework import mixins, serializers, viewsets

from inventory.models import Treasury, FinancialTransaction, ExpenseCategory

from ..serializers import (
    TreasurySerializer,
    TreasuryWriteSerializer,
    FinancialTransactionSerializer,
    FinancialTransactionCreateSerializer,
    ExpenseCategorySerializer,
)
from ..permissions import FINANCE, FINANCE_DESK, can_edit_branch
from .base import FullCrudViewSet, RoleScopedMixin


class TreasuryViewSet(FullCrudViewSet):
    read_roles = FINANCE_DESK
    write_roles = FINANCE

    def _check_branch(self, branch):
        if branch is not None and not can_edit_branch(self.request.user, branch.pk):
            raise serializers.ValidationError({'branch': 'صلاحيتك على الفرع ده عرض فقط.'})

    def perform_create(self, serializer):
        self._check_branch(serializer.validated_data.get('branch'))
        serializer.save()

    def perform_update(self, serializer):
        self._check_branch(serializer.instance.branch)
        self._check_branch(serializer.validated_data.get('branch'))
        serializer.save()

    def perform_destroy(self, instance):
        self._check_branch(instance.branch)
        if instance.balance:
            # خزنة فيها فلوس تتقفل (is_active=False) أو تتصفّى بتحويل — مش تتمسح
            raise serializers.ValidationError(
                {'detail': f'الخزنة فيها رصيد ({instance.balance}) — حوّله الأول أو أوقف الخزنة.'})
        instance.delete()

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return TreasuryWriteSerializer
        return TreasurySerializer

    def get_queryset(self):
        qs = self.scope_branches(Treasury.objects.select_related('branch').order_by('name'))
        if self.request.query_params.get('active') == 'true':
            qs = qs.filter(is_active=True)
        return qs


class FinancialTransactionViewSet(
    RoleScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.CreateModelMixin, viewsets.GenericViewSet,
):
    read_roles = FINANCE
    write_roles = FINANCE_DESK   # الكاشير بيسجّل مصروف يومي زي شاشة «مصروف سريع»

    def perform_create(self, serializer):
        """🔒 الخزنة لازم تكون نشطة وفي فرع الموظف، والصرف مايعدّيش رصيدها."""
        from inventory.models import Treasury as _T
        data = serializer.validated_data
        with transaction.atomic():
            till = _T.objects.select_for_update().get(pk=data['treasury'].pk)
            if not till.is_active:
                raise serializers.ValidationError({'treasury': 'الخزنة دي متوقفة.'})
            if not can_edit_branch(self.request.user, till.branch_id):
                raise serializers.ValidationError({'treasury': 'صلاحيتك على فرع الخزنة دي عرض فقط.'})
            if data['transaction_type'] == 'out' and till.balance < data['amount']:
                raise serializers.ValidationError(
                    {'amount': f'رصيد خزنة «{till.name}» مش كفاية (متاح: {till.balance}).'})
            serializer.save()

    def get_queryset(self):
        qs = self.scope_branches(
            FinancialTransaction.objects.select_related('treasury', 'category').order_by('-date'),
            field='treasury__branch_id')
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
    write_roles = FINANCE
    serializer_class = ExpenseCategorySerializer
    queryset = ExpenseCategory.objects.all().order_by('name')
