"""
🧠 المستشار الذكي (AI Business Advisor)
=======================================
بيجمّع صورة حيّة من السيستم (مبيعات · مشتريات · مخزون · عملاء) ويحوّلها إلى:
  1) مؤشرات (KPIs) زي الشركات العالمية.
  2) ملاحظات وتوصيات بقواعد ذكية (تشتغل دايماً حتى بدون AI).
  3) تحليل سردي + خطة عمل عبر نموذج اللغة (Together/Llama) — لو الـ AI مفعّل.

مربوط بالسيستم بالكامل: كل نداء بيقرأ البيانات الحيّة (مفيش بيانات مخزّنة قديمة).
"""
import logging
from decimal import Decimal
from datetime import timedelta

from django.db.models import Sum, Count, F, Q, DecimalField, ExpressionWrapper
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')

_D0 = Decimal('0')


def _f(v):
    """Decimal/None → float نظيف للـ JSON."""
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


def build_snapshot(branch=None, days=30):
    """يبني لقطة مؤشرات شاملة عن الشركة/الفرع لآخر `days` يوم + مقارنة بالفترة السابقة."""
    from inventory.models import (
        SaleInvoice, SaleInvoiceItem, PurchaseInvoice,
        Inventory, Product, Customer,
    )

    now = timezone.now()
    start = now - timedelta(days=days)
    prev_start = now - timedelta(days=days * 2)

    # ---------- المبيعات (الفترة الحالية + السابقة للمقارنة) ----------
    sales = SaleInvoice.objects.exclude(status='quotation')
    purch = PurchaseInvoice.objects.all()
    inv_rows = Inventory.objects.all()
    if branch is not None:
        sales = sales.filter(branch=branch)
        purch = purch.filter(branch=branch)
        inv_rows = inv_rows.filter(branch=branch)

    def _sales_agg(qs, s, e):
        a = qs.filter(date_created__gte=s, date_created__lt=e).aggregate(
            rev=Sum('total_amount', filter=Q(is_return=False)),
            ret=Sum('total_amount', filter=Q(is_return=True)),
            profit=Sum('net_profit', filter=Q(is_return=False)),
            paid=Sum('paid_amount'),
            cnt=Count('id', filter=Q(is_return=False)),
        )
        rev = (a['rev'] or _D0) - (a['ret'] or _D0)
        return {
            'net_sales': rev, 'returns': a['ret'] or _D0,
            'profit': a['profit'] or _D0, 'paid': a['paid'] or _D0,
            'count': a['cnt'] or 0,
        }

    cur = _sales_agg(sales, start, now)
    prev = _sales_agg(sales, prev_start, start)

    net_sales = cur['net_sales']
    profit = cur['profit']
    margin = (profit / net_sales * 100) if net_sales else _D0
    avg_ticket = (net_sales / cur['count']) if cur['count'] else _D0
    sales_change = (((net_sales - prev['net_sales']) / prev['net_sales'] * 100)
                    if prev['net_sales'] else _D0)
    due_period = net_sales - cur['paid']

    # ---------- المشتريات ----------
    pa = purch.filter(date_created__gte=start).aggregate(
        total=Sum('total_amount'), cnt=Count('id'))
    purchases_total = pa['total'] or _D0
    purchases_count = pa['cnt'] or 0

    # ---------- المخزون ----------
    cost_expr = ExpressionWrapper(
        F('quantity') * F('product__average_cost'),
        output_field=DecimalField(max_digits=16, decimal_places=2))
    retail_expr = ExpressionWrapper(
        F('quantity') * F('product__retail_price'),
        output_field=DecimalField(max_digits=16, decimal_places=2))
    inv_agg = inv_rows.aggregate(
        stock_cost=Sum(cost_expr),
        stock_retail=Sum(retail_expr),
        units=Sum('quantity'),
        skus=Count('product', distinct=True),
    )
    low_stock = inv_rows.filter(
        quantity__gt=0, quantity__lte=F('product__min_stock_level')).count()
    out_of_stock = inv_rows.filter(quantity__lte=0).count()

    # بضاعة راكدة: عندها رصيد بس مفيش مبيعات آخر 90 يوم
    sold_90_ids = set(
        SaleInvoiceItem.objects.filter(
            invoice__date_created__gte=now - timedelta(days=90),
            invoice__is_return=False,
            **({'invoice__branch': branch} if branch is not None else {}),
        ).values_list('product_id', flat=True)
    )
    dead_qs = inv_rows.filter(quantity__gt=0).exclude(product_id__in=sold_90_ids)
    dead_agg = dead_qs.aggregate(
        n=Count('product', distinct=True), val=Sum(cost_expr))
    dead_count = dead_agg['n'] or 0
    dead_value = dead_agg['val'] or _D0

    # ---------- أعلى المنتجات بالإيراد (الفترة) ----------
    line_rev = ExpressionWrapper(
        F('quantity') * F('unit_price') - F('discount'),
        output_field=DecimalField(max_digits=16, decimal_places=2))
    top_items = (
        SaleInvoiceItem.objects.filter(
            invoice__in=sales.filter(date_created__gte=start, is_return=False))
        .values('product__name')
        .annotate(qty=Sum('quantity'), rev=Sum(line_rev))
        .order_by('-rev')[:7]
    )
    top_products = [{'name': r['product__name'] or '—',
                     'qty': r['qty'] or 0, 'revenue': _f(r['rev'])}
                    for r in top_items]

    # ---------- أعلى العملاء بالمبيعات (الفترة) ----------
    top_cust = (
        sales.filter(date_created__gte=start, is_return=False)
        .values('customer__name')
        .annotate(total=Sum('total_amount'))
        .order_by('-total')[:5]
    )
    top_customers = [{'name': r['customer__name'] or '—', 'total': _f(r['total'])}
                     for r in top_cust]

    # ---------- إجمالي الآجل على العملاء ----------
    cust_qs = Customer.objects.all()
    receivables = cust_qs.filter(balance__gt=0).aggregate(s=Sum('balance'))['s'] or _D0

    return {
        'period_days': days,
        'branch': branch.name if branch is not None else 'كل الفروع',
        'generated_at': now.strftime('%Y-%m-%d %H:%M'),
        'sales': {
            'net_sales': _f(net_sales), 'profit': _f(profit),
            'margin_pct': _f(margin), 'invoices': cur['count'],
            'avg_ticket': _f(avg_ticket), 'returns': _f(cur['returns']),
            'due_this_period': _f(due_period),
            'change_vs_prev_pct': _f(sales_change),
            'prev_net_sales': _f(prev['net_sales']),
        },
        'purchases': {'total': _f(purchases_total), 'invoices': purchases_count},
        'inventory': {
            'skus': inv_agg['skus'] or 0, 'units': inv_agg['units'] or 0,
            'stock_value_cost': _f(inv_agg['stock_cost']),
            'stock_value_retail': _f(inv_agg['stock_retail']),
            'low_stock_items': low_stock, 'out_of_stock_items': out_of_stock,
            'dead_stock_items': dead_count, 'dead_stock_value': _f(dead_value),
        },
        'receivables_total': _f(receivables),
        'top_products': top_products,
        'top_customers': top_customers,
    }


