"""🏛️ المحاسبة: إقفال الشهور بالترتيب · سداد ضريبة القيمة المضافة · دفتر الأستاذ لكل حساب.

قبل الشاشة دي الإقفال كان من لوحة الأدمن الخام بس (من غير ما يتعمل سنة مالية،
ومن غير أي شرط على ترتيب الشهور)، والضريبة اللي بتتحصّل على الفواتير كانت بتتراكم
في «ض.ق.م المستحقة» من غير أي طريقة لسدادها غير إنها تتسجّل مصروف (فتقلّل الربح
غلط)، وميزان المراجعة مالوش تفاصيل لكل حساب.

جزء من inventory/views_lightning.
"""
import calendar
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from inventory.models import (
    AccountingEntry, AccountingPeriod, ChartOfAccount, FinancialTransaction, FiscalYear,
    JournalEntry, Treasury,
)
from inventory.report_export import export_report
from inventory.services.accounting_service import ACCOUNTS, AccountingService, period_close_problem
from inventory.views import _get_branch_for_user, tenant_required
from inventory.views.utils import _user_can_edit_branch, module_required, role_required

ZERO = Decimal('0.00')
MONTHS_AR = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
             'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']
VAT_CODE = ACCOUNTS['vat_output'][0]


def _back(anchor='', **params):
    url = reverse('inventory:accounting_center')
    if params:
        url += '?' + urlencode(params)
    return redirect(url + (f'#{anchor}' if anchor else ''))


def _vat_due():
    """الضريبة اللي اتحصّلت ولسه ماتسدّدتش = الرصيد الدائن لحساب ض.ق.م المستحقة."""
    agg = AccountingEntry.objects.filter(account__code=VAT_CODE).aggregate(d=Sum('debit'), c=Sum('credit'))
    return (agg['c'] or ZERO) - (agg['d'] or ZERO)


