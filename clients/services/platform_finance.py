"""
📒 قايمة الدخل (P&L) ومؤشرات منصة Mouss Tec — بنفس معايير شركات SaaS العالمية.

أساسين للحساب (زي أي ERP عالمي):
  * الاستحقاق (accrual — الافتراضي، المعيار المحاسبي GAAP/IFRS):
      - الاشتراك بيتحسب إيراد على شهور الفترة اللي بيغطيها (IFRS 15 / ASC 606) — اشتراك سنوي
        مدفوع مقدّم مش «إيراد الشهر ده كله»؛ الجزء اللي لسه ماتقدّمش = إيراد مؤجّل (التزام).
      - المصروف اللي ليه فترة (من/لحد) بيتوزّع على أيامها بالتساوي — اللي اندفع مقدّم = مصروف
        مدفوع مقدّماً (أصل) لحد ما فترته تيجي. من غير فترة = في تاريخ الدفع.
  * النقدي (cash): كل حاجة في تاريخ الدفع — للتدفق النقدي ومعدل الحرق والـ runway.

الإيراد: اشتراكات (PlatformInvoice المدفوعة) + عمولات سوق القطع (PartOrder اتفرج عنها) + يدوي
(PlatformIncome) — التمويل/الاستثمار مش إيراد تشغيل (بيتعرض لوحده).
الترتيب: إيراد ← تكلفة تشغيل الخدمة (COGS) ← مجمل الربح ← مصروفات تشغيلية ← صافي الربح.
"""
import calendar
import datetime
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Min, Q, Sum

from clients.models.platform_finance import (
    CATEGORY_GROUP, EXPENSE_CATEGORIES, EXPENSE_GROUPS, PlatformExpense, PlatformIncome,
)

ZERO = Decimal('0')
CENT = Decimal('0.01')
NON_OPERATING_INCOME = ('investment',)
ACCRUAL, CASH = 'accrual', 'cash'


# ─────────────────────────────── أدوات تاريخ ───────────────────────────────
def month_bounds(d):
    return d.replace(day=1), d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def previous_period(start, end):
    days = (end - start).days + 1
    prev_end = start - datetime.timedelta(days=1)
    return prev_end - datetime.timedelta(days=days - 1), prev_end


def _share(amount, cov_start, cov_end, start, end):
    """نصيب [start, end] من مبلغ متوزّع بالتساوي على أيام [cov_start, cov_end]."""
    lo, hi = max(cov_start, start), min(cov_end, end)
    if hi < lo:
        return ZERO
    total_days = (cov_end - cov_start).days + 1
    return Decimal(amount) * Decimal((hi - lo).days + 1) / Decimal(total_days)


def _overlapping(qs, start, end):
    """صفوف تاريخ دفعها في الفترة أو فترة تغطيتها بتتقاطع معاها."""
    return qs.filter(Q(period_start__isnull=True, date__gte=start, date__lte=end)
                     | Q(period_start__lte=end, period_end__gte=start))


def _sum(qs, field):
    return qs.aggregate(s=Sum(field))['s'] or ZERO


# ─────────────────────────────── الإيراد ───────────────────────────────
def _paid_invoices():
    from clients.models import PlatformInvoice
    return PlatformInvoice.objects.filter(status='paid', paid_at__isnull=False)


def _invoice_cover(inv):
    if inv.period_start and inv.period_end and inv.period_end >= inv.period_start:
        return inv.period_start, inv.period_end
    d = inv.paid_at.date()
    return d, d


def subscription_revenue(start, end, basis=ACCRUAL):
    if basis == CASH:
        return _sum(_paid_invoices().filter(paid_at__date__gte=start, paid_at__date__lte=end), 'total')
    total = ZERO
    for inv in _paid_invoices().filter(Q(period_start__lte=end, period_end__gte=start)
                                       | Q(paid_at__date__gte=start, paid_at__date__lte=end)).only(
            'total', 'period_start', 'period_end', 'paid_at'):
        total += _share(inv.total, *_invoice_cover(inv), start, end)
    return total


def commission_revenue(start, end):
    from clients.models import PartOrder
    return _sum(PartOrder.objects.filter(status='released', released_at__date__gte=start,
                                         released_at__date__lte=end), 'commission_amount')


def _money_rows(model, start, end, basis, **filters):
    qs = model.objects.filter(**filters)
    if basis == CASH:
        return [(r, r.amount_egp) for r in qs.filter(date__gte=start, date__lte=end)]
    return [(r, _share(r.amount_egp, *r.coverage, start, end)) for r in _overlapping(qs, start, end)]