def rule_based_insights(snap):
    """ملاحظات وتوصيات محسوبة بقواعد ذكية — تشتغل دايماً حتى بدون AI."""
    out = []
    s, inv = snap['sales'], snap['inventory']

    if s['change_vs_prev_pct'] <= -10:
        out.append({'level': 'danger', 'icon': 'fa-arrow-trend-down',
                    'title': 'المبيعات نازلة',
                    'text': f"المبيعات قلّت {abs(s['change_vs_prev_pct']):.0f}% عن الفترة السابقة. "
                            f"راجع أسباب التراجع (توريد ناقص؟ أسعار؟ موسم؟) وفعّل عروض سريعة."})
    elif s['change_vs_prev_pct'] >= 10:
        out.append({'level': 'good', 'icon': 'fa-arrow-trend-up',
                    'title': 'المبيعات في تحسّن',
                    'text': f"المبيعات زادت {s['change_vs_prev_pct']:.0f}% عن الفترة السابقة — "
                            f"ثبّت الأصناف الأكثر مبيعاً وتأكد من توفّر مخزونها."})

    if s['net_sales'] > 0 and s['margin_pct'] < 15:
        out.append({'level': 'warning', 'icon': 'fa-percent',
                    'title': 'هامش الربح منخفض',
                    'text': f"هامش الربح {s['margin_pct']:.1f}% فقط. راجع تسعير الأصناف وتكلفة الشراء، "
                            f"وقلّل الخصومات على البنود قليلة الهامش."})

    if inv['low_stock_items'] > 0:
        out.append({'level': 'warning', 'icon': 'fa-triangle-exclamation',
                    'title': 'أصناف تحت حد الأمان',
                    'text': f"{inv['low_stock_items']} صنف قرب ينفد. جهّز طلب توريد قبل ما تخسر مبيعات."})

    if inv['dead_stock_value'] > 0 and inv['dead_stock_items'] >= 5:
        out.append({'level': 'warning', 'icon': 'fa-box-open',
                    'title': 'بضاعة راكدة',
                    'text': f"{inv['dead_stock_items']} صنف بقيمة {inv['dead_stock_value']:.0f} ج.م "
                            f"من غير مبيعات آخر 90 يوم — اعمل عرض/خصم لتسييل الكاش المجمّد."})

    if snap['receivables_total'] > 0:
        out.append({'level': 'info', 'icon': 'fa-hand-holding-dollar',
                    'title': 'آجل على العملاء',
                    'text': f"إجمالي {snap['receivables_total']:.0f} ج.م مستحقة على العملاء. "
                            f"فعّل خطة تحصيل للعملاء الأعلى مديونية لتحسين السيولة."})

    if not out:
        out.append({'level': 'good', 'icon': 'fa-circle-check',
                    'title': 'الوضع مستقر',
                    'text': 'المؤشرات في نطاق صحّي. ركّز على تنمية المبيعات وتوسيع الأصناف الأكثر ربحاً.'})
    return out


