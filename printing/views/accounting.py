"""
📒 Printing accounting pages — views over the general ledger.

/printing/accounting/                       ميزان المراجعة + الميزانية + اليومية + أعمار الديون
                                            + ضريبة القيمة المضافة + الفترات المحاسبية
/printing/accounting/vat/file/              (POST) تقديم إقرار ضريبة لفترة
/printing/accounting/periods/action/        (POST) سنة مالية جديدة / إقفال فترة / إعادة فتحها
/printing/accounting/reconcile/<id>/        مطابقة بنك/محفظة مع كشف الحساب
/printing/supplier/<id>/statement/          كشف حساب المورد
/printing/material/<id>/card/               كارت الصنف (وارد/منصرف برصيد جاري)

أي صفحة فيها جدول بتطلع Excel بـ ``?export=xlsx``.
"""
import calendar
from datetime import date as _date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

ZERO = Decimal('0')
AGING_BUCKETS = ('current', 'd31_60', 'd61_90', 'over_90')
MONTHS_AR = ('يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو', 'يوليو', 'أغسطس',
             'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر')


def _can_view_books(user):
    if user.is_staff or user.is_superuser:
        return True
    perms = getattr(user, 'print_permissions', None)
    try:
        return bool(perms and (perms.can_view_reports or perms.can_view_profits))
    except Exception:
        return False


def _can_manage_books(user):
    """إقفال الفترات والإقرارات والمطابقة: المدير أو اللي ليه صلاحية الخزنة."""
    if user.is_superuser:
        return True
    perms = getattr(user, 'print_permissions', None)
    try:
        return bool(user.is_staff and perms and perms.can_manage_treasury)
    except Exception:
        return False


def _bucket(days):
    if days <= 30:
        return 'current'
    if days <= 60:
        return 'd31_60'
    if days <= 90:
        return 'd61_90'
    return 'over_90'


def _empty_buckets():
    return {b: ZERO for b in AGING_BUCKETS}


def receivables_aging(as_of):
    """
    أعمار ديون العملاء: الباقي على كل طلب اتسلّم (بعد المرتجعات والمدفوع) حسب
    عمره من يوم التسليم — والرصيد المقدّم بيتخصم من أقدم دين.
    """
    from printing.models import PrintCustomer
    rows, totals = [], _empty_buckets()
    for c in PrintCustomer.objects.all().order_by('name'):
        docs = []
        for o in c.printorder_set.filter(status='delivered').only(
                'order_number', 'total_amount', 'discount', 'tax_amount', 'paid_amount', 'credited_amount',
                'date_delivered', 'date_created'):
            left = o.due_amount - o.paid_amount
            if left > 0:
                docs.append((timezone.localtime(o.date_delivered or o.date_created).date(), left))
        summary = c.account_summary()
        if summary['opening_outstanding'] > 0:          # الرصيد الافتتاحي اللي لسه ماتسدّدش
            docs.append((c.opening_date, summary['opening_outstanding']))
        if not docs:
            continue
        credit = summary['credit']
        buckets = _empty_buckets()
        for d, left in sorted(docs):
            used = min(credit, left)
            credit -= used
            if left - used > 0:
                buckets[_bucket((as_of - d).days)] += left - used
        total = sum(buckets.values(), ZERO)
        if total <= 0:
            continue
        for b in AGING_BUCKETS:
            totals[b] += buckets[b]
        rows.append({'party': c, 'buckets': buckets, 'total': total,
                     'url': f'/printing/customer/{c.pk}/statement/'})
    rows.sort(key=lambda r: -r['total'])
    return {'rows': rows, 'totals': totals, 'total': sum(totals.values(), ZERO)}


