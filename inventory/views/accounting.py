"""
🏛️ Accounting API views — financial statements over the accrual ledger.

Thin HTTP adapters over ``AccountingReportService`` / ``AccountingService``.
Each returns JSON; all numbers are floated at the edge for the frontend.
Access is restricted to admin + manager + accountant roles.
"""

import logging
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.utils import timezone

from .utils import tenant_required, role_required, _json_response_safe

logger = logging.getLogger('mouss_tec_core')


def _f(value):
    """Decimal/None → float for JSON."""
    return float(value or 0)


def _parse_date(raw, default):
    if not raw:
        return default
    try:
        return timezone.datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return None


def _walk_floats(obj):
    """Recursively convert Decimals to floats inside dict/list structures."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _walk_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_floats(v) for v in obj]
    return obj


# =====================================================================
# 📊 Income statement (قائمة الدخل)
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def income_statement_api(request):
    from inventory.services.accounting_reports import AccountingReportService

    today = timezone.now().date()
    default_from = today.replace(day=1)
    date_from = _parse_date(request.GET.get('from'), default_from)
    date_to = _parse_date(request.GET.get('to'), today)
    if date_from is None or date_to is None:
        return _json_response_safe({'error': 'تنسيق تاريخ خاطئ (YYYY-MM-DD)'}, 400)

    data = AccountingReportService.income_statement(date_from, date_to)
    return _json_response_safe({'status': 'success', **_walk_floats(data)})


# =====================================================================
# 🏦 Balance sheet (الميزانية العمومية) — accrual
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def balance_sheet_v2_api(request):
    from inventory.services.accounting_reports import AccountingReportService

    as_of = _parse_date(request.GET.get('as_of'), timezone.now().date())
    if as_of is None:
        return _json_response_safe({'error': 'تنسيق تاريخ خاطئ (YYYY-MM-DD)'}, 400)
    data = AccountingReportService.balance_sheet(as_of)
    return _json_response_safe({'status': 'success', **_walk_floats(data)})


# =====================================================================
# 📒 Trial balance (ميزان المراجعة) — accrual service
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def trial_balance_v2_api(request):
    from inventory.services.accounting_reports import AccountingReportService

    as_of = _parse_date(request.GET.get('as_of'), timezone.now().date())
    if as_of is None:
        return _json_response_safe({'error': 'تنسيق تاريخ خاطئ (YYYY-MM-DD)'}, 400)
    data = AccountingReportService.trial_balance(as_of)
    return _json_response_safe({'status': 'success', **_walk_floats(data)})


# =====================================================================
# 💵 Cash flow (قائمة التدفقات النقدية)
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def cash_flow_api(request):
    from inventory.services.accounting_reports import AccountingReportService

    today = timezone.now().date()
    date_from = _parse_date(request.GET.get('from'), today.replace(day=1))
    date_to = _parse_date(request.GET.get('to'), today)
    if date_from is None or date_to is None:
        return _json_response_safe({'error': 'تنسيق تاريخ خاطئ (YYYY-MM-DD)'}, 400)
    data = AccountingReportService.cash_flow(date_from, date_to)
    return _json_response_safe({'status': 'success', **_walk_floats(data)})


# =====================================================================
# 📖 General ledger (دفتر الأستاذ لحساب واحد)
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def general_ledger_api(request, code):
    from inventory.models import ChartOfAccount
    from inventory.services.accounting_reports import AccountingReportService

    account = ChartOfAccount.objects.filter(code=code).first()
    if not account:
        return _json_response_safe({'error': 'حساب غير موجود'}, 404)

    date_from = _parse_date(request.GET.get('from'), None)
    date_to = _parse_date(request.GET.get('to'), timezone.now().date())
    data = AccountingReportService.general_ledger(account, date_from, date_to)
    return _json_response_safe({'status': 'success', **_walk_floats(data)})


# =====================================================================
# ⏳ AR / AP aging (أعمار الذمم)
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def receivables_aging_api(request):
    from inventory.services.accounting_reports import AccountingReportService

    as_of = _parse_date(request.GET.get('as_of'), timezone.now().date())
    data = AccountingReportService.receivables_aging(as_of)
    return _json_response_safe({'status': 'success', **_walk_floats(data)})


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def payables_aging_api(request):
    from inventory.services.accounting_reports import AccountingReportService

    as_of = _parse_date(request.GET.get('as_of'), timezone.now().date())
    data = AccountingReportService.payables_aging(as_of)
    return _json_response_safe({'status': 'success', **_walk_floats(data)})


# =====================================================================
# 📓 Journal entries list (قيود اليومية)
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
def journal_entries_api(request):
    from inventory.models import JournalEntry

    qs = JournalEntry.objects.all().order_by('-date', '-id')
    jtype = request.GET.get('type')
    if jtype:
        qs = qs.filter(journal_type=jtype)
    date_from = _parse_date(request.GET.get('from'), None)
    date_to = _parse_date(request.GET.get('to'), None)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)

    try:
        limit = min(int(request.GET.get('limit', 100)), 500)
    except (TypeError, ValueError):
        limit = 100

    entries = []
    for je in qs.prefetch_related('lines__account')[:limit]:
        entries.append({
            'number': je.number,
            'date': je.date.isoformat(),
            'type': je.journal_type,
            'description': je.description,
            'reference': je.reference,
            'status': je.status,
            'total_debit': _f(je.total_debit),
            'total_credit': _f(je.total_credit),
            'is_balanced': je.is_balanced,
            'lines': [
                {
                    'account_code': ln.account.code,
                    'account_name': ln.account.name,
                    'debit': _f(ln.debit),
                    'credit': _f(ln.credit),
                    'description': ln.description,
                }
                for ln in je.lines.all()
            ],
        })
    return _json_response_safe({'status': 'success', 'count': len(entries), 'entries': entries})
