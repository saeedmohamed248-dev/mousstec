"""
💰 Treasury Service — Owns all financial mutations.

Responsibilities:
- Treasury balance updates (atomic F-expressions)
- Core charge refund (double-entry with treasury guard)
- Accounting entry generation (double-entry ledger)
- Financial transaction lifecycle
"""

import logging
from decimal import Decimal
from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import F
from django.core.exceptions import ValidationError

logger = logging.getLogger('mouss_tec_core')

# Configurable account codes — override in settings.py via ACCOUNTING_CODES dict
_DEFAULT_CODES = {
    'cash': '1001',
    'sales_revenue': '4001',
    'other_revenue': '4099',
    'purchase_cost': '5001',
    'general_expense': '5099',
}


def _get_account_code(key):
    """Return the account code for a logical key, allowing settings override."""
    overrides = getattr(django_settings, 'ACCOUNTING_CODES', {})
    return overrides.get(key, _DEFAULT_CODES.get(key, key))


class TreasuryService:
    """All treasury and financial operations go through here."""

    # ------------------------------------------------------------------
    # Treasury Balance — atomic credit/debit
    # ------------------------------------------------------------------
    @staticmethod
    def update_balance(financial_transaction):
        """
        Atomically adjust treasury balance when a new FinancialTransaction is created.
        Called from signal: post_save(FinancialTransaction, created=True).
        """
        from inventory.models import Treasury

        amount = Decimal(str(financial_transaction.amount))
        with transaction.atomic():
            if financial_transaction.transaction_type == 'in':
                Treasury.objects.filter(
                    pk=financial_transaction.treasury.pk
                ).update(balance=F('balance') + amount)
            elif financial_transaction.transaction_type == 'out':
                Treasury.objects.filter(
                    pk=financial_transaction.treasury.pk
                ).update(balance=F('balance') - amount)

        logger.info(
            "[TREASURY] %s %s EGP on treasury #%s (type=%s)",
            "Credited" if financial_transaction.transaction_type == 'in' else "Debited",
            amount, financial_transaction.treasury_id, financial_transaction.transaction_type,
        )

    # ------------------------------------------------------------------
    # Core Charge Refund — safe double-entry with balance guard
    # ------------------------------------------------------------------
    @staticmethod
    def process_core_refund(sale_invoice_item):
        """
        Refund core charge to customer when the old part is returned.
        Double-entry: debit customer balance, debit treasury.
        Raises ValidationError if treasury balance insufficient.
        """
        from inventory.models import Treasury, FinancialTransaction

        instance = sale_invoice_item
        refund_amount = Decimal(str(instance.quantity)) * Decimal(str(instance.core_charge_applied))

        if refund_amount <= 0 or not instance.invoice.customer:
            return

        with transaction.atomic():
            customer = instance.invoice.customer
            customer.balance = F('balance') - refund_amount
            customer.save(update_fields=['balance'])

            if instance.invoice.treasury:
                treasury = Treasury.objects.select_for_update().get(
                    pk=instance.invoice.treasury.pk
                )

                if treasury.balance < refund_amount:
                    raise ValidationError(
                        f"خزينة {treasury.name} لا تحتوي على رصيد كافٍ لرد تأمين الكور."
                    )

                # NOTE: Do NOT manually deduct treasury.balance here.
                # Creating the FinancialTransaction triggers the
                # update_treasury_balance signal which atomically
                # deducts balance via TreasuryService.update_balance().
                FinancialTransaction.objects.create(
                    treasury=treasury,
                    transaction_type='out',
                    amount=refund_amount,
                    description=(
                        f"استرداد تأمين توالف لقطعة {instance.product.part_number} "
                        f"(الفاتورة #{instance.invoice.id})"
                    ),
                    customer=customer,
                )

            logger.info(
                "[CORE RETURN] Refunded %s EGP to %s (item pk=%s)",
                refund_amount, customer.name, instance.pk,
            )

    # ------------------------------------------------------------------
    # Accounting Entries — auto double-entry from FinancialTransaction
    # ------------------------------------------------------------------
    @staticmethod
    def generate_accounting_entries(financial_transaction):
        """
        Create double-entry accounting journal entries for a new FinancialTransaction.
        IN  → Debit cash account / Credit revenue account
        OUT → Debit expense account / Credit cash account
        """
        from inventory.models import ChartOfAccount, AccountingEntry
        from inventory.services.audit_service import AuditService

        instance = financial_transaction
        ref = f"FT-{instance.pk}"
        user = AuditService.get_request_user()

        try:
            if instance.transaction_type == 'in':
                # Debit: Cash (asset)
                cash_account = TreasuryService._get_or_create_account(
                    _get_account_code('cash'), 'الخزينة النقدية', 'asset'
                )
                AccountingEntry.objects.create(
                    reference=ref,
                    description=instance.description or 'إيداع نقدي',
                    account=cash_account,
                    debit=instance.amount,
                    credit=Decimal('0'),
                    financial_transaction=instance,
                    sale_invoice=instance.sale_invoice,
                    created_by=user,
                )
                # Credit: Revenue
                if instance.sale_invoice:
                    revenue_account = TreasuryService._get_or_create_account(
                        _get_account_code('sales_revenue'), 'إيرادات المبيعات', 'revenue'
                    )
                else:
                    revenue_account = TreasuryService._get_or_create_account(
                        _get_account_code('other_revenue'), 'إيرادات أخرى', 'revenue'
                    )
                AccountingEntry.objects.create(
                    reference=ref,
                    description=instance.description or 'إيراد',
                    account=revenue_account,
                    debit=Decimal('0'),
                    credit=instance.amount,
                    financial_transaction=instance,
                    sale_invoice=instance.sale_invoice,
                    created_by=user,
                )
            else:  # out
                # Debit: Expense
                if instance.purchase_invoice:
                    expense_account = TreasuryService._get_or_create_account(
                        _get_account_code('purchase_cost'), 'تكلفة المشتريات', 'expense'
                    )
                elif instance.category:
                    expense_account = TreasuryService._get_or_create_account(
                        f'5{instance.category.pk:03d}',
                        f'مصروفات — {instance.category.name}',
                        'expense',
                    )
                else:
                    expense_account = TreasuryService._get_or_create_account(
                        _get_account_code('general_expense'), 'مصروفات عمومية', 'expense'
                    )
                AccountingEntry.objects.create(
                    reference=ref,
                    description=instance.description or 'صرف نقدي',
                    account=expense_account,
                    debit=instance.amount,
                    credit=Decimal('0'),
                    financial_transaction=instance,
                    purchase_invoice=instance.purchase_invoice,
                    created_by=user,
                )
                # Credit: Cash
                cash_account = TreasuryService._get_or_create_account(
                    _get_account_code('cash'), 'الخزينة النقدية', 'asset'
                )
                AccountingEntry.objects.create(
                    reference=ref,
                    description=instance.description or 'سحب نقدي',
                    account=cash_account,
                    debit=Decimal('0'),
                    credit=instance.amount,
                    financial_transaction=instance,
                    purchase_invoice=instance.purchase_invoice,
                    created_by=user,
                )

            logger.info("[ACCOUNTING] Generated entries for %s (amount=%s)", ref, instance.amount)

        except Exception as e:
            logger.error("[ACCOUNTING] Failed to generate entries for FT #%s: %s", instance.pk, e)

    # ------------------------------------------------------------------
    # Technician Commission Payout — batch pay with atomic guard
    # ------------------------------------------------------------------
    @staticmethod
    def pay_commissions(queryset):
        """
        Pay outstanding commissions for a queryset of User objects.
        Returns (paid_count, total_paid, treasury_name) or raises if no active treasury.
        Called from admin action: pay_tech_commissions.
        """
        from inventory.models import Treasury, FinancialTransaction

        treasury = Treasury.objects.filter(is_active=True).first()
        if not treasury:
            raise ValidationError("لم يتم العثور على خزنة نشطة بالفرع لسحب المبالغ النقدية منها.")

        paid_count = 0
        total_paid = Decimal('0.00')

        with transaction.atomic():
            for user in queryset:
                if hasattr(user, 'employee_profile') and user.employee_profile.role == 'tech':
                    profile = user.employee_profile
                    amount = profile.commission_balance
                    if amount > 0:
                        FinancialTransaction.objects.create(
                            treasury=treasury,
                            transaction_type='out',
                            amount=amount,
                            description=(
                                f"صرف عمولات إنتاجية مستحقة للفني المعتمد: "
                                f"{user.get_full_name() or user.username}"
                            ),
                        )
                        profile.commission_balance = Decimal('0.00')
                        profile.save(update_fields=['commission_balance'])
                        paid_count += 1
                        total_paid += amount

        logger.info(
            "[COMMISSIONS] Paid %s technicians, total %s EGP from treasury '%s'",
            paid_count, total_paid, treasury.name,
        )
        return paid_count, total_paid, treasury.name

    # ------------------------------------------------------------------
    # Small Debt Reconciliation — write off micro-balances
    # ------------------------------------------------------------------
    @staticmethod
    def reconcile_small_debts(queryset, threshold=Decimal('20.00')):
        """
        Write off customer balances below threshold as allowed discount.
        Returns count of reconciled customers.
        Called from admin action: auto_reconcile_small_debts.
        """
        reconciled = 0
        with transaction.atomic():
            for customer in queryset:
                if Decimal('0') < customer.balance <= threshold:
                    customer.balance = Decimal('0')
                    customer.save(update_fields=['balance'])
                    reconciled += 1

        if reconciled:
            logger.info(
                "[RECONCILE] Wrote off small debts for %s customers (threshold=%s)",
                reconciled, threshold,
            )
        return reconciled

    # ------------------------------------------------------------------
    # Reverse Balance on Delete — undo treasury effect of deleted transaction
    # ------------------------------------------------------------------
    @staticmethod
    def reverse_balance_on_delete(financial_transaction):
        """
        Reverse the treasury balance effect when a FinancialTransaction is deleted.
        Deposit deleted → debit treasury | Expense deleted → credit treasury.
        Called from signal: post_delete(FinancialTransaction).
        """
        from inventory.models import Treasury

        amount = Decimal(str(financial_transaction.amount))
        try:
            with transaction.atomic():
                if financial_transaction.transaction_type == 'in':
                    Treasury.objects.filter(
                        pk=financial_transaction.treasury_id
                    ).update(balance=F('balance') - amount)
                elif financial_transaction.transaction_type == 'out':
                    Treasury.objects.filter(
                        pk=financial_transaction.treasury_id
                    ).update(balance=F('balance') + amount)

            logger.info(
                "[TREASURY] Reversed %s %s EGP on treasury #%s (deleted)",
                "debit" if financial_transaction.transaction_type == 'in' else "credit",
                amount, financial_transaction.treasury_id,
            )
        except Exception as e:
            logger.error("[TREASURY] Failed to reverse balance on delete: %s", e)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _get_or_create_account(code, name, account_type):
        from inventory.models import ChartOfAccount
        account, _ = ChartOfAccount.objects.get_or_create(
            code=code,
            defaults={'name': name, 'account_type': account_type},
        )
        return account