def payables_aging(as_of):
    """
    أعمار المستحق للموردين: فواتير الخامات (بعد المرتجعات) + فواتير المصروفات +
    الأصول الآجلة، كل واحدة بالباقي عليها وعمرها من ميعاد السداد (أو تاريخها)؛
    الدفعات على الحساب والزيادة على فاتورة بتتخصم من الأقدم.
    """
    from printing.models import PrintSupplier
    rows, totals = [], _empty_buckets()
    for s in PrintSupplier.objects.all().order_by('name'):
        docs = []
        for p in s.purchases.filter(status='received'):
            docs.append((p.date, p.remaining))
        for b in s.bills.filter(is_cancelled=False):
            docs.append((b.due_date or b.date, b.remaining))
        # الأصول الآجلة والرصيد الافتتاحي مالهمش سداد مربوط — بيتسدّدوا من الدفعات على الحساب.
        for a in s.fixed_assets.filter(funding='credit'):
            docs.append((a.purchase_date, a.cost))
        if s.opening_balance > 0:
            docs.append((s.opening_date, s.opening_balance))
        # الدفعات اللي مش متسدّد بيها فاتورة (شامل الزيادة على فاتورة) — بتقفل الأقدم الأول
        pool = s.unapplied_payments()
        open_docs = [(d, left) for d, left in sorted(docs) if left > 0]
        buckets = _empty_buckets()
        for d, left in open_docs:
            used = min(max(pool, ZERO), left)
            pool -= used
            if left - used > 0:
                buckets[_bucket((as_of - d).days)] += left - used
        total = sum(buckets.values(), ZERO)
        if total <= 0:
            continue
        for b in AGING_BUCKETS:
            totals[b] += buckets[b]
        rows.append({'party': s, 'buckets': buckets, 'total': total,
                     'url': f'/printing/supplier/{s.pk}/statement/'})
    rows.sort(key=lambda r: -r['total'])
    return {'rows': rows, 'totals': totals, 'total': sum(totals.values(), ZERO)}


def xlsx_response(filename, title, headers, rows, totals=None):
    """جدول ← ملف Excel (الشيت من اليمين للشمال، والأرقام أرقام مش نص)."""
    from io import BytesIO
    from django.http import HttpResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = str(title)[:31]
    ws.sheet_view.rightToLeft = True
    ws.append([str(title)])
    ws['A1'].font = Font(bold=True, size=13)
    ws.append([str(h) for h in headers])
    for cell in ws[2]:
        cell.font = Font(bold=True)

    def cell_value(v):
        if isinstance(v, Decimal):
            return float(v)
        if hasattr(v, 'tzinfo') and getattr(v, 'tzinfo', None) is not None:
            return timezone.localtime(v).replace(tzinfo=None)
        return v if isinstance(v, (int, float, _date)) or v is None else str(v)

    for row in rows:
        ws.append([cell_value(v) for v in row])
    if totals:
        ws.append([cell_value(v) for v in totals])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col[1:] if c.value is not None), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)
    buf = BytesIO()
    wb.save(buf)
    response = HttpResponse(buf.getvalue(),
                            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}.xlsx"'
    return response


def _wants_xlsx(request):
    return request.GET.get('export') == 'xlsx'


def _date_param(value):
    from datetime import date as _d
    try:
        return _d.fromisoformat((value or '').strip())
    except ValueError:
        return None


