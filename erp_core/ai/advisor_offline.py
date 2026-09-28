"""
🧮 Offline business advisor — real answers from the company's own data when
the AI provider isn't available (no key configured, outage, quota).

The LLM advisor already answers through data "tools" (cash, dead stock, …).
When it can't run, the chat used to reply with a dead end ("the advisor isn't
enabled — contact the admin"). Most questions a shop owner types are about a
handful of numbers, so this module recognises those intents with plain Arabic
keyword matching and answers from the same tools / queries:

    cash · receivables · sales & profit · dead stock · low stock ·
    "do I have <part>?" · "what if I sell X% of the slow stock"

Everything here is read-only and scoped to the current tenant schema.
"""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from html import escape

from django.db.models import F, Q, Sum
from django.utils import timezone

_AR_DIACRITICS = re.compile(r'[ً-ْـ]')


def _norm(text: str) -> str:
    """Loose Arabic normalisation: alef/yaa/taa-marbuta variants, no tashkeel."""
    t = _AR_DIACRITICS.sub('', text or '').lower()
    return (t.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
             .replace('ى', 'ي').replace('ة', 'ه'))


def _sym() -> str:
    try:
        from erp_core.localization import current_tenant_symbol
        return current_tenant_symbol()
    except Exception:
        return 'ج.م'


def _fmt(value) -> str:
    return f'{Decimal(str(value or 0)):,.2f} {_sym()}'


def _has(q: str, *words: str) -> bool:
    return any(_norm(w) in q for w in words)


# ── intents ──────────────────────────────────────────────────────────
def _cash(_q):
    from .advisor_tools import calculate_cash_flow_projections
    r = calculate_cash_flow_projections()
    if not r.get('success'):
        return None
    lines = ['💰 الكاش دلوقتي:']
    for t in r['cash_by_treasury']:
        lines.append(f"• {escape(t['treasury'])}: {_fmt(t['balance'])}")
    lines.append(f"الإجمالي: {_fmt(r['current_cash'])}")
    if r['total_receivables']:
        lines.append(f"ولو حصّلت الآجل ({r['outstanding_invoices_count']} فاتورة بـ {_fmt(r['total_receivables'])}) "
                     f"هتوصل لـ {_fmt(r['projected_cash_if_all_collected'])}.")
    return '\n'.join(lines)


def _receivables(_q):
    from .advisor_tools import calculate_cash_flow_projections
    r = calculate_cash_flow_projections()
    if not r.get('success'):
        return None
    if not r['total_receivables']:
        return '✅ مفيش فلوس آجل على العملاء — كل الفواتير متحصّلة.'
    lines = [f"🧾 الآجل على العملاء: {_fmt(r['total_receivables'])} في {r['outstanding_invoices_count']} فاتورة. أعلاهم:"]
    for inv in r['top_outstanding'][:5]:
        late = ' ⏰ متأخرة' if inv.get('is_overdue') else ''
        lines.append(f"• فاتورة #{inv['invoice_id']} — {escape(inv['customer'])}: {_fmt(inv['due'])}{late}")
    lines.append('تقدر تحصّل من صفحة الفواتير ← «تحصيل / خصم».')
    return '\n'.join(lines)


def _sales(q):
    from inventory.models import SaleInvoice
    now = timezone.localtime()
    if _has(q, 'النهارده', 'اليوم', 'انهارده'):
        start, label = now.replace(hour=0, minute=0, second=0, microsecond=0), 'النهارده'
    elif _has(q, 'الاسبوع', 'اسبوع'):
        start, label = now - timedelta(days=7), 'آخر 7 أيام'
    elif _has(q, 'السنه', 'سنه'):
        start, label = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0), 'السنة دي'
    else:
        start, label = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), 'الشهر ده'
    base = SaleInvoice.objects.filter(status='posted', date_created__gte=start, maintenance_contract__isnull=True)
    sales = base.filter(is_return=False).aggregate(t=Sum('total_amount'), p=Sum('net_profit'))
    rets = base.filter(is_return=True).aggregate(t=Sum('total_amount'), p=Sum('net_profit'))
    count = base.filter(is_return=False).count()
    net_sales = (sales['t'] or 0) - (rets['t'] or 0)
    profit = (sales['p'] or 0) - (rets['p'] or 0)
    txt = (f'📈 مبيعات {label}: {_fmt(net_sales)} من {count} فاتورة'
           + (f" (بعد مرتجعات {_fmt(rets['t'])})" if rets['t'] else '')
           + f'\nمجمل الربح: {_fmt(profit)}')
    return txt


