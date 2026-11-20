"""
📒 حسابات المنصة (/superadmin/finance/) — مصروفات وإيرادات Mouss Tec ككل + قايمة الدخل والمؤشرات.

المنطق كله في clients/services/platform_finance.py — هنا HTTP بس. صلاحية: لوحة المالك + ويدجت «الإيرادات».
"""
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from clients.models.platform_finance import (
    CURRENCY_CHOICES, EXPENSE_CATEGORIES, EXPENSE_GROUPS, INCOME_CATEGORIES, PAYMENT_METHODS,
    PlatformExpense, PlatformIncome,
)
from clients.permissions import widget_required
from clients.services import platform_finance as pf
from clients.views.saas_admin_views import saas_admin_required

_MAX_RECEIPT = 10 * 1024 * 1024
_RECEIPT_TYPES = ('image/', 'application/pdf')
PERIODS = (
    ('this_month', 'الشهر ده'), ('last_month', 'الشهر اللي فات'), ('quarter', 'آخر ٣ شهور'),
    ('year', 'السنة دي'), ('last_year', 'السنة اللي فاتت'), ('custom', 'فترة مخصّصة'),
)


def _period(request):
    today = timezone.localdate()
    key = request.GET.get('period') or 'this_month'
    if key == 'last_month':
        start, end = pf.month_bounds(pf.add_months(today.replace(day=1), -1))
    elif key == 'quarter':
        start, end = pf.add_months(today.replace(day=1), -2), today
    elif key == 'year':
        start, end = today.replace(month=1, day=1), today
    elif key == 'last_year':
        start, end = datetime.date(today.year - 1, 1, 1), datetime.date(today.year - 1, 12, 31)
    elif key == 'custom':
        try:
            start = datetime.date.fromisoformat(request.GET.get('from') or '')
            end = datetime.date.fromisoformat(request.GET.get('to') or '')
        except ValueError:
            key, (start, end) = 'this_month', pf.month_bounds(today)
        else:
            if start > end:
                start, end = end, start
    else:
        key, (start, end) = 'this_month', pf.month_bounds(today)
    return key, start, end