@login_required
def accounting_overview(request):
    """ميزان المراجعة والميزانية ودفتر اليومية — كله من الدفتر العام (استحقاق)."""
    from inventory.models import JournalEntry
    from inventory.services.accounting_reports import AccountingReportService
    from printing import ledger

    if not _can_view_books(request.user):
        return HttpResponseForbidden(_("لا تملك صلاحية مشاهدة الدفاتر المحاسبية."))
    ledger.ensure_bootstrapped()

    today = timezone.localdate()
    as_of = _date_param(request.GET.get('as_of')) or today
    date_from = _date_param(request.GET.get('from')) or today.replace(day=1)
    date_to = _date_param(request.GET.get('to')) or today

    tab = request.GET.get('tab') or 'trial'
    ctx = {'tab': tab, 'as_of': as_of, 'date_from': date_from, 'date_to': date_to,
           'can_manage': _can_manage_books(request.user)}
    if tab == 'journal':
        ctx['entries'] = (JournalEntry.objects.filter(date__gte=date_from, date__lte=date_to)
                          .prefetch_related('lines__account').order_by('-date', '-id')[:200])
    elif tab == 'sheet':
        ctx['sheet'] = AccountingReportService.balance_sheet(as_of=as_of)
    elif tab == 'aging':
        ctx['ar'] = receivables_aging(as_of)
        ctx['ap'] = payables_aging(as_of)
        if _wants_xlsx(request):
            heads = [_('الطرف'), _('النوع'), _('لحد 30 يوم'), _('31–60 يوم'), _('61–90 يوم'),
                     _('أكتر من 90 يوم'), _('الإجمالي')]
            rows = [(r['party'].name, label) + tuple(r['buckets'][b] for b in AGING_BUCKETS) + (r['total'],)
                    for label, data in ((_('عميل'), ctx['ar']), (_('مورد'), ctx['ap'])) for r in data['rows']]
            return xlsx_response(f'aging-{as_of}', _('أعمار الديون'), heads, rows)
    elif tab == 'vat':
        from printing.models import PrintVatReturn
        output, inputs = ledger.vat_activity(date_from, date_to)
        out_open, in_open = ledger.vat_balances(date_to)
        settlement = ledger.account('vat_settlement')
        ctx['vat'] = {
            'output': output, 'input': inputs, 'net': output - inputs,
            'open_output': out_open, 'open_input': in_open, 'open_net': out_open - in_open,
            'settlement_due': -_signed_balance(settlement, date_to),
            'returns': PrintVatReturn.objects.all()[:24],
        }
        # 🧾 الخصم من المنبع: شهادات العملاء (لينا) + اللي خصمناه من الموردين (لازم يتورّد)
        from printing.models import PrintTreasury as _T
        wallets = {w.kind: w for w in _T.objects.filter(kind__in=('wht_in', 'wht_out'))}
        ctx['wht'] = {
            'receivable': wallets['wht_in'].balance if 'wht_in' in wallets else ZERO,
            'payable': -wallets['wht_out'].balance if 'wht_out' in wallets else ZERO,
            'payable_wallet': wallets.get('wht_out'),
        }
    elif tab == 'branches':
        ctx['branch_rows'] = ledger.income_by_branch(date_from, date_to)
        if _wants_xlsx(request):
            rows = [((r['branch'].name if r['branch'] else _('عام (من غير فرع)')), r['revenue'], r['cogs'],
                     r['gross'], r['expenses'], r['net']) for r in ctx['branch_rows']]
            return xlsx_response(f'branches-{date_from}-{date_to}', _('أرباح الفروع'),
                                 [_('الفرع'), _('الإيرادات'), _('تكلفة الخامات'), _('مجمل الربح'),
                                  _('المصروفات'), _('صافي الربح')], rows)
    elif tab == 'periods':
        from inventory.models import FiscalYear
        ctx['years'] = FiscalYear.objects.prefetch_related('periods').order_by('-start_date')
        ctx['today'] = today
        ctx['next_year'] = (FiscalYear.objects.order_by('-end_date').values_list('end_date', flat=True).first()
                            or _date(today.year - 1, 12, 31)).year + 1
    else:
        ctx['trial'] = AccountingReportService.trial_balance(as_of=as_of)
        if _wants_xlsx(request):
            trial = ctx['trial']
            rows = [(r['code'], r['name'], r['debit'] or None, r['credit'] or None) for r in trial['rows']]
            return xlsx_response(f'trial-balance-{as_of}', _('ميزان المراجعة'),
                                 [_('الكود'), _('الحساب'), _('مدين'), _('دائن')], rows,
                                 totals=('', _('الإجمالي'), trial['total_debit'], trial['total_credit']))
    from printing.models import PrintTreasury
    ctx['banks'] = PrintTreasury.objects.filter(kind__in=('bank', 'wallet'), is_active=True)
    return render(request, 'printing/accounting.html', ctx)


def _signed_balance(acc, as_of):
    """مدين − دائن للحساب لحد تاريخ."""
    from inventory.models import AccountingEntry
    agg = AccountingEntry.objects.filter(account=acc, journal_entry__date__lte=as_of).aggregate(
        d=Sum('debit'), c=Sum('credit'))
    return (agg['d'] or ZERO) - (agg['c'] or ZERO)


