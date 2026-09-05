"""
🏛️ AccountingService — the accrual double-entry posting engine.

Every economic event becomes a balanced ``JournalEntry`` (قيد يومية) whose
lines are ``AccountingEntry`` rows. Debits must equal credits or the post is
rejected, so the general ledger is *always* balanced. This is the accrual
core that makes the system behave like a real, standards-grade accounting
package (revenue recognised when earned, COGS matched to it, receivables and
payables tracked in the ledger, VAT split out, period closing, reversals).

Posting model
-------------
Sale invoice posted (accrual)
    DR Accounts Receivable (total incl. VAT)
        CR Sales Revenue (subtotal ex-VAT)
        CR VAT Payable (tax)
    DR COGS (cost)
        CR Inventory (cost)

Customer payment received
    DR Cash / Bank
        CR Accounts Receivable

Purchase invoice posted (accrual)
    DR Inventory (cost)
        CR Accounts Payable

Vendor payment made
    DR Accounts Payable
        CR Cash / Bank

Direct expense (no invoice)     DR Expense / CR Cash
Direct/other income (no sale)   DR Cash / CR Other Revenue
Sale return                     reverse of the sale (contra-revenue + COGS back)
Period close                    revenue & expense → Income Summary → Retained Earnings

Every high-level method is idempotent — re-posting the same source document is
a no-op — and each post runs inside a savepoint so a half-written, unbalanced
entry can never reach the ledger.
"""

import logging
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')

TWO = Decimal('0.01')