def _dead_stock(_q):
    from .advisor_tools import get_dead_stock_report
    r = get_dead_stock_report(limit=5)
    if not r.get('success'):
        return None
    if not r['total_dead_skus']:
        return '✅ مفيش أصناف راكدة — كل اللي في المخزن اتباع منه في آخر 90 يوم.'
    lines = [f"🐌 عندك {r['total_dead_skus']} صنف راكد (ماتباعش من 90 يوم) حابسين {_fmt(r['total_capital_locked'])}. أكترهم:"]
    for it in r['items']:
        lines.append(f"• {escape(it['name'])} ({escape(it['part_number'])}) — {it['qty']} قطعة، رأس مال {_fmt(it['locked_capital'])}")
    lines.append('💡 جرّب تسأل: «لو بعت 30% من الراكد هكسب كام؟»')
    return '\n'.join(lines)


def _simulate(q):
    from .advisor_tools import simulate_inventory_sale
    m = re.search(r'(\d{1,3})\s*[%٪]|(\d{1,3})\s*(?:في الميه|بالميه)', q)
    pct = float(next(g for g in m.groups() if g)) if m else 20.0
    r = simulate_inventory_sale(pct)
    if not r.get('success'):
        return None
    if not r['total_units_to_sell']:
        return 'مفيش مخزون راكد كفاية نعمل عليه محاكاة دلوقتي.'
    return (f"🧪 لو بعت {r['percentage_simulated']:.0f}% من الراكد ({r['total_units_to_sell']} قطعة):\n"
            f"• إيراد متوقع: {_fmt(r['expected_revenue'])}\n"
            f"• ربح صافي متوقع: {_fmt(r['expected_net_profit'])} (هامش {r['profit_margin_percent']}%)")


def _low_stock(_q):
    from inventory.models import Product
    rows = list(
        Product.objects.filter(is_active=True)
        .annotate(qty=Sum('inventory__quantity'))
        .filter(Q(qty__lte=F('min_stock_level')) | Q(qty__isnull=True))
        .order_by('qty')[:10]
        .values('name', 'part_number', 'qty', 'min_stock_level')
    )
    if not rows:
        return '✅ كل الأصناف فوق الحد الأدنى.'
    lines = ['📦 أصناف محتاجة طلب (تحت الحد الأدنى):']
    for r in rows:
        lines.append(f"• {escape(r['name'])} ({escape(r['part_number'])}) — المتاح {r['qty'] or 0} / الحد {r['min_stock_level']}")
    lines.append('اعمل فاتورة شراء من «المشتريات ← فاتورة شراء جديدة».')
    return '\n'.join(lines)


_STOP = {_norm(w) for w in (
    'عندي', 'عندك', 'عندنا', 'عندكم', 'هل', 'فيه', 'في', 'موجود', 'متوفر', 'متاح', 'كام', 'قطعه', 'قطع',
    'من', 'ف', 'المخزن', 'المخزون', 'لسه', 'ولا', 'لا', 'بكام', 'سعر', 'ايه', 'اي', 'يا', 'ال', 'على',
    'و', 'ده', 'دي', 'the', 'مخزون', 'رصيد', 'الصنف', 'صنف',
)}