@login_required
@require_POST
def vat_return_file(request):
    """
    تقديم إقرار ضريبة لفترة: بيقفل ضريبة المبيعات والمشتريات لحد آخر الفترة في
    «صافي مستحق السداد» — وسداده بعدين حركة خزنة ببند «سداد ضريبة القيمة المضافة».
    """
    from printing import ledger
    from printing.models import PrintVatReturn
    if not _can_manage_books(request.user):
        return HttpResponseForbidden(_("لا تملك صلاحية تقديم الإقرارات."))
    ledger.ensure_bootstrapped()
    d_from, d_to = _date_param(request.POST.get('from')), _date_param(request.POST.get('to'))
    back = redirect(f'/printing/accounting/?tab=vat&from={request.POST.get("from", "")}&to={request.POST.get("to", "")}')
    if not d_from or not d_to or d_from > d_to:
        messages.error(request, _("اختار فترة صحيحة للإقرار."))
        return back
    if d_to > timezone.localdate():
        messages.error(request, _("الإقرار بيتقدّم عن فترة خلصت — آخر الفترة بعد النهارده."))
        return back
    if PrintVatReturn.objects.filter(date_from__lte=d_to, date_to__gte=d_from).exists():
        messages.error(request, _("فيه إقرار متقدّم بالفعل عن جزء من الفترة دي."))
        return back
    output, inputs = ledger.vat_balances(d_to)
    with transaction.atomic():
        vat = PrintVatReturn.objects.create(date_from=d_from, date_to=d_to, output_vat=output, input_vat=inputs,
                                            notes=(request.POST.get('notes') or '')[:300], created_by=request.user)
    messages.success(request, _("اتقدّم الإقرار: صافي %(n)s %(w)s.") % {
        'n': f'{abs(vat.net_payable):,.2f}',
        'w': _('مستحق للضرايب') if vat.net_payable >= 0 else _('رصيد لينا')})
    return back


@login_required
@require_POST
def periods_action(request):
    """سنة مالية جديدة (12 شهر) / إقفال فترة / إعادة فتح فترة (للمدير بس)."""
    from inventory.models import AccountingPeriod, FiscalYear, JournalEntry
    from inventory.services.accounting_service import AccountingService
    from printing import ledger
    if not _can_manage_books(request.user):
        return HttpResponseForbidden(_("لا تملك صلاحية إقفال الفترات."))
    back = redirect('/printing/accounting/?tab=periods')
    action = request.POST.get('action')
    today = timezone.localdate()

    if action == 'create_year':
        try:
            year = int(request.POST.get('year') or 0)
        except ValueError:
            year = 0
        if not 2000 <= year <= today.year + 1:
            messages.error(request, _("سنة غير صالحة."))
            return back
        start, end = _date(year, 1, 1), _date(year, 12, 31)
        if FiscalYear.objects.filter(start_date__lte=end, end_date__gte=start).exists():
            messages.error(request, _("السنة دي (أو جزء منها) متسجّلة قبل كده."))
            return back
        with transaction.atomic():
            fy = FiscalYear.objects.create(code=str(year), name=_('السنة المالية %(y)s') % {'y': year},
                                           start_date=start, end_date=end)
            for m in range(1, 13):
                AccountingPeriod.objects.create(
                    fiscal_year=fy, name=f'{MONTHS_AR[m - 1]} {year}',
                    start_date=_date(year, m, 1), end_date=_date(year, m, calendar.monthrange(year, m)[1]))
        messages.success(request, _("اتعملت السنة المالية %(y)s بـ 12 فترة شهرية.") % {'y': year})
        return back

    period = get_object_or_404(AccountingPeriod, pk=request.POST.get('period'))
    if action == 'close':
        if period.is_closed:
            messages.info(request, _("الفترة مقفولة بالفعل."))
            return back
        if period.end_date >= today:
            messages.error(request, _("مينفعش تقفل فترة لسه ماخلصتش."))
            return back
        earlier_open = AccountingPeriod.objects.filter(end_date__lt=period.start_date, is_closed=False).exists()
        if earlier_open:
            messages.error(request, _("اقفل الفترات اللي قبلها الأول بالترتيب."))
            return back
        ledger.ensure_bootstrapped()      # إهلاك الشهر وكل القيود متقيّدة قبل الإقفال
        try:
            AccountingService.close_period(period, created_by=request.user)
        except ValidationError as exc:
            messages.error(request, '؛ '.join(exc.messages))
            return back
        messages.success(request, _("اتقفلت فترة %(p)s — أرباحها اترحّلت للأرباح المحتجزة.") % {'p': period.name})
        return back

    if action == 'reopen':
        if not request.user.is_superuser:
            return HttpResponseForbidden(_("إعادة فتح فترة مقفولة للمدير بس."))
        if not period.is_closed:
            return back
        later_closed = AccountingPeriod.objects.filter(start_date__gt=period.end_date, is_closed=True).exists()
        if later_closed:
            messages.error(request, _("افتح الفترات اللي بعدها الأول (من الأحدث للأقدم)."))
            return back
        with transaction.atomic():
            JournalEntry.objects.filter(reference__in=[f'CLOSE-{period.pk}', f'CLOSE-RE-{period.pk}']).delete()
            AccountingPeriod.objects.filter(pk=period.pk).update(is_closed=False, closed_at=None, closed_by=None)
        messages.success(request, _("اتفتحت فترة %(p)s تاني — قيود الإقفال بتاعتها اتشالت.") % {'p': period.name})
        return back

    messages.error(request, _("إجراء غير معروف."))
    return back


