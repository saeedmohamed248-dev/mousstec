"""📈 القوائم المالية من الدفتر: الأرباح والخسائر، ميزان المراجعة، المركز المالي.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.shortcuts import render
from inventory.models import FinancialTransaction, SaleInvoice
from inventory.views import _get_branch_for_user, tenant_required
from inventory.views.utils import module_required, role_required
from inventory.report_export import export_report
from inventory.services.accounting_service import (
    ADVANCE_TAG as _ADVANCE_TAG,
    TRANSFER_TAG as _TRANSFER_TAG,
)
from ._common import _report_branch, _report_period


# =====================================================================
# 📈 تقرير الأرباح والخسائر (P&L)
# =====================================================================
def _advance_recoveries(start, end, branch=None):
    """مجموع أقساط السلف (اللي اتصرفت من النظام) المخصومة من مرتبات اتصرفت في الفترة."""
    try:
        from hr.models import AdvanceInstallment
    except Exception:  # noqa: BLE001
        return Decimal('0')
    qs = AdvanceInstallment.objects.filter(
        status='deducted', advance__disbursed_at__isnull=False,
        deducted_in_payroll__payroll_run__status='paid')
    if start is not None:
        qs = qs.filter(deducted_in_payroll__payroll_run__paid_at__gte=start)
    if end is not None:
        qs = qs.filter(deducted_in_payroll__payroll_run__paid_at__lt=end)
    if branch is not None:
        qs = qs.filter(advance__employee__user__employee_profile__branch=branch)
    return qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def pnl_report(request):
    """قائمة الدخل: المبيعات − تكلفة البضاعة = مجمّل الربح، ناقص المصروفات = صافي الربح."""
    from inventory.models import PurchaseInvoiceExtraCost
    # نفس فلتر الفترة الموحّد بتاع باقي التقارير (فيه الشهر اللي فات ومن/إلى)
    start, end, label, period = _report_period(request)

    branch, branch_options, can_pick_branch = _report_branch(request)

    inv = SaleInvoice.objects.exclude(status='quotation')
    exp = (FinancialTransaction.objects
           .filter(transaction_type='out', sale_invoice__isnull=True,
                   purchase_invoice__isnull=True, vendor__isnull=True, customer__isnull=True,
                   equity_kind='')   # 🐛 مسحوبات المالك كانت بتتخصم من الربح كمصروف
           .exclude(description__startswith=_TRANSFER_TAG)
           # سلفة الموظف وقت صرفها أصل (فلوس لينا عنده) — بتبقى مصروف لما
           # قسطها يتخصم من المرتب (تحت).
           .exclude(employee__isnull=False, description__startswith=_ADVANCE_TAG))
    if branch is not None:
        inv = inv.filter(branch=branch)
        exp = exp.filter(treasury__branch=branch)
    # 🐛 [FIX]: مصاريف الفترة على فواتير الشراء/الشحنات (سفر/إعاشة/أخرى «مصروف»)
    #    ماكانتش بتظهر خالص: المدفوعة من خزنة متربطة بفاتورة الشراء (فمتشالة
    #    بالفلتر فوق)، والآجلة مالهاش حركة خزنة أصلاً — فالربح كان طالع أكبر.
    #    بنحسبها بتاريخ فاتورة الشراء (استحقاق) زي ما الدفتر بيقيّدها.
    ship_exp = PurchaseInvoiceExtraCost.objects.filter(
        behavior='expense', invoice__status='posted', invoice__is_applied=True)
    if branch is not None:
        inv = inv.filter(branch=branch)
        exp = exp.filter(treasury__branch=branch)
        ship_exp = ship_exp.filter(invoice__branch=branch)
    if start is not None:
        inv = inv.filter(date_created__gte=start)
        exp = exp.filter(date__gte=start)
        ship_exp = ship_exp.filter(invoice__date_created__gte=start)
    if end is not None:
        inv = inv.filter(date_created__lt=end)
        exp = exp.filter(date__lt=end)
        ship_exp = ship_exp.filter(invoice__date_created__lt=end)

    agg = inv.aggregate(sales_r=Sum('total_amount', filter=Q(is_return=True)))
    # 🐛 [FIX]: صافي المبيعات كان شامل ض.ق.م — فالضريبة كلها بتطلع «ربح».
    from inventory.services.reporting_service import ReportingService
    net_sales, cogs = ReportingService.net_sales_ex_vat(inv)
    gross = net_sales - cogs

    exp_rows = list(exp.values('category__name').annotate(t=Sum('amount')).order_by('-t'))
    kind_label = dict(PurchaseInvoiceExtraCost.KIND_CHOICES)
    for r in ship_exp.values('kind').annotate(t=Sum('amount')).order_by('-t'):
        exp_rows.append({'category__name': f"🚢 {kind_label.get(r['kind'], r['kind'])} (مشتريات/شحنات)",
                         't': r['t']})
    # 💵 أقساط السلف اللي اتخصمت من المرتبات في الفترة (جزء من المرتب اتدفع
    #    مقدّماً وقت صرف السلفة) — نفس قيد PayrollService._post_advance_recovery.
    recovered = _advance_recoveries(start, end, branch)
    if recovered:
        exp_rows.append({'category__name': 'رواتب وأجور (أقساط سلف مخصومة)', 't': recovered})
    exp_rows.sort(key=lambda r: -(r['t'] or 0))
    total_exp = sum((r['t'] or Decimal('0') for r in exp_rows), Decimal('0'))
    net_profit = gross - total_exp
    margin = (net_profit / net_sales * Decimal('100')) if net_sales else Decimal('0')

    _exp = export_report(
        request, f"pnl_{period}", "قائمة الأرباح والخسائر",
        f"{label} · {branch.name if branch else 'كل الفروع'}",
        [{
            "columns": ["البند", "المبلغ"],
            "rows": [["صافي المبيعات", net_sales],
                     ["تكلفة البضاعة المباعة", -cogs],
                     ["مجمّل الربح", gross]]
                    + [[(e['category__name'] or 'بدون بند'), -(e['t'] or Decimal('0'))] for e in exp_rows]
                    + [["إجمالي المصروفات", -total_exp]],
            "total": ["صافي الربح", net_profit],
        }])
    if _exp:
        return _exp

    return render(request, 'inventory/pnl_report.html', {
        'branch': branch, 'period': period, 'label': label,
        'branch_options': branch_options, 'can_pick_branch': can_pick_branch,
        'net_sales': net_sales, 'cogs': cogs, 'gross': gross,
        'exp_rows': exp_rows, 'total_exp': total_exp,
        'net_profit': net_profit, 'margin': margin,
        'invoices_count': inv.filter(is_return=False).count(),
        'returns_amount': (agg['sales_r'] or Decimal('0')),
    })


# =====================================================================
# ⚖️ ميزان المراجعة (Trial Balance) من دفتر الأستاذ
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def trial_balance(request):
    """ميزان المراجعة: مجاميع المدين/الدائن لكل حساب من القيود، وإجمالي متوازن."""
    from inventory.models import AccountingEntry, ChartOfAccount
    from django.utils import timezone as _tz
    now = _tz.localtime()   # بداية اليوم/الشهر بتوقيت المحل مش UTC
    period = request.GET.get('period', 'all')
    if period == 'month':
        start, label = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), "هذا الشهر"
    elif period == 'year':
        start, label = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0), "هذه السنة"
    else:
        period, start, label = 'all', None, "كل الفترات (تراكمي)"

    # 🏢 فلتر الفرع: الفرع النشط من المبدّل، أو None = كل الفروع (موحّد).
    branch = _get_branch_for_user(request.user)
    scope = branch.name if branch is not None else "كل الفروع"

    qs = AccountingEntry.objects.all()
    if start is not None:
        qs = qs.filter(entry_date__gte=start)
    if branch is not None:
        qs = qs.filter(journal_entry__branch=branch)

    agg = (qs.values('account_id', 'account__code', 'account__name', 'account__account_type')
           .annotate(d=Sum('debit'), c=Sum('credit'))
           .order_by('account__code'))

    type_label = dict(ChartOfAccount.ACCOUNT_TYPES)
    rows, tot_d, tot_c = [], Decimal('0'), Decimal('0')
    for r in agg:
        d = r['d'] or Decimal('0')
        c = r['c'] or Decimal('0')
        if d == 0 and c == 0:
            continue
        atype = r['account__account_type']
        net = (d - c) if atype in ('asset', 'expense') else (c - d)
        rows.append({
            'code': r['account__code'], 'name': r['account__name'],
            'type': atype, 'type_label': type_label.get(atype, atype),
            'debit': d, 'credit': c, 'net': net,
            'net_side': 'debit' if atype in ('asset', 'expense') else 'credit',
        })
        tot_d += d
        tot_c += c

    _exp = export_report(
        request, f"trial_balance_{period}", "ميزان المراجعة", label,
        [{
            "columns": ["الكود", "الحساب", "مدين", "دائن"],
            "rows": [[r['code'], r['name'], r['debit'], r['credit']] for r in rows],
            "total": ["", "الإجمالي", tot_d, tot_c],
        }])
    if _exp:
        return _exp

    return render(request, 'inventory/trial_balance.html', {
        'rows': rows, 'total_debit': tot_d, 'total_credit': tot_c,
        'balanced': (tot_d == tot_c), 'diff': (tot_d - tot_c),
        'period': period, 'label': label, 'scope': scope,
        # 📒 اسم الحساب بيفتح دفتر الأستاذ بتاعه لنفس الفترة
        'ledger_from': (start.date().isoformat() if start is not None else '2000-01-01'),
        'ledger_to': now.date().isoformat(),
    })


# =====================================================================
# 🏦 قائمة المركز المالي (Balance Sheet)
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def balance_sheet(request):
    """المركز المالي: الأصول = الخصوم + حقوق الملكية + صافي الربح (من دفتر الأستاذ)."""
    from inventory.models import AccountingEntry
    from django.utils import timezone as _tz
    now = _tz.localtime()   # بداية اليوم/الشهر بتوقيت المحل مش UTC
    period = request.GET.get('period', 'all')
    if period == 'month':
        start, label = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), "هذا الشهر"
    elif period == 'year':
        start, label = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0), "هذه السنة"
    else:
        period, start, label = 'all', None, "حتى تاريخه (تراكمي)"

    qs = AccountingEntry.objects.all()
    if start is not None:
        qs = qs.filter(entry_date__gte=start)
    agg = (qs.values('account__code', 'account__name', 'account__account_type')
           .annotate(d=Sum('debit'), c=Sum('credit')).order_by('account__code'))

    buckets = {'asset': [], 'liability': [], 'equity': [], 'revenue': [], 'expense': []}
    tot = {'asset': Decimal('0'), 'liability': Decimal('0'), 'equity': Decimal('0'),
           'revenue': Decimal('0'), 'expense': Decimal('0')}
    for r in agg:
        d = r['d'] or Decimal('0')
        c = r['c'] or Decimal('0')
        if d == 0 and c == 0:
            continue
        atype = r['account__account_type']
        if atype not in buckets:
            continue
        net = (d - c) if atype in ('asset', 'expense') else (c - d)
        buckets[atype].append({'code': r['account__code'], 'name': r['account__name'], 'net': net})
        tot[atype] += net

    net_income = tot['revenue'] - tot['expense']
    total_assets = tot['asset']
    total_liab_equity = tot['liability'] + tot['equity'] + net_income
    diff = total_assets - total_liab_equity

    _exp = export_report(
        request, f"balance_sheet_{period}", "قائمة المركز المالي", label,
        [
            {"name": "الأصول",
             "columns": ["الحساب", "المبلغ"],
             "rows": [[a['name'], a['net']] for a in buckets['asset']],
             "total": ["إجمالي الأصول", total_assets]},
            {"name": "الخصوم وحقوق الملكية",
             "columns": ["الحساب", "المبلغ"],
             "rows": [[l['name'], l['net']] for l in buckets['liability']]
                     + [[e['name'], e['net']] for e in buckets['equity']]
                     + [["صافي ربح الفترة", net_income]],
             "total": ["إجمالي الخصوم وحقوق الملكية", total_liab_equity]},
        ])
    if _exp:
        return _exp

    return render(request, 'inventory/balance_sheet.html', {
        'assets': buckets['asset'], 'liabilities': buckets['liability'],
        'equity': buckets['equity'],
        'total_assets': total_assets, 'total_liabilities': tot['liability'],
        'total_equity': tot['equity'], 'net_income': net_income,
        'total_liab_equity': total_liab_equity,
        'balanced': (abs(diff) < Decimal('0.01')), 'diff': diff,
        'period': period, 'label': label,
        # الميزانية دايماً على مستوى الشركة (حقوق الملكية مركزية) — نوضّح ده.
        'scope': "الشركة (كل الفروع)",
    })
