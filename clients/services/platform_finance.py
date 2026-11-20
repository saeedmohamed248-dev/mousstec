"""
📒 قايمة الدخل (P&L) لمنصة Mouss Tec — زي شركات SaaS العالمية.

الإيراد (أوتوماتيك من السيستم + اليدوي):
  * اشتراكات الشركات: فواتير PlatformInvoice المدفوعة (بتاريخ الدفع).
  * عمولات سوق القطع: PartOrder اللي اتفرج عنها للبائع (بتاريخ الإفراج).
  * إيرادات يدوية (PlatformIncome) — ماعدا «تمويل/استثمار» لأنه مش إيراد تشغيل.
المصروف: PlatformExpense بالجنيه (amount_egp متثبّت بسعر الصرف وقت الدفع).

الترتيب: إيراد ← تكلفة تشغيل الخدمة (COGS) ← مجمل الربح ← مصروفات تشغيلية ← صافي الربح.
"""
import calendar
import datetime
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.db.models.functions import TruncMonth

from clients.models.platform_finance import (
    CATEGORY_GROUP, EXPENSE_CATEGORIES, EXPENSE_GROUPS, PlatformExpense, PlatformIncome,
)

ZERO = Decimal('0')
NON_OPERATING_INCOME = ('investment',)


def _sum(qs, field):
    return qs.aggregate(s=Sum(field))['s'] or ZERO


def _subscriptions(start, end):
    from clients.models import PlatformInvoice
    return PlatformInvoice.objects.filter(status='paid', paid_at__date__gte=start, paid_at__date__lte=end)


def _commissions(start, end):
    from clients.models import PartOrder
    return PartOrder.objects.filter(status='released', released_at__date__gte=start, released_at__date__lte=end)


def revenue(start, end):
    """{'subscriptions', 'commissions', 'manual', 'total'} + التمويل (برّه الإيراد)."""
    manual_qs = PlatformIncome.objects.filter(date__gte=start, date__lte=end)
    subs = _sum(_subscriptions(start, end), 'total')
    comm = _sum(_commissions(start, end), 'commission_amount')
    manual = _sum(manual_qs.exclude(category__in=NON_OPERATING_INCOME), 'amount_egp')
    return {'subscriptions': subs, 'commissions': comm, 'manual': manual, 'total': subs + comm + manual,
            'financing': _sum(manual_qs.filter(category__in=NON_OPERATING_INCOME), 'amount_egp')}


def expenses(start, end):
    """المصروفات بالمجموعة والبند (الأكبر الأول)."""
    rows = (PlatformExpense.objects.filter(date__gte=start, date__lte=end)
            .values('category').annotate(s=Sum('amount_egp')).order_by('-s'))
    labels = {k: str(lbl) for k, lbl, _g in EXPENSE_CATEGORIES}
    by_group = {g: ZERO for g, _l in EXPENSE_GROUPS}
    by_category = []
    for r in rows:
        g = CATEGORY_GROUP.get(r['category'], 'ga')
        by_group[g] += r['s']
        by_category.append({'key': r['category'], 'label': labels.get(r['category'], r['category']),
                            'group': g, 'total': r['s']})
    total = sum(by_group.values(), ZERO)
    for c in by_category:
        c['pct'] = round(float(c['total'] * 100 / total), 1) if total else 0
    return {'by_group': by_group, 'by_category': by_category, 'total': total}


def _pct(part, whole):
    return round(float(part * 100 / whole), 1) if whole else None


def profit_and_loss(start, end):
    rev = revenue(start, end)
    exp = expenses(start, end)
    cogs = exp['by_group']['cogs']
    gross = rev['total'] - cogs
    opex = exp['total'] - cogs
    net = rev['total'] - exp['total']
    return {'start': start, 'end': end, 'revenue': rev, 'expenses': exp,
            'cogs': cogs, 'gross_profit': gross, 'gross_margin': _pct(gross, rev['total']),
            'opex': opex, 'net_profit': net, 'net_margin': _pct(net, rev['total'])}


def change_pct(now, before):
    if not before:
        return None
    return round(float((now - before) * 100 / abs(before)), 1)


def previous_period(start, end):
    days = (end - start).days + 1
    prev_end = start - datetime.timedelta(days=1)
    return prev_end - datetime.timedelta(days=days - 1), prev_end


def month_bounds(d):
    return d.replace(day=1), d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))