def _snapshot_brief(snap):
    """يحوّل اللقطة لنص مضغوط يتبعت للنموذج (توفير توكنز)."""
    s, p, inv = snap['sales'], snap['purchases'], snap['inventory']
    tp = '؛ '.join(f"{r['name']} ({r['revenue']:.0f})" for r in snap['top_products'][:5]) or '—'
    tc = '؛ '.join(f"{r['name']} ({r['total']:.0f})" for r in snap['top_customers'][:5]) or '—'
    return (
        f"الفرع: {snap['branch']} | الفترة: آخر {snap['period_days']} يوم | التاريخ: {snap['generated_at']}\n"
        f"المبيعات الصافية: {s['net_sales']:.0f} ج.م (تغيّر {s['change_vs_prev_pct']:+.0f}% عن الفترة السابقة {s['prev_net_sales']:.0f})\n"
        f"الربح: {s['profit']:.0f} ج.م | الهامش: {s['margin_pct']:.1f}% | عدد الفواتير: {s['invoices']} | متوسط الفاتورة: {s['avg_ticket']:.0f}\n"
        f"المرتجعات: {s['returns']:.0f} | آجل الفترة: {s['due_this_period']:.0f} | إجمالي الآجل على العملاء: {snap['receivables_total']:.0f}\n"
        f"المشتريات: {p['total']:.0f} ج.م ({p['invoices']} فاتورة)\n"
        f"المخزون: {inv['skus']} صنف / {inv['units']} قطعة | قيمته بالتكلفة {inv['stock_value_cost']:.0f} وبالبيع {inv['stock_value_retail']:.0f}\n"
        f"نواقص: {inv['low_stock_items']} تحت الأمان، {inv['out_of_stock_items']} نافد | راكد: {inv['dead_stock_items']} صنف بقيمة {inv['dead_stock_value']:.0f}\n"
        f"أعلى المنتجات: {tp}\n"
        f"أعلى العملاء: {tc}"
    )


_SYSTEM_PROMPT = (
    "أنت مستشار أعمال خبير لمحل قطع غيار/ورشة سيارات في مصر. بتتكلم عربي مصري "
    "واضح ومباشر وعملي. مهمتك تحلّل بيانات الشركة الحقيقية وتدّي توصيات قابلة "
    "للتنفيذ بأرقامها. ممنوع تخترع أرقام مش موجودة في البيانات؛ استخدم بس اللي "
    "اتعطى لك. لو البيانات ناقصة قول كده. ركّز على: قراية الوضع، أهم المخاطر "
    "والفرص، وخطوات عملية مرتّبة بالأولوية."
)


def ai_analysis(snap, question=None, history=None):
    """يستدعي نموذج اللغة لتحليل اللقطة أو الرد على سؤال. يرجّع نص أو None لو AI مطفي/فشل."""
    from inventory.ai_services import call_llm_layer

    brief = _snapshot_brief(snap)
    if question:
        user = (f"دي بيانات شركتي الحيّة:\n{brief}\n\n"
                f"سؤالي: {question}\n\n"
                f"جاوب باختصار عملي بالاعتماد على البيانات دي، وبأرقام لما ينفع.")
    else:
        user = (f"دي بيانات شركتي الحيّة:\n{brief}\n\n"
                f"اعملي تحليل منظّم بالعناوين دي بالظبط:\n"
                f"1) قراية الوضع الحالي (٢-٣ أسطر).\n"
                f"2) أهم ٣ ملاحظات/مخاطر بأرقامها.\n"
                f"3) توصيات فورية (bullet points عملية).\n"
                f"4) خطة الأسبوع الجاي (خطوات مرتّبة بالأولوية).")

    messages = [{'role': 'system', 'content': _SYSTEM_PROMPT}]
    for h in (history or [])[-6:]:
        role = 'assistant' if h.get('role') == 'assistant' else 'user'
        content = str(h.get('content') or '').strip()
        if content:
            messages.append({'role': role, 'content': content[:1500]})
    messages.append({'role': 'user', 'content': user})

    try:
        return call_llm_layer(messages, json_mode=False, max_retries=2)
    except Exception as exc:  # noqa: BLE001 — الـ AI لا يوقف الصفحة
        logger.warning("[ADVISOR] ai_analysis failed: %s", exc)
        return None
