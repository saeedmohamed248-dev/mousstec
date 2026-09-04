"""Finance serializers — الخزائن، الحركات المالية، فئات المصروفات."""
from rest_framework import serializers

from inventory.models import Treasury, FinancialTransaction, ExpenseCategory


class TreasurySerializer(serializers.ModelSerializer):
    branch_name = serializers.CharField(source='branch.name', read_only=True)

    class Meta:
        model = Treasury
        fields = ('id', 'name', 'branch', 'branch_name', 'type', 'balance', 'is_active')


class TreasuryWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Treasury
        fields = ('id', 'name', 'branch', 'type', 'balance', 'is_active')


class ExpenseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ('id', 'name', 'system_key')


class FinancialTransactionSerializer(serializers.ModelSerializer):
    treasury_name = serializers.CharField(source='treasury.name', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True, default=None)
    type_display = serializers.CharField(source='get_transaction_type_display', read_only=True)

    class Meta:
        model = FinancialTransaction
        fields = (
            'id', 'treasury', 'treasury_name', 'transaction_type', 'type_display',
            'amount', 'currency', 'category', 'category_name', 'description', 'date',
        )
        read_only_fields = ('date',)


class FinancialTransactionCreateSerializer(serializers.ModelSerializer):
    """تسجيل حركة نقدية (قبض/صرف) من التطبيق."""

    class Meta:
        model = FinancialTransaction
        fields = ('id', 'treasury', 'transaction_type', 'amount', 'category', 'description')

    def validate_amount(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError('المبلغ يجب أن يكون أكبر من صفر.')
        return value