@login_required
def bank_reconciliation(request, treasury_id):
    """
    مطابقة البنك/المحفظة: علّم الحركات اللي ظهرت في كشف الحساب لحد تاريخه —
    الرصيد الافتتاحي + المطابق لازم يساوي رصيد الكشف، والباقي (معلّق) يتشرح.
    """
    from printing import ledger
    from printing.models import PrintBankReconciliation, PrintTransaction, PrintTreasury
    if not _can_manage_books(request.user):
        return HttpResponseForbidden(_("لا تملك صلاحية مطابقة البنوك."))
    treasury = get_object_or_404(PrintTreasury, pk=treasury_id)
    statement_date = _date_param(request.POST.get('statement_date') or request.GET.get('date')) \
        or timezone.localdate()
    opening, _opening_date = ledger.treasury_opening(treasury)
    upto = PrintTransaction.objects.filter(treasury=treasury, date__date__lte=statement_date)

    def signed_sum(qs):
        agg = qs.aggregate(ins=Sum('amount', filter=Q(transaction_type='in')),
                           outs=Sum('amount', filter=Q(transaction_type='out')))
        return (agg['ins'] or ZERO) - (agg['outs'] or ZERO)

    if request.method == 'POST':
        try:
            statement_balance = Decimal((request.POST.get('statement_balance') or '').replace(',', ''))
        except ArithmeticError:
            messages.error(request, _("اكتب رصيد كشف البنك."))
            return redirect(request.path)
        ids = [int(i) for i in request.POST.getlist('txn') if i.isdigit()]
        with transaction.atomic():
            upto.filter(pk__in=ids, cleared=False).update(cleared=True, cleared_on=statement_date)
            cleared_balance = opening + signed_sum(upto.filter(cleared=True))
            rec = PrintBankReconciliation.objects.create(
                treasury=treasury, statement_date=statement_date, statement_balance=statement_balance,
                cleared_balance=cleared_balance, book_balance=opening + signed_sum(upto),
                notes=(request.POST.get('notes') or '')[:300], created_by=request.user)
        if rec.difference:
            messages.warning(request, _("اتحفظت المطابقة بفرق %(d)s — راجع الحركات اللي مش في السيستم "
                                        "(مصاريف بنكية/فوايد) وسجّلها.") % {'d': f'{rec.difference:,.2f}'})
        else:
            messages.success(request, _("المطابقة مظبوطة — رصيد السيستم = رصيد الكشف."))
        return redirect(f'{request.path}?date={statement_date.isoformat()}')

    uncleared = upto.filter(cleared=False).order_by('date', 'pk')
    return render(request, 'printing/bank_reconciliation.html', {
        'treasury': treasury,
        'statement_date': statement_date,
        'opening': opening,
        'cleared_total': opening + signed_sum(upto.filter(cleared=True)),
        'book_balance': opening + signed_sum(upto),
        'uncleared': uncleared,
        'history': treasury.reconciliations.all()[:12],
    })