def monthly_series(months=12, today=None):
    """آخر N شهر: [{'month', 'revenue', 'expenses', 'net'}] — للرسم."""
    today = today or datetime.date.today()
    first = add_months(today.replace(day=1), -(months - 1))
    last = month_bounds(today)[1]
    buckets = {add_months(first, i): {'revenue': ZERO, 'expenses': ZERO} for i in range(months)}

    def _add(rows, key):
        for r in rows:
            m = r['m'].date() if hasattr(r['m'], 'date') else r['m']
            if m in buckets:
                buckets[m][key] += r['s'] or ZERO

    _add(_subscriptions(first, last).annotate(m=TruncMonth('paid_at')).values('m').annotate(s=Sum('total')), 'revenue')
    _add(_commissions(first, last).annotate(m=TruncMonth('released_at')).values('m')
         .annotate(s=Sum('commission_amount')), 'revenue')
    _add(PlatformIncome.objects.filter(date__gte=first, date__lte=last).exclude(category__in=NON_OPERATING_INCOME)
         .annotate(m=TruncMonth('date')).values('m').annotate(s=Sum('amount_egp')), 'revenue')
    _add(PlatformExpense.objects.filter(date__gte=first, date__lte=last)
         .annotate(m=TruncMonth('date')).values('m').annotate(s=Sum('amount_egp')), 'expenses')
    return [{'month': m, 'revenue': v['revenue'], 'expenses': v['expenses'], 'net': v['revenue'] - v['expenses']}
            for m, v in sorted(buckets.items())]


def kpis(today=None, cash_balance=None):
    """مؤشرات SaaS: MRR، معدل الحرق، تكلفة العميل الجديد، والمدة اللي الفلوس تكفّيها."""
    from clients.models import Client
    today = today or datetime.date.today()
    # آخر ٣ شهور كاملة ⇒ متوسط شهري (أهدى من شهر واحد)
    this_month_start = today.replace(day=1)
    start3 = add_months(this_month_start, -3)
    end3 = this_month_start - datetime.timedelta(days=1)
    exp3 = _sum(PlatformExpense.objects.filter(date__gte=start3, date__lte=end3), 'amount_egp')
    rev3 = revenue(start3, end3)['total']
    burn = (exp3 / 3).quantize(Decimal('0.01'))
    net_burn = ((exp3 - rev3) / 3).quantize(Decimal('0.01'))

    mrr = ZERO
    for inv in _subscriptions(add_months(today, -12), today).only('total', 'billing_cycle_months', 'period_end'):
        if inv.period_end and inv.period_end >= today:
            mrr += inv.total / max(inv.billing_cycle_months or 1, 1)
    mrr = mrr.quantize(Decimal('0.01'))

    month_start, month_end = month_bounds(today)
    ads = _sum(PlatformExpense.objects.filter(date__gte=month_start, date__lte=month_end,
                                              category__in=('ads', 'content', 'sales_commission', 'events')),
               'amount_egp')
    new_paying = (_subscriptions(month_start, month_end).values('tenant_id').distinct().count())
    new_signups = Client.objects.exclude(schema_name='public').filter(
        created_on__gte=month_start, created_on__lte=month_end).count() if hasattr(Client, 'created_on') else 0

    runway = None
    if cash_balance is not None and net_burn > 0:
        runway = round(float(Decimal(cash_balance) / net_burn), 1)
    return {'mrr': mrr, 'arr': mrr * 12, 'burn': burn, 'net_burn': net_burn,
            'marketing_this_month': ads, 'new_signups': new_signups, 'new_paying': new_paying,
            'cac': (ads / new_paying).quantize(Decimal('0.01')) if new_paying else None,
            'runway_months': runway}


def generate_recurring(today=None):
    """ينسخ المصروفات المتكررة لكل شهر فات من غير نسخة لحد الشهر الحالي (مرة واحدة لكل شهر).

    بيشتغل كل ما صفحة الحسابات تتفتح — فلو الصفحة ماتفتحتش شهرين، الشهرين بيتسجّلوا.
    """
    today = today or datetime.date.today()
    created = 0
    for src in PlatformExpense.objects.filter(is_recurring=True, recurring_source__isnull=True,
                                              date__lt=today.replace(day=1)):
        month = add_months(src.date.replace(day=1), 1)
        while month <= today:
            day = min(src.date.day, calendar.monthrange(month.year, month.month)[1])
            target = month.replace(day=day)
            if target > today:
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