def revenue(start, end, basis=ACCRUAL):
    manual = financing = ZERO
    for row, amount in _money_rows(PlatformIncome, start, end, basis):
        if row.category in NON_OPERATING_INCOME:
            financing += amount
        else:
            manual += amount
    subs = subscription_revenue(start, end, basis)
    comm = commission_revenue(start, end)
    return {'subscriptions': subs.quantize(CENT), 'commissions': comm, 'manual': manual.quantize(CENT),
            'total': (subs + comm + manual).quantize(CENT), 'financing': financing.quantize(CENT)}


# ─────────────────────────────── المصروف ───────────────────────────────
def expenses(start, end, basis=ACCRUAL):
    """المصروفات بالمجموعة والبند (الأكبر الأول)."""
    labels = {k: str(lbl) for k, lbl, _g in EXPENSE_CATEGORIES}
    per_cat = {}
    for row, amount in _money_rows(PlatformExpense, start, end, basis):
        per_cat[row.category] = per_cat.get(row.category, ZERO) + amount
    by_group = {g: ZERO for g, _l in EXPENSE_GROUPS}
    by_category = []
    for key, amount in sorted(per_cat.items(), key=lambda kv: -kv[1]):
        amount = amount.quantize(CENT)
        if not amount:
            continue
        g = CATEGORY_GROUP.get(key, 'ga')
        by_group[g] += amount
        by_category.append({'key': key, 'label': labels.get(key, key), 'group': g, 'total': amount})
    total = sum(by_group.values(), ZERO)
    for c in by_category:
        c['pct'] = round(float(c['total'] * 100 / total), 1) if total else 0
    return {'by_group': by_group, 'by_category': by_category, 'total': total}


# ─────────────────────────────── قايمة الدخل ───────────────────────────────
def _pct(part, whole):
    return round(float(part * 100 / whole), 1) if whole else None


def change_pct(now, before):
    if not before:
        return None
    return round(float((now - before) * 100 / abs(before)), 1)


def profit_and_loss(start, end, basis=ACCRUAL):
    rev = revenue(start, end, basis)
    exp = expenses(start, end, basis)
    cogs = exp['by_group']['cogs']
    gross = rev['total'] - cogs
    opex = exp['total'] - cogs
    net = rev['total'] - exp['total']
    return {'start': start, 'end': end, 'basis': basis, 'revenue': rev, 'expenses': exp,
            'cogs': cogs, 'gross_profit': gross, 'gross_margin': _pct(gross, rev['total']),
            'opex': opex, 'opex_ratio': _pct(opex, rev['total']),
            'net_profit': net, 'net_margin': _pct(net, rev['total'])}


def monthly_series(months=12, today=None, basis=ACCRUAL):
    """آخر N شهر: [{'month', 'revenue', 'expenses', 'net'}] — للرسم."""
    today = today or datetime.date.today()
    out = []
    for i in range(months - 1, -1, -1):
        m_start, m_end = month_bounds(add_months(today.replace(day=1), -i))
        rev = revenue(m_start, m_end, basis)['total']
        exp = expenses(m_start, m_end, basis)['total']
        out.append({'month': m_start, 'revenue': rev, 'expenses': exp, 'net': rev - exp})
    return out


# ─────────────────────────────── الميزانية المختصرة ───────────────────────────────
def balance_items(today=None):
    """إيراد مؤجّل (اشتراكات اتدفعت عن فترة لسه ماجتش) ومصروفات مدفوعة مقدّماً."""
    today = today or datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    far = datetime.date(9999, 12, 31)
    deferred = sum((_share(inv.total, *_invoice_cover(inv), tomorrow, far)
                    for inv in _paid_invoices().filter(period_end__gt=today, paid_at__date__lte=today)), ZERO)
    prepaid = sum((_share(e.amount_egp, *e.coverage, tomorrow, far)
                   for e in PlatformExpense.objects.filter(period_end__gt=today, date__lte=today)), ZERO)
    return {'deferred_revenue': deferred.quantize(CENT), 'prepaid_expenses': prepaid.quantize(CENT)}


# ─────────────────────────────── مؤشرات SaaS ───────────────────────────────
def _monthly_by_tenant(day):
    """{tenant_id: إيراد شهري} للشركات اللي عندها اشتراك مدفوع بيغطي اليوم ده (أحدث فاتورة لكل شركة)."""
    out = {}
    for inv in (_paid_invoices().filter(period_start__lte=day, period_end__gte=day)
                .order_by('period_start').only('tenant_id', 'total', 'billing_cycle_months')):
        out[inv.tenant_id] = Decimal(inv.total) / max(inv.billing_cycle_months or 1, 1)
    return out