@login_required
def supplier_statement(request, supplier_id):
    """كشف حساب المورد: فواتير الشراء المستلمة (له) والمدفوعات (عليه) برصيد جاري."""
    from printing.models import PrintSupplier
    from printing.views.finance import _can_see_money

    if not (_can_see_money(request.user, 'can_view_reports')
            or _can_see_money(request.user, 'can_manage_treasury')):
        return HttpResponseForbidden(_("لا تملك صلاحية مشاهدة كشوف الحساب."))
    supplier = get_object_or_404(PrintSupplier, pk=supplier_id)

    events = []
    for p in supplier.purchases.filter(status='received'):
        events.append({
            'date': p.received_at or timezone.make_aware(
                timezone.datetime.combine(p.date, timezone.datetime.min.time())),
            'kind': 'purchase', 'ref': p.number, 'obj_id': p.pk,
            'description': _('فاتورة شراء #%(no)s') % {'no': p.number} + (
                ' (' + _('فاتورة المورد %(no)s') % {'no': p.supplier_invoice_no} + ')' if p.supplier_invoice_no else ''),
            'credit': p.net_total, 'debit': Decimal('0'),
        })
    from printing.models import MaterialPurchaseReturn

    def _dt(d):
        return timezone.make_aware(timezone.datetime.combine(d, timezone.datetime.min.time()))

    if supplier.opening_balance:
        events.append({
            'date': _dt(supplier.opening_date), 'kind': 'opening', 'ref': '—', 'obj_id': supplier.pk,
            'description': _('رصيد افتتاحي (قبل السيستم)'),
            'credit': max(supplier.opening_balance, Decimal('0')),
            'debit': max(-supplier.opening_balance, Decimal('0')),
        })
    for b in supplier.bills.filter(is_cancelled=False):
        events.append({
            'date': _dt(b.date), 'kind': 'bill', 'ref': b.number, 'obj_id': b.pk,
            'description': _('فاتورة مصروف #%(no)s — %(d)s') % {'no': b.number, 'd': b.description},
            'credit': b.total, 'debit': Decimal('0'),
        })
    for r in MaterialPurchaseReturn.objects.filter(purchase__supplier=supplier, status='posted').select_related('purchase'):
        events.append({
            'date': _dt(r.date), 'kind': 'return', 'ref': r.number, 'obj_id': r.pk,
            'description': _('مرتجع مشتريات #%(no)s من فاتورة #%(p)s') % {'no': r.number, 'p': r.purchase.number},
            'credit': Decimal('0'), 'debit': r.total,
        })
    for a in supplier.fixed_assets.filter(funding='credit'):
        events.append({
            'date': _dt(a.purchase_date), 'kind': 'asset', 'ref': a.name, 'obj_id': a.pk,
            'description': _('شراء أصل ثابت آجل — %(n)s') % {'n': a.name},
            'credit': a.cost, 'debit': Decimal('0'),
        })
    for t in supplier.payments_qs().select_related('treasury', 'purchase'):
        paid = t.transaction_type == 'out'
        events.append({
            'date': t.date, 'kind': 'payment' if paid else 'refund', 'ref': f'#{t.pk}', 'obj_id': t.pk,
            'description': t.description or ((_('سداد للمورد — %(t)s') if paid
                                              else _('استرداد من المورد — %(t)s')) % {'t': t.treasury.name}),
            'debit': t.amount if paid else Decimal('0'),
            'credit': Decimal('0') if paid else t.amount,
        })
    events.sort(key=lambda e: e['date'])
    running = Decimal('0')
    for ev in events:
        running += ev['credit'] - ev['debit']
        ev['balance'] = running
    summary = supplier.account_summary()
    if _wants_xlsx(request):
        rows = [(ev['date'], ev['ref'], ev['description'], ev['credit'] or None, ev['debit'] or None, ev['balance'])
                for ev in events]
        return xlsx_response(f'supplier-{supplier.pk}-statement', _('كشف حساب المورد %(n)s') % {'n': supplier.name},
                             [_('التاريخ'), _('المرجع'), _('البيان'), _('له (علينا)'), _('عليه (دفعنا)'),
                              _('الرصيد')], rows)
    return render(request, 'printing/supplier_statement.html', {
        'supplier': supplier,
        'events': events,
        'summary': summary,
        'print_mode': request.GET.get('print') == '1',
    })