def _stock_lookup(raw_query):
    from inventory.models import Product
    words = [w for w in re.findall(r'[\w\-]+', raw_query) if _norm(w) not in _STOP and len(w) > 1]
    if not words:
        return None
    qs = Product.objects.filter(is_active=True)
    cond = Q()
    for w in words[:4]:
        cond &= (Q(name__icontains=w) | Q(part_number__icontains=w) | Q(brand__icontains=w)
                 | Q(car_model__icontains=w))
    hits = list(qs.filter(cond).annotate(qty=Sum('inventory__quantity'))[:6])
    if not hits and len(words) > 1:   # any word, for looser phrasing
        cond = Q()
        for w in words[:4]:
            cond |= Q(name__icontains=w) | Q(part_number__icontains=w)
        hits = list(qs.filter(cond).annotate(qty=Sum('inventory__quantity'))[:6])
    if not hits:
        return f"🔍 مالقيتش صنف بالاسم «{escape(' '.join(words))}» في المخزن."
    lines = ['🔍 اللي لقيته في المخزن:']
    for p in hits:
        qty = p.qty or 0
        state = '✅' if qty > p.min_stock_level else ('⚠️' if qty > 0 else '❌ نافد')
        lines.append(f"• {escape(p.name)} ({escape(p.part_number)}) — {qty} قطعة {state} — السعر {_fmt(p.retail_price)}")
    return '\n'.join(lines)


_MENU = ('🌙 المستشار الذكي مش متاح دلوقتي، بس أقدر أرد من أرقامك مباشرة.\nجرّب تسأل:\n'
         '• «الكاش كام؟»\n• «مين عليه فلوس؟»\n• «مبيعات النهارده / الشهر»\n'
         '• «إيه الراكد؟»\n• «إيه اللي ناقص؟»\n• «عندي فلتر زيت؟»\n'
         '• «لو بعت 30% من الراكد؟»')


def offline_answer(query: str, sector: str = 'automotive') -> dict:
    """Answer ``query`` from the tenant's data without an LLM."""
    if sector != 'automotive':
        return {'success': False, 'error': 'ai_disabled',
                'answer': '🌙 المستشار الذكي لسه مش مفعّل على السيرفر — تواصل مع الإدارة.'}
    q = _norm(query)
    routes = [
        (('لو بعت', 'لو بيعت', 'محاكاه', 'هكسب كام', 'اكسب كام'), _simulate),
        (('راكد', 'واقف', 'مش بيتباع', 'ركود', 'مابيتباعش', 'نايم'), _dead_stock),
        (('ناقص', 'خلص', 'نافد', 'تحت الحد', 'محتاج اطلب', 'اطلب ايه'), _low_stock),
        (('اجل', 'مديونيه', 'عليه فلوس', 'عليهم', 'مستحقات', 'ديون', 'متاخرات'), _receivables),
        (('مبيعات', 'بعت كام', 'ربح', 'ارباح', 'مكسب', 'دخل'), _sales),
        (('كاش', 'خزنه', 'خزينه', 'خزن', 'فلوس', 'سيوله', 'رصيد الخزن'), _cash),
    ]
    for words, handler in routes:
        if _has(q, *words):
            try:
                answer = handler(q)
            except Exception:
                answer = None
            if answer:
                return {'success': True, 'answer': answer, 'mode': 'offline', 'tool_calls': []}
            break
    greetings = ('ازيك', 'اهلا', 'السلام', 'مرحبا', 'هاي', 'صباح', 'مساء', 'شكرا', 'تمام', 'hi', 'hello')
    if _has(q, *greetings) and len(q.split()) <= 3:
        return {'success': True, 'answer': 'أهلاً بيك 👋\n' + _MENU.split('\n', 1)[1],
                'mode': 'offline', 'tool_calls': []}
    try:
        answer = _stock_lookup(query)
    except Exception:
        answer = None
    if answer and not answer.startswith('🔍 مالقيتش'):
        return {'success': True, 'answer': answer, 'mode': 'offline', 'tool_calls': []}
    return {'success': True, 'answer': (answer + '\n\n' if answer else '') + _MENU,
            'mode': 'offline', 'tool_calls': []}
