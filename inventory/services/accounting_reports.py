"""
📊 AccountingReportService — financial statements from the general ledger.

Pure read-side. Every figure is derived from posted ``AccountingEntry`` rows,
so the statements always tie back to the ledger (and to each other):

    Trial Balance        — every account's debit/credit balance; must net to 0
    General Ledger       — line-by-line movement of one account with running bal
    Income Statement     — revenue − expenses = net profit (قائمة الدخل)
    Balance Sheet        — assets = liabilities + equity (الميزانية العمومية)
    Cash Flow            — movement across cash & bank accounts (التدفقات النقدية)
    AR / AP Aging        — receivables & payables bucketed by age

All amounts are Decimal; callers format for display.
"""

from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

ZERO = Decimal('0.00')

# Debit-normal account types (assets, expenses). Everything else is credit-normal.
_DEBIT_NORMAL = ('asset', 'expense')


def _account_balance(agg_debit, agg_credit, account_type):
    d = agg_debit or ZERO
    c = agg_credit or ZERO
    if account_type in _DEBIT_NORMAL:
        return d - c
    return c - d


class AccountingReportService:
    """All financial statements (static, read-only)."""

    # ------------------------------------------------------------------
    @staticmethod
    def _entries(as_of=None, date_from=None, date_to=None, account=None):
        from inventory.models import AccountingEntry
        qs = AccountingEntry.objects.all()
        if account is not None:
            qs = qs.filter(account=account)
        if date_from is not None:
            qs = qs.filter(entry_date__date__gte=date_from)
        if date_to is not None:
            qs = qs.filter(entry_date__date__lte=date_to)
        if as_of is not None:
            qs = qs.filter(entry_date__date__lte=as_of)
        return qs

    # ==================================================================
    # Trial balance
    # ==================================================================
    @staticmethod
    def trial_balance(as_of=None):
        from inventory.models import ChartOfAccount

        as_of = as_of or timezone.now().date()
        rows, total_debit, total_credit = [], ZERO, ZERO

        for acct in ChartOfAccount.objects.filter(is_active=True).order_by('code'):
            agg = AccountingReportService._entries(as_of=as_of, account=acct).aggregate(
                d=Sum('debit'), c=Sum('credit'),
            )
            bal = _account_balance(agg['d'], agg['c'], acct.account_type)
            if bal == 0:
                continue
            debit = bal if bal > 0 else ZERO
            credit = -bal if bal < 0 else ZERO
            # Present against the account's normal side.
            if acct.account_type in _DEBIT_NORMAL:
                row_debit, row_credit = (bal if bal > 0 else ZERO, -bal if bal < 0 else ZERO)
            else:
                row_credit, row_debit = (bal if bal > 0 else ZERO, -bal if bal < 0 else ZERO)
            rows.append({
                'code': acct.code, 'name': acct.name, 'type': acct.account_type,
                'debit': row_debit, 'credit': row_credit,
            })
            total_debit += row_debit
            total_credit += row_credit

        return {
            'as_of': str(as_of),
            'rows': rows,
            'total_debit': total_debit,
            'total_credit': total_credit,
            'is_balanced': abs(total_debit - total_credit) < Decimal('0.01'),
        }

    # ==================================================================
    # General ledger (one account, running balance)
    # ==================================================================
    @staticmethod
    def general_ledger(account, date_from=None, date_to=None):
        from inventory.models import ChartOfAccount

        if not hasattr(account, 'pk'):
            account = ChartOfAccount.objects.get(code=account)

        # Opening balance = everything strictly before date_from.
        opening = ZERO
        if date_from is not None:
            agg = AccountingReportService._entries(
                account=account, date_to=None,
            ).filter(entry_date__date__lt=date_from).aggregate(d=Sum('debit'), c=Sum('credit'))
            opening = _account_balance(agg['d'], agg['c'], account.account_type)

        qs = AccountingReportService._entries(
            account=account, date_from=date_from, date_to=date_to,
        ).order_by('entry_date', 'pk')

        running = opening
        lines = []
        for e in qs:
            if account.account_type in _DEBIT_NORMAL:
                running += (e.debit - e.credit)
            else:
                running += (e.credit - e.debit)
            lines.append({
                'date': e.entry_date.date().isoformat(),
                'reference': e.reference,
                'description': e.description,
                'debit': e.debit, 'credit': e.credit,
                'balance': running,
                'journal_entry': e.journal_entry_id,
            })

        return {
            'account': {'code': account.code, 'name': account.name, 'type': account.account_type},
            'opening_balance': opening,
            'closing_balance': running,
            'lines': lines,
        }

    # ==================================================================
    # Income statement (P&L)
    # ==================================================================
    @staticmethod
    def income_statement(date_from, date_to):
        from inventory.models import ChartOfAccount

        def section(acc_type):
            items, total = [], ZERO
            for acct in ChartOfAccount.objects.filter(
                account_type=acc_type, is_active=True,
            ).order_by('code'):
                agg = AccountingReportService._entries(
                    date_from=date_from, date_to=date_to, account=acct,
                ).aggregate(d=Sum('debit'), c=Sum('credit'))
                bal = _account_balance(agg['d'], agg['c'], acc_type)
                if bal != 0:
                    items.append({'code': acct.code, 'name': acct.name, 'amount': bal})
                    total += bal
            return items, total

        revenue_items, total_revenue = section('revenue')
        expense_items, total_expenses = section('expense')
        net_profit = total_revenue - total_expenses

        return {
            'date_from': str(date_from), 'date_to': str(date_to),
            'revenue': {'items': revenue_items, 'total': total_revenue},
            'expenses': {'items': expense_items, 'total': total_expenses},
            'net_profit': net_profit,
            'is_profit': net_profit >= 0,
        }

    # ==================================================================
    # Balance sheet
    # ==================================================================
    @staticmethod
    def balance_sheet(as_of=None):
        from inventory.models import ChartOfAccount

        as_of = as_of or timezone.now().date()

        def section(acc_type):
            items, total = [], ZERO
            for acct in ChartOfAccount.objects.filter(
                account_type=acc_type, is_active=True,
            ).order_by('code'):
                agg = AccountingReportService._entries(as_of=as_of, account=acct).aggregate(
                    d=Sum('debit'), c=Sum('credit'),
                )
                bal = _account_balance(agg['d'], agg['c'], acc_type)
                if bal != 0:
                    items.append({'code': acct.code, 'name': acct.name, 'amount': bal})
                    total += bal
            return items, total

        assets, total_assets = section('asset')
        liabilities, total_liabilities = section('liability')
        equity, total_equity = section('equity')

        # Undistributed profit (revenue − expense not yet closed to equity).
        _, total_revenue = section('revenue')
        _, total_expense = section('expense')
        retained_current = total_revenue - total_expense
        equity_total = total_equity + retained_current

        return {
            'as_of': str(as_of),
            'assets': {'items': assets, 'total': total_assets},
            'liabilities': {'items': liabilities, 'total': total_liabilities},
            'equity': {
                'items': equity,
                'current_period_result': retained_current,
                'total': equity_total,
            },
            'total_assets': total_assets,
            'total_liabilities_and_equity': total_liabilities + equity_total,
            'is_balanced': abs(total_assets - (total_liabilities + equity_total)) < Decimal('0.01'),
        }

    # ==================================================================
    # Cash flow (movement across cash & bank accounts)
    # ==================================================================
    @staticmethod
    def cash_flow(date_from, date_to):
        from inventory.models import ChartOfAccount
        from inventory.services.accounting_service import ACCOUNTS

        cash_codes = [ACCOUNTS['cash'][0], ACCOUNTS['bank'][0]]
        cash_accounts = ChartOfAccount.objects.filter(code__in=cash_codes)

        inflow, outflow = ZERO, ZERO
        opening = ZERO
        for acct in cash_accounts:
            before = AccountingReportService._entries(account=acct).filter(
                entry_date__date__lt=date_from,
            ).aggregate(d=Sum('debit'), c=Sum('credit'))
            opening += (before['d'] or ZERO) - (before['c'] or ZERO)

            period = AccountingReportService._entries(
                account=acct, date_from=date_from, date_to=date_to,
            ).aggregate(d=Sum('debit'), c=Sum('credit'))
            inflow += (period['d'] or ZERO)
            outflow += (period['c'] or ZERO)

        net = inflow - outflow
        return {
            'date_from': str(date_from), 'date_to': str(date_to),
            'opening_cash': opening,
            'inflow': inflow,
            'outflow': outflow,
            'net_cash_flow': net,
            'closing_cash': opening + net,
        }

    # ==================================================================
    # AR / AP aging
    # ==================================================================
    @staticmethod
    def receivables_aging(as_of=None):
        return AccountingReportService._aging('customer', as_of)

    @staticmethod
    def payables_aging(as_of=None):
        return AccountingReportService._aging('vendor', as_of)

    @staticmethod
    def _aging(party, as_of):
        """
        Age open balances from the subsidiary ledger (Customer/Vendor.balance).
        Buckets by the party's most recent open invoice date.
        """
        from datetime import timedelta
        from inventory.models import Customer, Vendor, SaleInvoice, PurchaseInvoice

        as_of = as_of or timezone.now().date()
        buckets = {'current': ZERO, '1_30': ZERO, '31_60': ZERO, '61_90': ZERO, 'over_90': ZERO}
        rows = []

        if party == 'customer':
            qs = Customer.objects.filter(balance__gt=0)
        else:
            qs = Vendor.objects.filter(balance__gt=0)

        for entity in qs:
            bal = Decimal(str(entity.balance))
            if party == 'customer':
                last = (SaleInvoice.objects.filter(customer=entity, status='posted')
                        .order_by('-date_created').values_list('date_created', flat=True).first())
            else:
                last = (PurchaseInvoice.objects.filter(vendor=entity, status='posted')
                        .order_by('-date_created').values_list('date_created', flat=True).first())
            age_days = (as_of - last.date()).days if last else 0
            if age_days <= 0:
                bucket = 'current'
            elif age_days <= 30:
                bucket = '1_30'
            elif age_days <= 60:
                bucket = '31_60'
            elif age_days <= 90:
                bucket = '61_90'
            else:
                bucket = 'over_90'
            buckets[bucket] += bal
            rows.append({
                'name': entity.name, 'balance': bal,
                'age_days': age_days, 'bucket': bucket,
            })

        return {
            'as_of': str(as_of),
            'party': party,
            'buckets': buckets,
            'total': sum(buckets.values(), ZERO),
            'rows': rows,
        }