def material_movements(material):
    """
    كل حركات الخامة بالترتيب: شراء مستلم، إلغاء شراء، مرتجع للمورد، صرف على شغل،
    تسوية مخزن — والرصيد الافتتاحي = الكمية الحالية − صافي الحركات المسجّلة.
    """
    from printing.models import MaterialPurchase, MaterialPurchaseReturnLine
    moves = []
    for p in MaterialPurchase.objects.filter(lines__material=material, received_at__isnull=False).distinct():
        lines = list(p.lines.order_by('pk'))
        eff = p.effective_line_totals(lines)
        for line in lines:
            if line.material_id != material.pk:
                continue
            unit = (eff[line.pk] / line.quantity) if line.quantity else ZERO
            moves.append({'date': p.received_at, 'kind': _('شراء'), 'ref': p.number,
                          'url': f'/secure-portal/printing/materialpurchase/{p.pk}/change/',
                          'qty': line.quantity, 'unit_cost': unit, 'description': p.supplier.name})
            if p.status == 'cancelled' and p.cancelled_at:
                moves.append({'date': p.cancelled_at, 'kind': _('إلغاء شراء'), 'ref': p.number,
                              'url': f'/secure-portal/printing/materialpurchase/{p.pk}/change/',
                              'qty': -line.quantity, 'unit_cost': unit, 'description': p.supplier.name})
    for rl in (MaterialPurchaseReturnLine.objects.filter(purchase_line__material=material, ret__status='posted')
               .select_related('ret__purchase__supplier')):
        ret = rl.ret
        moves.append({'date': ret.posted_at or timezone.make_aware(
                          timezone.datetime.combine(ret.date, timezone.datetime.min.time())),
                      'kind': _('مرتجع للمورد'), 'ref': ret.number,
                      'url': f'/secure-portal/printing/materialpurchasereturn/{ret.pk}/change/',
                      'qty': -rl.quantity, 'unit_cost': (rl.value / rl.quantity) if rl.quantity else ZERO,
                      'description': ret.purchase.supplier.name})
    for use in material.usages.select_related('job__order'):
        moves.append({'date': use.created_at, 'kind': _('صرف على شغل'), 'ref': use.job.order.order_number,
                      'url': f'/secure-portal/printing/printorder/{use.job.order_id}/change/',
                      'qty': -use.quantity, 'unit_cost': use.unit_cost, 'description': use.job.description[:80]})
    for adj in material.adjustments.all():
        moves.append({'date': adj.created_at, 'kind': _('تسوية مخزن'), 'ref': adj.get_reason_display(),
                      'url': f'/secure-portal/printing/printstockadjustment/{adj.pk}/change/',
                      'qty': adj.quantity_change, 'unit_cost': adj.unit_cost, 'description': adj.notes})
    moves.sort(key=lambda m: m['date'])
    opening_qty = material.quantity - sum((m['qty'] for m in moves), ZERO)
    return opening_qty, moves


@login_required
def material_stock_card(request, material_id):
    """كارت الصنف: الرصيد الافتتاحي + كل وارد ومنصرف برصيد جاري (كمية وقيمة)."""
    from printing.models import PrintMaterial
    if not _can_view_books(request.user):
        return HttpResponseForbidden(_("لا تملك صلاحية مشاهدة الدفاتر المحاسبية."))
    material = get_object_or_404(PrintMaterial, pk=material_id)
    opening_qty, moves = material_movements(material)
    d_from, d_to = _date_param(request.GET.get('from')), _date_param(request.GET.get('to'))
    running = opening_qty
    rows = []
    for m in moves:
        running += m['qty']
        m['balance'] = running
        m['value'] = (m['qty'] * m['unit_cost']).quantize(Decimal('0.01'))
        m['in_qty'], m['out_qty'] = max(m['qty'], ZERO), max(-m['qty'], ZERO)
        day = timezone.localtime(m['date']).date()
        if d_from and day < d_from:
            opening_qty = running            # رصيد أول الفترة = بعد آخر حركة قبلها
            continue
        if d_to and day > d_to:
            continue
        rows.append(m)
    totals = {'in': sum((m['qty'] for m in rows if m['qty'] > 0), ZERO),
              'out': -sum((m['qty'] for m in rows if m['qty'] < 0), ZERO)}
    if _wants_xlsx(request):
        data = [(m['date'], m['kind'], m['ref'], m['description'], m['qty'] if m['qty'] > 0 else None,
                 -m['qty'] if m['qty'] < 0 else None, m['unit_cost'], m['balance']) for m in rows]
        return xlsx_response(f'stock-card-{material.pk}', _('كارت الصنف — %(m)s') % {'m': material.name},
                             [_('التاريخ'), _('الحركة'), _('المرجع'), _('البيان'), _('وارد'), _('منصرف'),
                              _('تكلفة الوحدة'), _('الرصيد')],
                             [(None, _('رصيد أول المدة'), '', '', None, None, None, opening_qty)] + data)
    return render(request, 'printing/stock_card.html', {
        'material': material, 'opening_qty': opening_qty, 'rows': rows, 'totals': totals,
        'date_from': d_from, 'date_to': d_to,
        'value': (material.quantity * material.cost_per_unit).quantize(Decimal('0.01')),
    })