def kpis(today=None, cash_balance=None):
    """Unit economics زي ما المستثمرين والشركات العالمية بتقيس:
    MRR/ARR ونموّه، العملاء النشطين، ARPA، معدل الإلغاء (churn)، LTV، CAC، LTV:CAC، فترة استرداد
    CAC، Burn وصافيه، Runway، وقاعدة الـ 40."""
    from clients.models import Client
    today = today or datetime.date.today()
    now = _monthly_by_tenant(today)
    month_ago = _monthly_by_tenant(today - datetime.timedelta(days=30))
    mrr = sum(now.values(), ZERO).quantize(CENT)
    mrr_prev = sum(month_ago.values(), ZERO).quantize(CENT)
    active = len(now)
    arpa = (mrr / active).quantize(CENT) if active else None
    lost = [t for t in month_ago if t not in now]
    churn = round(len(lost) * 100 / len(month_ago), 1) if month_ago else None
    mrr_growth = change_pct(mrr, mrr_prev)

    # آخر ٩٠ يوم (٣ شهور) — أهدى من شهر واحد
    t_start = today - datetime.timedelta(days=89)
    pnl3 = profit_and_loss(t_start, today, ACCRUAL)
    gm = (pnl3['gross_margin'] or 0) / 100
    cash_out = _sum(PlatformExpense.objects.filter(date__gte=t_start, date__lte=today), 'amount_egp')
    cash_in = revenue(t_start, today, CASH)['total']
    burn = (cash_out / 3).quantize(CENT)
    net_burn = ((cash_out - cash_in) / 3).quantize(CENT)

    # CAC = مصروف التسويق والمبيعات ÷ العملاء الجداد (أول فاتورة مدفوعة ليهم) في نفس الـ ٩٠ يوم
    sm_spend = pnl3['expenses']['by_group']['marketing']
    first_paid = (_paid_invoices().values('tenant_id').annotate(first=Min('paid_at'))
                  .filter(first__date__gte=t_start, first__date__lte=today))
    new_customers = first_paid.count()
    cac = (sm_spend / new_customers).quantize(CENT) if new_customers else None
    ltv = None
    if arpa and churn:
        ltv = (arpa * Decimal(str(gm)) / Decimal(str(churn / 100))).quantize(CENT)
    ltv_cac = round(float(ltv / cac), 1) if (ltv and cac) else None
    payback = round(float(cac / (arpa * Decimal(str(gm)))), 1) if (cac and arpa and gm > 0) else None

    runway = None
    if cash_balance is not None and net_burn > 0:
        runway = round(float(Decimal(cash_balance) / net_burn), 1)
    rule_of_40 = None
    if mrr_growth is not None and pnl3['net_margin'] is not None:
        yearly_growth = ((1 + mrr_growth / 100) ** 12 - 1) * 100
        rule_of_40 = round(yearly_growth + pnl3['net_margin'], 1)
    month_start, month_end = month_bounds(today)
    return {
        'mrr': mrr, 'arr': mrr * 12, 'mrr_prev': mrr_prev, 'mrr_growth': mrr_growth,
        'active_customers': active, 'arpa': arpa, 'churn': churn, 'lost_customers': len(lost),
        'gross_margin': pnl3['gross_margin'], 'ltv': ltv, 'cac': cac, 'ltv_cac': ltv_cac,
        'payback_months': payback, 'sm_spend': sm_spend, 'new_customers': new_customers,
        'new_signups': Client.objects.exclude(schema_name='public').filter(
            created_on__gte=month_start, created_on__lte=month_end).count(),
        'burn': burn, 'net_burn': net_burn, 'runway_months': runway, 'rule_of_40': rule_of_40,
    }


# ─────────────────────────────── المتكرر ───────────────────────────────
def generate_recurring(today=None):
    """ينسخ المصروفات المتكررة لكل شهر فات من غير نسخة لحد الشهر الحالي (مرة واحدة لكل شهر).

    بيشتغل كل ما صفحة الحسابات تتفتح — فلو الصفحة ماتفتحتش شهرين، الشهرين بيتسجّلوا.
    `recurring_until` بيوقف التكرار عند تاريخ معيّن (مثلاً عقد ٦ شهور).
    """
    today = today or datetime.date.today()
    created = 0
    for src in PlatformExpense.objects.filter(is_recurring=True, recurring_source__isnull=True,
                                              date__lt=today.replace(day=1)):
        stop = min(today, src.recurring_until) if src.recurring_until else today
        month = add_months(src.date.replace(day=1), 1)
        while month <= stop:
            day = min(src.date.day, calendar.monthrange(month.year, month.month)[1])
            target = month.replace(day=day)
            if target > stop:
                break
            try:
                with transaction.atomic():
                    PlatformExpense.objects.create(
                        date=target, category=src.category, description=src.description, vendor=src.vendor,
                        amount=src.amount, currency=src.currency, exchange_rate=src.exchange_rate,
                        payment_method=src.payment_method, notes=src.notes, recurring_source=src,
                        created_by=src.created_by)
                created += 1
            except IntegrityError:
                pass   # اتنسخ قبل كده للشهر ده
            month = add_months(month, 1)
    return created