def _period_rows(today, user):
    """كل الفترات (الأحدث الأول) مع صافي ربح كل شهر من الدفتر وإيه اللي ينفع يتعمل فيها."""
    periods = list(AccountingPeriod.objects.select_related('fiscal_year', 'closed_by')
                   .order_by('-start_date'))
    pnl = (AccountingEntry.objects
           .exclude(journal_entry__journal_type='closing')
           .filter(account__account_type__in=('revenue', 'expense')))
    last_closed = next((p for p in periods if p.is_closed), None)
    first_open_ended = None
    for p in reversed(periods):   # من الأقدم
        if not p.is_closed:
            first_open_ended = p if p.end_date < today else None
            break
    rows = []
    for p in periods:
        agg = (pnl.filter(entry_date__date__gte=p.start_date, entry_date__date__lte=p.end_date)
               .aggregate(rd=Sum('debit', filter=Q(account__account_type='revenue')),
                          rc=Sum('credit', filter=Q(account__account_type='revenue')),
                          ed=Sum('debit', filter=Q(account__account_type='expense')),
                          ec=Sum('credit', filter=Q(account__account_type='expense'))))
        revenue = (agg['rc'] or ZERO) - (agg['rd'] or ZERO)
        expenses = (agg['ed'] or ZERO) - (agg['ec'] or ZERO)
        ended = p.end_date < today
        earlier_open = (not p.is_closed and first_open_ended is not None
                        and p.start_date > first_open_ended.start_date)
        rows.append({
            'p': p, 'revenue': revenue, 'expenses': expenses, 'net': revenue - expenses,
            'ended': ended, 'current': p.start_date <= today <= p.end_date,
            # «اقفل لحد هنا» بيقفل الشهور المفتوحة اللي قبله بالترتيب
            'can_close': not p.is_closed and ended,
            'closes_earlier': earlier_open,
            'can_reopen': p.is_closed and last_closed is not None and p.pk == last_closed.pk
                          and user.is_superuser,
        })
    return rows


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def accounting_center(request):
    """🏛️ الإقفال الشهري + ضريبة القيمة المضافة + روابط القوائم المالية."""
    today = timezone.localdate()
    branch = _get_branch_for_user(request.user)
    treasuries = Treasury.objects.filter(is_active=True).select_related('branch').order_by('branch__name', 'name')
    if branch is not None:
        treasuries = treasuries.filter(branch=branch)
    month_start = today.replace(day=1)
    vat_this_month = (AccountingEntry.objects.filter(account__code=VAT_CODE, entry_date__date__gte=month_start)
                      .exclude(journal_entry__financial_transaction__equity_kind='vat')
                      .aggregate(d=Sum('debit'), c=Sum('credit')))
    years = list(FiscalYear.objects.order_by('-start_date').values_list('start_date__year', flat=True))
    return render(request, 'inventory/accounting_center.html', {
        'today': today,
        'periods': _period_rows(today, request.user),
        'has_current_year': FiscalYear.objects.filter(start_date__lte=today, end_date__gte=today).exists(),
        'years': years,
        'next_year': today.year,
        'vat_due': _vat_due(),
        'vat_this_month': (vat_this_month['c'] or ZERO) - (vat_this_month['d'] or ZERO),
        'vat_payments': (FinancialTransaction.objects.filter(equity_kind='vat')
                         .select_related('treasury').order_by('-date', '-id')[:10]),
        'treasuries': [t for t in treasuries if _user_can_edit_branch(request.user, t.branch)],
        'flash': request.GET.get('ok'), 'err': request.GET.get('err'), 'msg': request.GET.get('msg', ''),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
@require_POST
def accounting_periods_action(request):
    """سنة مالية جديدة (١٢ شهر) / إقفال لحد شهر معيّن بالترتيب / إعادة فتح آخر شهر اتقفل."""
    action = request.POST.get('action')
    today = timezone.localdate()

    if action == 'create_year':
        try:
            year = int(request.POST.get('year') or 0)
        except ValueError:
            year = 0
        if not 2000 <= year <= today.year + 1:
            return _back('periods', err='year')
        start, end = date(year, 1, 1), date(year, 12, 31)
        if FiscalYear.objects.filter(start_date__lte=end, end_date__gte=start).exists():
            return _back('periods', err='year_exists')
        with transaction.atomic():
            fy = FiscalYear.objects.create(code=str(year), name=f'السنة المالية {year}',
                                           start_date=start, end_date=end)
            for m in range(1, 13):
                AccountingPeriod.objects.create(
                    fiscal_year=fy, name=f'{MONTHS_AR[m - 1]} {year}',
                    start_date=date(year, m, 1), end_date=date(year, m, calendar.monthrange(year, m)[1]))
        return _back('periods', ok='year')

    period = get_object_or_404(AccountingPeriod, pk=request.POST.get('period'))

    if action == 'close':
        if period.is_closed:
            return _back('periods', ok='closed')
        if period.end_date >= today:
            return _back('periods', err='not_ended')
        # الشهور المفتوحة اللي قبله بتتقفل الأول بالترتيب (لحد الشهر المختار)
        targets = list(AccountingPeriod.objects.filter(is_closed=False, end_date__lte=period.end_date)
                       .order_by('start_date'))
        for p in targets:
            problem = period_close_problem(p, today=today)
            if problem:
                return _back('periods', err='msg', msg=problem)
            try:
                AccountingService.close_period(p, created_by=request.user)
            except ValidationError as exc:
                return _back('periods', err='msg', msg='؛ '.join(exc.messages))
        return _back('periods', ok='closed')

    if action == 'reopen':
        # إعادة الفتح بتغيّر أرقام اتقفلت واتسلّمت — للمالك بس، ومن الأحدث للأقدم.
        if not request.user.is_superuser:
            return HttpResponseForbidden("إعادة فتح فترة مقفولة للمالك بس.")
        if not period.is_closed:
            return _back('periods')
        if AccountingPeriod.objects.filter(start_date__gt=period.end_date, is_closed=True).exists():
            return _back('periods', err='reopen_order')
        with transaction.atomic():
            JournalEntry.objects.filter(
                reference__in=[f'CLOSE-{period.pk}', f'CLOSE-RE-{period.pk}'], journal_type='closing').delete()
            AccountingPeriod.objects.filter(pk=period.pk).update(is_closed=False, closed_at=None, closed_by=None)
        return _back('periods', ok='reopened')

    return _back('periods', err='action')


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
@require_POST
def vat_payment(request):
    """💸 سداد ضريبة القيمة المضافة للضرايب من خزنة.

    بيقفل جزء من الالتزام (مدين ٢٢٠٠ / دائن الخزنة) — مش مصروف، فمابيقلّلش الربح
    ومابيظهرش في قايمة المصاريف. مايتعدّاش اللي اتحصّل فعلاً ولسه ماتسدّدش.
    """
    treasury = Treasury.objects.filter(pk=request.POST.get('treasury_id'), is_active=True).first()
    if treasury is None:
        return _back('vat', err='treasury')
    if not _user_can_edit_branch(request.user, treasury.branch):
        return _back('vat', err='perm')
    try:
        amount = Decimal(str(request.POST.get('amount') or '0').replace(',', ''))
    except InvalidOperation:
        amount = Decimal('0')
    if not amount.is_finite() or amount <= 0:
        return _back('vat', err='amount')
    amount = amount.quantize(Decimal('0.01'))
    note = (request.POST.get('description') or '').strip()
    desc = ('سداد ضريبة القيمة المضافة' + (f' — {note}' if note else ''))[:255]
    with transaction.atomic():
        # قفل حساب الضريبة: سدادين في نفس اللحظة من خزنتين مايعدّوش اللي اتحصّل
        ChartOfAccount.objects.select_for_update().filter(code=VAT_CODE).first()
        locked = Treasury.objects.select_for_update().get(pk=treasury.pk)
        if amount > _vat_due():
            return _back('vat', err='over')
        if (locked.balance or ZERO) < amount:
            return _back('vat', err='balance')
        FinancialTransaction.objects.create(
            treasury=locked, transaction_type='out', amount=amount,
            description=desc, equity_kind='vat')
    return _back('vat', ok='vat_paid')


def _source_link(entry):
    """رابط المستند اللي طلع منه سطر القيد (فاتورة بيع/شراء/حركة خزنة)."""
    je = entry.journal_entry
    sale_id = entry.sale_invoice_id or (je.sale_invoice_id if je else None)
    if sale_id:
        return reverse('inventory:print_invoice_a4', args=[sale_id]), f'فاتورة بيع #{sale_id}'
    purchase_id = entry.purchase_invoice_id or (je.purchase_invoice_id if je else None)
    if purchase_id:
        return reverse('inventory:purchase_edit', args=[purchase_id]), f'فاتورة شراء #{purchase_id}'
    ft = entry.financial_transaction or (je.financial_transaction if je else None)
    if ft is not None:
        return reverse('inventory:treasury_detail', args=[ft.treasury_id]), f'حركة خزنة #{ft.pk}'
    return None, ''


def _parse_day(raw):
    try:
        return datetime.strptime((raw or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def account_ledger(request, code):
    """📒 دفتر الأستاذ لحساب واحد: رصيد أول المدة + كل السطور برصيد جاري + رابط مصدر كل سطر."""
    account = get_object_or_404(ChartOfAccount, code=code)
    today = timezone.localdate()
    d_from = _parse_day(request.GET.get('from')) or today.replace(day=1)
    d_to = _parse_day(request.GET.get('to')) or today
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    branch = _get_branch_for_user(request.user)
    qs = AccountingEntry.objects.filter(account=account)
    if branch is not None:
        qs = qs.filter(journal_entry__branch=branch)
    debit_normal = account.account_type in ('asset', 'expense')

    def signed(d, c):
        d, c = d or ZERO, c or ZERO
        return d - c if debit_normal else c - d

    before = qs.filter(entry_date__date__lt=d_from).aggregate(d=Sum('debit'), c=Sum('credit'))
    opening = signed(before['d'], before['c'])
    lines = (qs.filter(entry_date__date__gte=d_from, entry_date__date__lte=d_to)
             .select_related('journal_entry', 'journal_entry__financial_transaction', 'financial_transaction')
             .order_by('entry_date', 'pk'))
    running, rows, tot_d, tot_c = opening, [], ZERO, ZERO
    # الصفحة بتعرض أول ٢٠٠٠ سطر؛ التصدير (Excel/PDF) بياخد الفترة كلها
    limit = None if request.GET.get('export') else 2000
    for e in (lines if limit is None else lines[:limit]):
        running += signed(e.debit, e.credit)
        tot_d += e.debit
        tot_c += e.credit
        url, label = _source_link(e)
        rows.append({'e': e, 'balance': running, 'url': url, 'label': label})
    truncated = lines.count() > len(rows)

    _exp = export_report(
        request, f"ledger_{account.code}", f"دفتر الأستاذ — {account.code} {account.name}",
        f"{d_from} → {d_to}",
        [{
            "columns": ["التاريخ", "القيد", "البيان", "مدين", "دائن", "الرصيد"],
            "rows": [["", "", "رصيد أول المدة", "", "", opening]]
                    + [[r['e'].entry_date.date().isoformat(), r['e'].reference, r['e'].description,
                        r['e'].debit, r['e'].credit, r['balance']] for r in rows],
            "total": ["", "", "الإجمالي", tot_d, tot_c, running],
        }])
    if _exp:
        return _exp

    return render(request, 'inventory/account_ledger.html', {
        'account': account, 'type_label': dict(ChartOfAccount.ACCOUNT_TYPES).get(account.account_type, ''),
        'd_from': d_from, 'd_to': d_to, 'opening': opening, 'closing': running,
        'rows': rows, 'total_debit': tot_d, 'total_credit': tot_c, 'truncated': truncated,
        'scope': branch.name if branch is not None else '',
    })
