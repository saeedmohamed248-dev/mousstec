"""
📲 ملخص اليوم للمالك على واتساب — كل يوم 9 بالليل (clients.tasks.send_daily_owner_summaries).

الفكرة: المالك يفتح السيستم كل يوم حتى لو مش هو اللي بيبيع — «مبيعات النهارده كذا، الخزنة
فيها كذا، فلان عليه كذا». ده اللي بيخلّي السيستم جزء من يومه.

* بيتبعت بس لو حصل شغل النهارده (مانزعجش حد بأصفار).
* المالك يقدر يقفله: Client.daily_summary_whatsapp.
* Meta مابتسمحش برسالة نصية برّه نافذة الـ 24 ساعة — لو WHATSAPP_DAILY_SUMMARY_TEMPLATE
  متظبط بنبعت القالب (المتغيّرات بالترتيب تحت)، وإلا رسالة نصية.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

D0 = Decimal('0')


def _fmt(v):
    v = Decimal(v or 0).quantize(Decimal('1'))
    return f"{v:,}"


def _today_range():
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    start = timezone.make_aware(datetime.combine(today, time.min), tz)
    return today, start, start + timedelta(days=1)


def _auto(start, end):
    from inventory.models import Customer, FinancialTransaction, Inventory, SaleInvoice, Treasury
    from django.db.models import F
    sales = SaleInvoice.objects.filter(is_return=False, created_at__gte=start, created_at__lt=end) \
        .exclude(status='quotation').aggregate(n=Count('id'), t=Sum('total_amount'))
    collected = FinancialTransaction.objects.filter(
        transaction_type='in', date__gte=start, date__lt=end).aggregate(t=Sum('amount'))['t'] or D0
    cash = Treasury.objects.filter(is_active=True).aggregate(t=Sum('balance'))['t'] or D0
    debt = Customer.objects.filter(balance__gt=0).aggregate(t=Sum('balance'), n=Count('id'))
    low = Inventory.objects.filter(product__min_stock_level__gt=0,
                                   quantity__lte=F('product__min_stock_level')).count()
    return {
        'count': sales['n'] or 0, 'sales': sales['t'] or D0, 'collected': collected, 'cash': cash,
        'debt': debt['t'] or D0, 'debtors': debt['n'] or 0, 'extra': low,
        'extra_label': 'صنف وصل لحد التنبيه',
    }


def _print(start, end):
    from printing.models import PrintJob, PrintOrder, PrintTransaction, PrintTreasury
    orders = PrintOrder.objects.filter(created_at__gte=start, created_at__lt=end) \
        .exclude(status__in=('draft', 'cancelled')).aggregate(n=Count('id'), t=Sum('total_amount'))
    collected = PrintTransaction.objects.filter(
        transaction_type='in', date__gte=start, date__lt=end).aggregate(t=Sum('amount'))['t'] or D0
    cash = PrintTreasury.objects.aggregate(t=Sum('balance'))['t'] or D0
    open_orders = [o for o in PrintOrder.objects.exclude(status__in=('draft', 'cancelled'))
                   .only('id', 'total_amount', 'discount', 'paid_amount', 'customer_id')
                   if o.remaining > 0]
    in_production = PrintJob.objects.filter(is_complete=False) \
        .exclude(order__status__in=('draft', 'cancelled', 'delivered')).count()
    return {
        'count': orders['n'] or 0, 'sales': orders['t'] or D0, 'collected': collected, 'cash': cash,
        'debt': sum((o.remaining for o in open_orders), D0),
        'debtors': len({o.customer_id for o in open_orders}), 'extra': in_production,
        'extra_label': 'مهمة لسه في الإنتاج',
    }


def build_summary(tenant):
    """dict الأرقام + text الرسالة، أو None لو مفيش شغل النهارده."""
    today, start, end = _today_range()
    is_print = getattr(tenant, 'industry', '') == 'printing'
    d = _print(start, end) if is_print else _auto(start, end)
    if not d['count'] and not d['collected']:
        return None
    noun = 'طلب' if is_print else 'فاتورة'
    lines = [
        f"📊 ملخص النهارده — {tenant.name} ({today:%Y-%m-%d})",
        f"🧾 المبيعات: {_fmt(d['sales'])} ج ({d['count']} {noun})",
        f"💵 اتحصّل: {_fmt(d['collected'])} ج",
        f"🏦 الخزنة دلوقتي: {_fmt(d['cash'])} ج",
        f"📒 الآجل على العملاء: {_fmt(d['debt'])} ج ({d['debtors']} عميل)",
    ]
    if d['extra']:
        lines.append(f"⚠️ {d['extra']} {d['extra_label']}")
    lines.append("— Mouss Tec")
    d['text'] = "\n".join(lines)
    d['date'] = today
    d['subject'] = f"📊 ملخص النهارده — {tenant.name} ({today:%Y-%m-%d})"
    d['rows'] = [('🧾 المبيعات', f"{_fmt(d['sales'])} ج ({d['count']} {noun})"),
                 ('💵 اتحصّل', f"{_fmt(d['collected'])} ج"),
                 ('🏦 الخزنة دلوقتي', f"{_fmt(d['cash'])} ج"),
                 ('📒 الآجل على العملاء', f"{_fmt(d['debt'])} ج ({d['debtors']} عميل)")]
    # بنفس ترتيب متغيّرات قالب Meta: الاسم، المبيعات، العدد، المحصّل، الخزنة، الآجل
    d['params'] = [tenant.name, _fmt(d['sales']), d['count'], _fmt(d['collected']), _fmt(d['cash']), _fmt(d['debt'])]
    return d


def summary_email_html(tenant, d):
    """نسخة HTML بسيطة من الملخص للإيميل (RTL) — النص العادي بيتبعت معاها."""
    from django.utils.html import escape
    rows = ''.join(
        f'<tr><td style="padding:10px 14px;color:#475467;border-bottom:1px solid #EAECF0">{escape(k)}</td>'
        f'<td style="padding:10px 14px;font-weight:700;color:#1E2A57;border-bottom:1px solid #EAECF0" dir="rtl">{escape(v)}</td></tr>'
        for k, v in d['rows'])
    extra = (f'<p style="color:#B54708;margin:14px 0 0">⚠️ {d["extra"]} {escape(d["extra_label"])}</p>'
             if d.get('extra') else '')
    return (f'<div dir="rtl" style="font-family:Tahoma,Arial,sans-serif;background:#F6F3EE;padding:24px">'
            f'<div style="max-width:520px;margin:auto;background:#fff;border-radius:14px;padding:22px;border:1px solid #E6E1D8">'
            f'<h2 style="margin:0 0 4px;color:#1E2A57">📊 ملخص النهارده</h2>'
            f'<p style="margin:0 0 16px;color:#667085">{escape(tenant.name)} · <span dir="ltr">{d["date"]:%Y-%m-%d}</span></p>'
            f'<table style="width:100%;border-collapse:collapse;font-size:15px">{rows}</table>{extra}'
            f'<p style="margin:20px 0 0;color:#98A2B3;font-size:12px">Mouss Tec — بيوصلك كل يوم فيه شغل. '
            f'تقدر تقفله من الدعم الفني.</p></div></div>')
