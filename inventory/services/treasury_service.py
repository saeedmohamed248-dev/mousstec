"""
💰 Treasury Service — Owns all financial mutations.

Responsibilities:
- Treasury balance updates (atomic F-expressions)
- Core charge refund (double-entry with treasury guard)
- Accounting entry generation (double-entry ledger)
- Financial transaction lifecycle
"""

import logging
from decimal import Decimal, ROUND_HALF_UP
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
        refund_amount = (Decimal(str(instance.quantity)) * Decimal(str(instance.core_charge_applied))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

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
    # Commission Payout — explicit treasury, branch-scoped, idempotent.
    # ------------------------------------------------------------------
    # 🐛 [Commission audit FIX 2026-06-05]: pay_commissions كان بياخد أول
    # خزينة في الـ DB من غير ما يـ branch-scope، كان محدود role='tech' بس،
    # ما كانش بيـ link الـ FinancialTransaction لـ employee FK، وما كانش
    # عنده select_for_update فـ double-click ممكن يـ double-pay. النسخة دي
    # تـ accept treasury صريح، تشتغل لكل الأدوار اللي بتاخد عمولة، وتـ lock
    # الـ profile قبل ما تـ debit الـ balance.
    @staticmethod
    def pay_commissions(employee_profiles, treasury, paid_by_user=None, allowed_roles=None):
        """
        Pay outstanding commissions for a queryset of EmployeeProfile objects.

        Args:
            employee_profiles: queryset / iterable of EmployeeProfile
            treasury:          Treasury instance to debit (REQUIRED — no auto-pick)
            paid_by_user:      User performing the payout (audit trail)
            allowed_roles:     filter to these roles (default: all roles that
                               accrue commission: tech, sales, cashier, manager)

        Returns: {
            'paid_count': N,
            'total_paid': Decimal,
            'treasury_name': str,
            'breakdown': [{'employee_id', 'name', 'role', 'amount'}],
        }

        Raises ValidationError on:
            - inactive/missing treasury
            - empty queryset (no one selected)
            - treasury balance after payout would go negative
        """
        from inventory.models import (
            Treasury, FinancialTransaction, EmployeeProfile,
        )

        if not treasury or not treasury.is_active:
            raise ValidationError("الخزنة غير صالحة أو معطّلة.")

        if allowed_roles is None:
            allowed_roles = {'tech', 'sales', 'cashier', 'manager', 'admin'}
        else:
            allowed_roles = set(allowed_roles)

        breakdown = []
        total_paid = Decimal('0.00')

        # Pre-flight: estimate total to pay and guard against certain overdraft
        estimated_total = sum(
            p.commission_balance for p in employee_profiles
            if p.commission_balance > Decimal('0') and p.role in allowed_roles
        )
        if estimated_total > treasury.balance:
            raise ValidationError(
                f"إجمالي العمولات المستحقة ({estimated_total:.2f} ج) يتجاوز رصيد الخزينة ({treasury.balance:.2f} ج). "
                "يرجى إعادة تعبئة الخزينة أو اختيار موظفين أقل."
            )

        with transaction.atomic():
            # Lock the treasury row so concurrent payouts can't race the balance check.
            treasury_locked = Treasury.objects.select_for_update().get(pk=treasury.pk)

            for profile in employee_profiles:
                # Re-fetch with row lock — prevents double-pay on double-click.
                # 🐛 [test FIX]: PostgreSQL refuses FOR UPDATE on the nullable side of
                # an outer join (user/branch FKs are nullable → select_related does
                # LEFT JOIN). Lock only the EmployeeProfile row itself via of=('self',)
                # then pull user/branch separately for the display name.
                profile = (
                    EmployeeProfile.objects.select_for_update(of=('self',))
                    .get(pk=profile.pk)
                )
                # Hydrate related (no lock needed — read-only for description)
                if profile.user_id:
                    profile.user = type(profile).user.field.related_model.objects.filter(pk=profile.user_id).first()
                if profile.role not in allowed_roles:
                    continue
                amount = profile.commission_balance
                if amount <= Decimal('0.00'):
                    continue

                # Audit-linked transaction
                user = profile.user
                display_name = (
                    (user.get_full_name() or user.username) if user else f'#{profile.pk}'
                )
                FinancialTransaction.objects.create(
                    treasury=treasury_locked,
                    transaction_type='out',
                    amount=amount,
                    description=(
                        f"صرف عمولات مستحقة لـ «{display_name}» "
                        f"(دور: {profile.get_role_display()})"
                        + (f" — معتمد من {paid_by_user.username}" if paid_by_user else "")
                    ),
                    employee=profile,
                )

                profile.commission_balance = Decimal('0.00')
                profile.save(update_fields=['commission_balance'])

                breakdown.append({
                    'employee_id': profile.pk,
                    'name': display_name,
                    'role': profile.role,
                    'amount': amount,
                })
                total_paid += amount

        if not breakdown:
            raise ValidationError(
                "لم يتم العثور على أي عمولة مستحقة في الموظفين المحددين."
            )

        logger.info(
            "[COMMISSIONS] paid=%s total=%s EGP treasury=%s by=%s",
            len(breakdown), total_paid, treasury.name,
            paid_by_user.username if paid_by_user else '?',
        )
        return {
            'paid_count': len(breakdown),
            'total_paid': total_paid,
            'treasury_name': treasury.name,
            'breakdown': breakdown,
        }

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