def _dec(value):
    try:
        d = Decimal(str(value or '').replace(',', '').strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


@saas_admin_required
@widget_required('revenue')
def finance_dashboard(request):
    pf.generate_recurring(timezone.localdate())
    key, start, end = _period(request)
    pnl = pf.profit_and_loss(start, end)
    p_start, p_end = pf.previous_period(start, end)
    prev = pf.profit_and_loss(p_start, p_end)
    cash = _dec(request.GET.get('cash'))
    series = pf.monthly_series(12, timezone.localdate())
    peak = max([max(m['revenue'], m['expenses']) for m in series] + [Decimal('1')])
    for m in series:
        m['rev_h'] = int(m['revenue'] * 100 / peak)
        m['exp_h'] = int(m['expenses'] * 100 / peak)

    exp_qs = PlatformExpense.objects.filter(date__gte=start, date__lte=end).select_related('created_by')
    cat = request.GET.get('cat') or ''
    if cat:
        exp_qs = exp_qs.filter(category=cat)
    inc_qs = PlatformIncome.objects.filter(date__gte=start, date__lte=end)

    from inventory.report_export import export_report
    resp = export_report(request, f'mousstec-pnl-{start}-{end}', 'حسابات Mouss Tec', f'{start} → {end}', [
        {'name': 'قايمة الدخل', 'columns': ['البند', 'المبلغ (ج.م)'], 'rows': [
            ['اشتراكات الشركات', pnl['revenue']['subscriptions']],
            ['عمولات سوق القطع', pnl['revenue']['commissions']],
            ['إيرادات أخرى', pnl['revenue']['manual']],
            ['إجمالي الإيراد', pnl['revenue']['total']],
            ['تكلفة تشغيل الخدمة', pnl['cogs']],
            ['مجمل الربح', pnl['gross_profit']],
            ['مصروفات تشغيلية', pnl['opex']],
            ['صافي الربح', pnl['net_profit']],
        ]},
        {'name': 'المصروفات', 'columns': ['التاريخ', 'البند', 'البيان', 'المورّد', 'المبلغ', 'العملة',
                                           'سعر الصرف', 'بالجنيه'],
         'rows': [[e.date, e.get_category_display(), e.description, e.vendor, e.amount, e.currency,
                   e.exchange_rate, e.amount_egp] for e in exp_qs]},
        {'name': 'إيرادات يدوية', 'columns': ['التاريخ', 'النوع', 'البيان', 'من', 'بالجنيه'],
         'rows': [[i.date, i.get_category_display(), i.description, i.source, i.amount_egp] for i in inc_qs]},
    ])
    if resp is not None:
        return resp

    groups = [{'key': g, 'label': lbl, 'total': pnl['expenses']['by_group'][g],
               'prev': prev['expenses']['by_group'][g],
               'items': [c for c in pnl['expenses']['by_category'] if c['group'] == g]}
              for g, lbl in EXPENSE_GROUPS]
    return render(request, 'clients/saas_admin/platform_finance.html', {
        'period': key, 'periods': PERIODS, 'start': start, 'end': end, 'pnl': pnl, 'prev': prev,
        'changes': {k: pf.change_pct(pnl[k] if k != 'revenue' else pnl['revenue']['total'],
                                     prev[k] if k != 'revenue' else prev['revenue']['total'])
                    for k in ('revenue', 'gross_profit', 'net_profit')},
        'expense_change': pf.change_pct(pnl['expenses']['total'], prev['expenses']['total']),
        'groups': groups, 'series': series, 'kpis': pf.kpis(timezone.localdate(), cash), 'cash': cash,
        'expenses': exp_qs[:300], 'incomes': inc_qs[:100], 'cat': cat,
        'categories': [(k, lbl) for k, lbl, _g in EXPENSE_CATEGORIES],
        'grouped_categories': [(lbl, [(k, cl) for k, cl, cg in EXPENSE_CATEGORIES if cg == g])
                               for g, lbl in EXPENSE_GROUPS],
        'currencies': CURRENCY_CHOICES, 'payment_methods': PAYMENT_METHODS, 'income_categories': INCOME_CATEGORIES,
        'today': timezone.localdate(), 'qs': request.GET.urlencode(),
        'qs_nocat': _qs_without(request, 'cat'),
        'last_rates': {e['currency']: e['exchange_rate'] for e in PlatformExpense.objects.exclude(currency='EGP')
                       .order_by('currency', '-date', '-id').distinct('currency').values('currency', 'exchange_rate')},
    })


def _qs_without(request, *keys):
    q = request.GET.copy()
    for k in keys:
        q.pop(k, None)
    return q.urlencode()


def _money_fields(request):
    """(البيانات المشتركة، رسالة_خطأ) — التاريخ والبيان والمبلغ والعملة وسعر الصرف."""
    try:
        date = datetime.date.fromisoformat(request.POST.get('date') or '')
    except ValueError:
        return None, 'التاريخ غلط.'
    description = (request.POST.get('description') or '').strip()[:200]
    amount = _dec(request.POST.get('amount'))
    currency = request.POST.get('currency') or 'EGP'
    rate = Decimal('1') if currency == 'EGP' else _dec(request.POST.get('exchange_rate'))
    if not description:
        return None, 'اكتب البيان.'
    if amount is None or amount <= 0:
        return None, 'المبلغ لازم يكون أكبر من صفر.'
    if currency not in dict(CURRENCY_CHOICES):
        return None, 'العملة غلط.'
    if rate is None or rate <= 0:
        return None, 'اكتب سعر الصرف بالجنيه وقت الدفع.'
    return {'date': date, 'description': description, 'amount': amount, 'currency': currency,
            'exchange_rate': rate, 'notes': (request.POST.get('notes') or '').strip()[:2000],
            'created_by': request.user}, None


def _back(request):
    nxt = request.POST.get('next') or ''
    return redirect(nxt if nxt.startswith('/superadmin/finance/') else 'saas_platform_finance')


@saas_admin_required
@widget_required('revenue')
@require_POST
def expense_add(request):
    data, err = _money_fields(request)
    category = request.POST.get('category') or ''
    if not err and category not in {k for k, _l, _g in EXPENSE_CATEGORIES}:
        err = 'اختار البند.'
    receipt = request.FILES.get('receipt')
    if not err and receipt is not None:
        ctype = (receipt.content_type or '').lower()
        if receipt.size > _MAX_RECEIPT or not ctype.startswith(_RECEIPT_TYPES):
            err = 'الإيصال لازم يكون صورة أو PDF أقل من 10 ميجا.'
    if err:
        messages.error(request, err)
        return _back(request)
    method = request.POST.get('payment_method') or 'card'
    PlatformExpense.objects.create(
        category=category, vendor=(request.POST.get('vendor') or '').strip()[:120],
        payment_method=method if method in dict(PAYMENT_METHODS) else 'other',
        receipt=receipt, is_recurring=request.POST.get('is_recurring') == '1', **data)
    messages.success(request, 'المصروف اتسجّل ✅')
    return _back(request)


@saas_admin_required
@widget_required('revenue')
@require_POST
def income_add(request):
    data, err = _money_fields(request)
    category = request.POST.get('category') or 'other'
    if not err and category not in dict(INCOME_CATEGORIES):
        err = 'اختار النوع.'
    if err:
        messages.error(request, err)
        return _back(request)
    PlatformIncome.objects.create(category=category, source=(request.POST.get('source') or '').strip()[:120], **data)
    messages.success(request, 'الإيراد اتسجّل ✅')
    return _back(request)


@saas_admin_required
@widget_required('revenue')
@require_POST
def entry_delete(request, kind, pk):
    model = {'expense': PlatformExpense, 'income': PlatformIncome}.get(kind)
    if model is None:
        raise Http404
    obj = get_object_or_404(model, pk=pk)
    if kind == 'expense' and request.POST.get('stop_recurring') == '1':
        # إيقاف التكرار من غير مسح اللي اتسجّل قبل كده
        src = obj.recurring_source or obj
        PlatformExpense.objects.filter(pk=src.pk).update(is_recurring=False)
        messages.success(request, 'التكرار الشهري وقف.')
        return _back(request)
    if kind == 'expense' and obj.receipt:
        obj.receipt.delete(save=False)
    obj.delete()
    messages.success(request, 'اتمسح.')
    return _back(request)


@saas_admin_required
@widget_required('revenue')
def expense_receipt(request, pk):
    """الإيصال من لوحة المالك بس — مش رابط ميديا عام."""
    obj = get_object_or_404(PlatformExpense, pk=pk)
    if not obj.receipt:
        raise Http404
    try:
        fh = obj.receipt.open('rb')
    except (FileNotFoundError, OSError):
        raise Http404
    resp = FileResponse(fh)
    resp['X-Content-Type-Options'] = 'nosniff'
    resp['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    resp['Cache-Control'] = 'private, no-store'
    return resp