def _q(value):
    """Quantise to 2dp, banker-safe (half-up)."""
    return Decimal(str(value or '0')).quantize(TWO, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Standard chart-of-accounts keys → (code, name, type). Matches the CoA seeded
# in clients.signals.seed_tenant_after_schema_sync; any missing account is
# auto-created on first use so posting never fails on a fresh tenant.
# ---------------------------------------------------------------------------
ACCOUNTS = {
    'cash':               ('1001', 'النقدية والخزائن', 'asset'),
    'bank':               ('1002', 'البنك', 'asset'),
    'ar':                 ('1100', 'المدينون (ذمم العملاء)', 'asset'),
    'vat_input':          ('1250', 'ضريبة القيمة المضافة (مدخلات)', 'asset'),
    'inventory':          ('1200', 'المخزون', 'asset'),
    'ap':                 ('2100', 'الدائنون (ذمم الموردين)', 'liability'),
    'commission_payable': ('2110', 'عمولات مستحقة للموظفين', 'liability'),
    'vat_output':         ('2200', 'ضريبة القيمة المضافة المستحقة', 'liability'),
    'retained_earnings':  ('3100', 'الأرباح المحتجزة', 'equity'),
    'income_summary':     ('3900', 'ملخص الدخل (إقفال)', 'equity'),
    'sales_revenue':      ('4001', 'إيرادات المبيعات', 'revenue'),
    'service_revenue':    ('4002', 'إيرادات الخدمات', 'revenue'),
    'sales_returns':      ('4090', 'مردودات ومسموحات المبيعات', 'revenue'),
    'other_revenue':      ('4099', 'إيرادات أخرى', 'revenue'),
    'cogs':               ('5001', 'تكلفة البضاعة المباعة', 'expense'),
    'commission_expense': ('5210', 'عمولات الفنيين والبائعين', 'expense'),
    'general_expense':    ('5099', 'مصروفات عمومية', 'expense'),
}


class AccountingService:
    """Accrual double-entry posting engine (all methods are ``@staticmethod``)."""

    # ==================================================================
    # Account resolution
    # ==================================================================
    @staticmethod
    def account(key_or_code, name=None, account_type=None):
        """
        Resolve a ChartOfAccount. ``key_or_code`` may be a logical key from
        ACCOUNTS ('ar', 'cash', …) or a raw account code. Missing accounts are
        created on demand so a fresh/partly-seeded tenant can always post.
        """
        from inventory.models import ChartOfAccount

        if key_or_code in ACCOUNTS:
            code, default_name, default_type = ACCOUNTS[key_or_code]
        else:
            code = key_or_code
            default_name = name or key_or_code
            default_type = account_type or 'asset'

        obj, _created = ChartOfAccount.objects.get_or_create(
            code=code, defaults={'name': default_name, 'account_type': default_type},
        )
        return obj

    # ==================================================================
    # Period resolution / guard
    # ==================================================================
    @staticmethod
    def _resolve_period(on_date):
        """Return the AccountingPeriod covering ``on_date`` and refuse if closed."""
        from inventory.models import AccountingPeriod

        period = AccountingPeriod.for_date(on_date)
        if period and period.is_closed:
            raise ValidationError(
                _closed_period_msg(period)
            )
        return period

    # ==================================================================
    # Core: post a balanced journal entry
    # ==================================================================
    @staticmethod
    def post_journal(*, description, lines, date=None, journal_type='general',
                     reference='', source=None, created_by=None, status='posted'):
        """
        Create and post a balanced JournalEntry from ``lines``.

        Args:
            description: human-readable narration (البيان).
            lines: iterable of dicts:
                {'account': <key|code|ChartOfAccount>, 'debit': D, 'credit': D,
                 'description': str (optional)}
                Zero-amount lines are dropped. Each surviving line must be
                single-sided (debit XOR credit).
            date: posting date (defaults to today).
            journal_type: one of JournalEntry.JOURNAL_TYPES.
            reference: source reference string.
            source: optional model instance (SaleInvoice / PurchaseInvoice /
                FinancialTransaction) — linked on the header AND every line so
                legacy reports keyed on those FKs keep working.
            created_by: User.
            status: 'posted' (default) or 'draft'.

        Returns the JournalEntry, or None if there was nothing to post.
        Raises ValidationError if the entry does not balance or the period is
        closed.
        """
        from inventory.models import JournalEntry, AccountingEntry

        post_date = (date or timezone.now().date())
        if hasattr(post_date, 'date'):  # a datetime slipped in
            post_date = post_date.date()

        # Normalise + drop empty lines.
        norm = []
        for ln in lines:
            debit = _q(ln.get('debit', 0))
            credit = _q(ln.get('credit', 0))
            if debit == 0 and credit == 0:
                continue
            if debit > 0 and credit > 0:
                raise ValidationError("سطر القيد لا يمكن أن يكون مدين ودائن معاً.")
            acct = ln['account']
            if not hasattr(acct, 'pk'):
                acct = AccountingService.account(acct)
            norm.append({
                'account': acct,
                'debit': debit,
                'credit': credit,
                'description': ln.get('description') or description,
            })

        if not norm:
            return None

        total_debit = sum((l['debit'] for l in norm), Decimal('0'))
        total_credit = sum((l['credit'] for l in norm), Decimal('0'))
        if total_debit != total_credit:
            raise ValidationError(
                f"قيد غير متوازن: مدين={total_debit} دائن={total_credit} — {description}"
            )

        period = AccountingService._resolve_period(post_date)

        source_kwargs = AccountingService._source_kwargs(source)

        with transaction.atomic():
            je = JournalEntry.objects.create(
                date=post_date,
                journal_type=journal_type,
                reference=reference or '',
                description=description[:255],
                status=status,
                period=period,
                created_by=created_by,
                posted_at=timezone.now() if status == 'posted' else None,
                **source_kwargs,
            )
            je.number = f"JE-{post_date:%Y%m}-{je.pk:06d}"
            je.save(update_fields=['number'])

            entry_dt = timezone.make_aware(
                datetime.combine(post_date, time.min),
                timezone.get_current_timezone(),
            )
            line_source = AccountingService._line_source_kwargs(source)
            for ln in norm:
                entry = AccountingEntry(
                    journal_entry=je,
                    entry_date=entry_dt,
                    reference=je.number,
                    description=ln['description'][:255],
                    account=ln['account'],
                    debit=ln['debit'],
                    credit=ln['credit'],
                    created_by=created_by,
                    **line_source,
                )
                entry.clean()
                entry.save()

            je.assert_balanced()

        logger.info(
            "[JOURNAL] Posted %s (%s) — %s lines, total=%s",
            je.number, journal_type, len(norm), total_debit,
        )
        return je

    # ==================================================================
    # Reversal
    # ==================================================================
    @staticmethod
    def reverse_journal(journal_entry, *, date=None, created_by=None, reason=''):
        """Post a mirror-image entry that cancels ``journal_entry``."""
        from inventory.models import JournalEntry

        if journal_entry.status == 'reversed':
            logger.info("[JOURNAL] %s already reversed — skipping", journal_entry.number)
            return None

        lines = [
            {'account': l.account, 'debit': l.credit, 'credit': l.debit,
             'description': f"عكس: {l.description}"}
            for l in journal_entry.lines.all()
        ]
        rev = AccountingService.post_journal(
            description=(reason or f"عكس القيد {journal_entry.number}"),
            lines=lines,
            date=date,
            journal_type=journal_entry.journal_type,
            reference=journal_entry.reference,
            created_by=created_by,
        )
        if rev:
            rev.reversal_of = journal_entry
            rev.save(update_fields=['reversal_of'])
            JournalEntry.objects.filter(pk=journal_entry.pk).update(status='reversed')
        return rev

    # ==================================================================
    # High-level: Sale invoice accrual (revenue + COGS)
    # ==================================================================
    @staticmethod
    def post_sale_invoice(invoice, created_by=None):
        """
        Recognise revenue + COGS for a posted sale invoice (accrual basis).
        Handles returns (contra-revenue + inventory back). Idempotent.
        """
        from inventory.models import JournalEntry

        # Contract-covered work is not a receivable here — revenue for it is
        # tied to the maintenance contract, so skip the AR/revenue accrual to
        # avoid a phantom customer balance in the ledger.
        if getattr(invoice, 'maintenance_contract_id', None):
            return None

        # Idempotency — one sales JE per invoice.
        if JournalEntry.objects.filter(
            sale_invoice=invoice, journal_type='sales'
        ).exists():
            return None

        total = _q(invoice.total_amount)
        cost = _q(getattr(invoice, 'total_cost', 0))
        if total == 0 and cost == 0:
            return None

        tax_amount = AccountingService._invoice_tax(invoice)
        revenue_ex_tax = _q(total - tax_amount)
        is_return = bool(getattr(invoice, 'is_return', False))

        lines = []
        if is_return:
            # Reverse of a sale: contra-revenue + VAT reversal, credit AR.
            if revenue_ex_tax > 0:
                lines.append({'account': 'sales_returns', 'debit': revenue_ex_tax, 'credit': 0,
                              'description': f"مردودات فاتورة #{invoice.pk}"})
            if tax_amount > 0:
                lines.append({'account': 'vat_output', 'debit': tax_amount, 'credit': 0,
                              'description': "عكس ض.ق.م على المردود"})
            if total > 0:
                lines.append({'account': 'ar', 'debit': 0, 'credit': total,
                              'description': f"إلغاء مديونية مردود #{invoice.pk}"})
            # Inventory comes back, COGS reversed.
            if cost > 0:
                lines.append({'account': 'inventory', 'debit': cost, 'credit': 0,
                              'description': "رد بضاعة المردود للمخزون"})
                lines.append({'account': 'cogs', 'debit': 0, 'credit': cost,
                              'description': "عكس تكلفة بضاعة مباعة"})
            desc = f"مردود مبيعات #{invoice.pk} — {invoice.customer.name}"
        else:
            if total > 0:
                lines.append({'account': 'ar', 'debit': total, 'credit': 0,
                              'description': f"مديونية فاتورة #{invoice.pk}"})
            if revenue_ex_tax > 0:
                lines.append({'account': 'sales_revenue', 'debit': 0, 'credit': revenue_ex_tax,
                              'description': f"إيراد فاتورة #{invoice.pk}"})
            if tax_amount > 0:
                lines.append({'account': 'vat_output', 'debit': 0, 'credit': tax_amount,
                              'description': "ض.ق.م مستحقة على المبيعات"})
            if cost > 0:
                lines.append({'account': 'cogs', 'debit': cost, 'credit': 0,
                              'description': f"تكلفة بضاعة مباعة #{invoice.pk}"})
                lines.append({'account': 'inventory', 'debit': 0, 'credit': cost,
                              'description': "خصم قيمة المخزون المباع"})
            desc = f"فاتورة مبيعات #{invoice.pk} — {invoice.customer.name}"

        return AccountingService.post_journal(
            description=desc,
            lines=lines,
            date=getattr(invoice, 'date_created', None) or timezone.now(),
            journal_type='sales',
            reference=f"SINV-{invoice.pk}",
            source=invoice,
            created_by=created_by,
        )

    # ==================================================================
    # High-level: Purchase invoice accrual (inventory + payable)
    # ==================================================================
    @staticmethod
    def post_purchase_invoice(invoice, created_by=None):
        """Capitalise purchased goods to inventory against a payable. Idempotent."""
        from inventory.models import JournalEntry

        if JournalEntry.objects.filter(
            purchase_invoice=invoice, journal_type='purchase'
        ).exists():
            return None

        total = _q(invoice.total_amount)
        if total == 0:
            return None

        lines = [
            {'account': 'inventory', 'debit': total, 'credit': 0,
             'description': f"استلام بضاعة فاتورة شراء #{invoice.pk}"},
            {'account': 'ap', 'debit': 0, 'credit': total,
             'description': f"التزام للمورد {invoice.vendor.name}"},
        ]
        return AccountingService.post_journal(
            description=f"فاتورة مشتريات #{invoice.pk} — {invoice.vendor.name}",
            lines=lines,
            date=getattr(invoice, 'date_created', None) or timezone.now(),
            journal_type='purchase',
            reference=f"PINV-{invoice.pk}",
            source=invoice,
            created_by=created_by,
        )

    # ==================================================================
    # High-level: Payment / cash movement settlement
    # ==================================================================
    @staticmethod
    def post_payment(financial_transaction, created_by=None):
        """
        Post the ledger effect of a FinancialTransaction. Routes to the right
        counter-account: customer/vendor settlements hit AR/AP; standalone
        cash movements hit revenue/expense. Idempotent per transaction.
        """
        from inventory.models import JournalEntry

        ft = financial_transaction
        if JournalEntry.objects.filter(financial_transaction=ft).exists():
            return None

        amount = _q(ft.amount)
        if amount == 0:
            return None

        cash_key = AccountingService._cash_key_for(ft.treasury)
        is_in = (ft.transaction_type == 'in')

        # --- Customer settlement (payment for / refund on a sale) ----------
        if ft.sale_invoice_id or ft.customer_id:
            if is_in:  # customer pays us
                lines = [
                    {'account': cash_key, 'debit': amount, 'credit': 0},
                    {'account': 'ar', 'debit': 0, 'credit': amount},
                ]
                jtype = 'cash_receipt'
            else:  # refund to customer
                lines = [
                    {'account': 'ar', 'debit': amount, 'credit': 0},
                    {'account': cash_key, 'debit': 0, 'credit': amount},
                ]
                jtype = 'cash_payment'
            desc = ft.description or 'تحصيل/رد عميل'

        # --- Vendor settlement (payment for / refund on a purchase) --------
        elif ft.purchase_invoice_id or ft.vendor_id:
            if is_in:  # refund from vendor
                lines = [
                    {'account': cash_key, 'debit': amount, 'credit': 0},
                    {'account': 'ap', 'debit': 0, 'credit': amount},
                ]
                jtype = 'cash_receipt'
            else:  # we pay the vendor
                lines = [
                    {'account': 'ap', 'debit': amount, 'credit': 0},
                    {'account': cash_key, 'debit': 0, 'credit': amount},
                ]
                jtype = 'cash_payment'
            desc = ft.description or 'سداد/استرداد مورد'

        # --- Standalone cash movement (direct expense / other income) ------
        else:
            if is_in:
                lines = [
                    {'account': cash_key, 'debit': amount, 'credit': 0},
                    {'account': 'other_revenue', 'debit': 0, 'credit': amount},
                ]
                jtype = 'cash_receipt'
                desc = ft.description or 'إيراد نقدي'
            else:
                expense_acct = AccountingService._expense_account_for(ft)
                lines = [
                    {'account': expense_acct, 'debit': amount, 'credit': 0},
                    {'account': cash_key, 'debit': 0, 'credit': amount},
                ]
                jtype = 'cash_payment'
                desc = ft.description or 'مصروف نقدي'

        return AccountingService.post_journal(
            description=desc,
            lines=lines,
            date=getattr(ft, 'date', None) or timezone.now(),
            journal_type=jtype,
            reference=f"FT-{ft.pk}",
            source=ft,
            created_by=created_by,
        )

    # ==================================================================
    # Period close (قيد الإقفال)
    # ==================================================================
    @staticmethod
    def close_period(period, created_by=None):
        """
        Close a period: sweep every revenue and expense account's net balance
        into Income Summary, then Income Summary into Retained Earnings, and
        mark the period closed. Returns the closing JournalEntry (or None if
        there was no P&L activity). Refuses if already closed.
        """
        from inventory.models import ChartOfAccount, AccountingEntry
        from django.db.models import Sum

        if period.is_closed:
            raise ValidationError("الفترة مُقفلة بالفعل.")

        income_summary = AccountingService.account('income_summary')
        retained = AccountingService.account('retained_earnings')

        lines = []
        net_income = Decimal('0.00')

        pnl_accounts = ChartOfAccount.objects.filter(
            account_type__in=('revenue', 'expense'), is_active=True,
        )
        for acct in pnl_accounts:
            agg = AccountingEntry.objects.filter(
                account=acct,
                entry_date__date__gte=period.start_date,
                entry_date__date__lte=period.end_date,
            ).aggregate(d=Sum('debit'), c=Sum('credit'))
            d = agg['d'] or Decimal('0.00')
            c = agg['c'] or Decimal('0.00')
            if acct.account_type == 'revenue':
                bal = c - d  # credit-normal
                if bal != 0:
                    # Close revenue: debit revenue, credit income summary
                    lines.append({'account': acct, 'debit': _q(bal) if bal > 0 else 0,
                                  'credit': _q(-bal) if bal < 0 else 0,
                                  'description': f"إقفال {acct.name}"})
                    net_income += bal
            else:  # expense — debit-normal
                bal = d - c
                if bal != 0:
                    # Close expense: credit expense, debit income summary
                    lines.append({'account': acct, 'debit': _q(-bal) if bal < 0 else 0,
                                  'credit': _q(bal) if bal > 0 else 0,
                                  'description': f"إقفال {acct.name}"})
                    net_income -= bal

        if not lines:
            # Nothing to close but still lock the period.
            period.is_closed = True
            period.closed_at = timezone.now()
            period.closed_by = created_by
            period.save(update_fields=['is_closed', 'closed_at', 'closed_by'])
            return None

        # Balancing leg: Income Summary absorbs the net, then rolls to equity.
        net_income = _q(net_income)
        if net_income > 0:  # profit → credit income summary here, then move to RE
            lines.append({'account': income_summary, 'debit': 0, 'credit': net_income,
                          'description': "ملخص الدخل — صافي الربح"})
        elif net_income < 0:  # loss
            lines.append({'account': income_summary, 'debit': -net_income, 'credit': 0,
                          'description': "ملخص الدخل — صافي الخسارة"})

        with transaction.atomic():
            je = AccountingService.post_journal(
                description=f"قيد إقفال الفترة — {period.name}",
                lines=lines,
                date=period.end_date,
                journal_type='closing',
                reference=f"CLOSE-{period.pk}",
                created_by=created_by,
            )
            # Roll Income Summary into Retained Earnings.
            if net_income != 0:
                if net_income > 0:
                    re_lines = [
                        {'account': income_summary, 'debit': net_income, 'credit': 0,
                         'description': "ترحيل صافي الربح"},
                        {'account': retained, 'debit': 0, 'credit': net_income,
                         'description': "إلى الأرباح المحتجزة"},
                    ]
                else:
                    re_lines = [
                        {'account': retained, 'debit': -net_income, 'credit': 0,
                         'description': "تحميل الخسارة على الأرباح المحتجزة"},
                        {'account': income_summary, 'debit': 0, 'credit': -net_income,
                         'description': "تصفية ملخص الدخل"},
                    ]
                AccountingService.post_journal(
                    description=f"ترحيل نتيجة الفترة إلى الأرباح المحتجزة — {period.name}",
                    lines=re_lines,
                    date=period.end_date,
                    journal_type='closing',
                    reference=f"CLOSE-RE-{period.pk}",
                    created_by=created_by,
                )

            period.is_closed = True
            period.closed_at = timezone.now()
            period.closed_by = created_by
            period.save(update_fields=['is_closed', 'closed_at', 'closed_by'])

        logger.info("[CLOSE] Period %s closed — net income=%s", period.name, net_income)
        return je

    # ==================================================================
    # Opening balances
    # ==================================================================
    @staticmethod
    def post_opening_balances(entries, *, date=None, created_by=None, description=None):
        """
        Post an opening-balance journal. ``entries`` is a list of
        {'account': key|code, 'debit': D, 'credit': D}. Must balance.
        """
        return AccountingService.post_journal(
            description=description or "الأرصدة الافتتاحية",
            lines=entries,
            date=date,
            journal_type='opening',
            reference='OPENING',
            created_by=created_by,
        )

    # ==================================================================
    # Internal helpers
    # ==================================================================
    @staticmethod
    def _invoice_tax(invoice):
        """VAT portion of a sale invoice's total (total is VAT-inclusive)."""
        rate = Decimal(str(getattr(invoice, 'tax_percentage', 0) or 0))
        total = _q(invoice.total_amount)
        if rate <= 0 or total == 0:
            return Decimal('0.00')
        subtotal = (total / (Decimal('1') + rate / Decimal('100')))
        return _q(total - subtotal)

    @staticmethod
    def _cash_key_for(treasury):
        """Pick cash vs bank ledger account from the treasury type."""
        t = getattr(treasury, 'type', 'cash')
        return 'bank' if t in ('bank', 'visa') else 'cash'

    @staticmethod
    def _expense_account_for(ft):
        """Resolve the expense account for a categorised direct payment."""
        category = getattr(ft, 'category', None)
        if category is not None:
            return AccountingService.account(
                f'5{category.pk:03d}', f'مصروفات — {category.name}', 'expense',
            )
        return AccountingService.account('general_expense')

    @staticmethod
    def _source_kwargs(source):
        """Header FK kwargs for the JournalEntry from a source instance."""
        if source is None:
            return {}
        cls = source.__class__.__name__
        if cls == 'SaleInvoice':
            return {'sale_invoice': source}
        if cls == 'PurchaseInvoice':
            return {'purchase_invoice': source}
        if cls == 'FinancialTransaction':
            return {'financial_transaction': source}
        return {}

    @staticmethod
    def _line_source_kwargs(source):
        """Legacy line-level FK kwargs so old reports keyed on them still work."""
        if source is None:
            return {}
        cls = source.__class__.__name__
        if cls == 'SaleInvoice':
            return {'sale_invoice': source}
        if cls == 'PurchaseInvoice':
            return {'purchase_invoice': source}
        if cls == 'FinancialTransaction':
            return {'financial_transaction': source}
        return {}


def _closed_period_msg(period):
    return f"الفترة المحاسبية «{period.name}» مُقفلة — لا يمكن الترحيل فيها."
