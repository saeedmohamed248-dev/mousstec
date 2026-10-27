"""🧠 المستشار الذكي + توقّع الطلب وإعادة الطلب.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.shortcuts import render
from django.views.decorators.http import require_POST
from inventory.models import Inventory, Product, SaleInvoiceItem
from inventory.views import _json_response_safe, tenant_required
from inventory.views.utils import module_required, role_required
from inventory.report_export import export_report
from ._common import _report_branch
from .reports import _sales_invoices


# =====================================================================
# 🧠 المستشار الذكي (AI Business Advisor) — تحليل + توصيات + خطة
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def business_advisor(request):
    """صفحة المستشار الذكي: مؤشرات حيّة + ملاحظات بقواعد ذكية + تحليل AI + شات."""
    from inventory.services import business_advisor as _adv

    branch, branch_options, can_pick_branch = _report_branch(request)
    try:
        days = int(request.GET.get('days') or 30)
    except (TypeError, ValueError):
        days = 30
    if days not in (7, 30, 90, 365):
        days = 30

    snap = _adv.build_snapshot(branch=branch, days=days)
    insights = _adv.rule_based_insights(snap)
    ai_text = _adv.ai_analysis(snap)  # None لو الـ AI مطفي

    # 📈 اتجاه المبيعات — آخر 12 شهر (للرسم البياني)
    from django.utils import timezone as _tz2
    from datetime import timedelta as _td
    trend_start = (_tz2.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0) - _td(days=365))
    trend_qs = (   # صافي بعد المرتجعات (كان بيعرض المبيعات من غير ما يطرح المرتجع)
        _sales_invoices(request, branch, trend_start, None)
        .annotate(m=TruncMonth('date_created')).values('m')
        .annotate(total=(Coalesce(Sum('total_amount', filter=Q(is_return=False)), Decimal('0'))
                         - Coalesce(Sum('total_amount', filter=Q(is_return=True)), Decimal('0'))),
                  profit=(Coalesce(Sum('net_profit', filter=Q(is_return=False)), Decimal('0'))
                          - Coalesce(Sum('net_profit', filter=Q(is_return=True)), Decimal('0'))))
        .order_by('m')
    )
    _ar_months = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
                  'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']
    trend = [{
        'label': (f"{_ar_months[r['m'].month - 1]} {r['m'].year}" if r['m'] else '—'),
        'sales': float(r['total'] or 0), 'profit': float(r['profit'] or 0),
    } for r in trend_qs]

    # 🗓️ آخر ملخّص أسبوعي تلقائي محفوظ (من الكاش)
    saved_brief = None
    try:
        from django.core.cache import cache as _cache
        from django.db import connection as _conn
        saved_brief = _cache.get(f"{_conn.schema_name}:advisor_brief")
    except Exception:  # noqa: BLE001
        saved_brief = None

    from django.conf import settings as _st
    return render(request, 'inventory/business_advisor.html', {
        'branch': branch, 'branch_options': branch_options,
        'can_pick_branch': can_pick_branch, 'days': days,
        'snap': snap, 'insights': insights, 'ai_text': ai_text,
        'trend': trend, 'saved_brief': saved_brief,
        'ai_enabled': bool(getattr(_st, 'ENABLE_AI_PREDICTIONS', False)),
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
@require_POST
def business_advisor_ask(request):
    """شات المستشار: سؤال حر يُجاب عليه من بيانات السيستم الحيّة."""
    import json as _json
    from inventory.services import business_advisor as _adv

    try:
        payload = _json.loads(request.body or b"{}")
    except ValueError:
        return _json_response_safe({"error": "بيانات غير صالحة."}, status=400)

    question = (payload.get('question') or '').strip()
    if not question:
        return _json_response_safe({"error": "اكتب سؤالك."}, status=400)
    history = payload.get('history') or []

    branch, _opts, _pick = _report_branch(request)
    try:
        days = int(payload.get('days') or 30)
    except (TypeError, ValueError):
        days = 30
    if days not in (7, 30, 90, 365):
        days = 30

    snap = _adv.build_snapshot(branch=branch, days=days)
    answer = _adv.ai_analysis(snap, question=question, history=history)
    if not answer:
        return _json_response_safe({
            "answer": None,
            "error": "المساعد الذكي غير مُفعّل حالياً. فعّل ENABLE_AI_PREDICTIONS "
                     "وأضف مفتاح Google AI Studio المجاني (AI_VISION_API_KEY) "
                     "لتشغيل التحليل النصّي والشات مجاناً.",
        }, status=200)
    return _json_response_safe({"answer": answer})


# =====================================================================
# 📦🔮 توقّع الطلب وإعادة الطلب (Demand Forecast / Reorder)
#   لكل صنف: سرعة البيع · تغطية المخزون بالأيام · نقطة وكمية إعادة الطلب.
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def reorder_report(request):
    """تقرير ذكي (رقمي) يقترح إعادة الطلب بناءً على سرعة البيع الفعلية.

    المنهجية:
      سرعة البيع = المباع في النافذة ÷ أيام النافذة.
      تغطية المخزون (أيام) = الرصيد الحالي ÷ سرعة البيع.
      نقطة إعادة الطلب = سرعة البيع × مهلة التوريد + مخزون الأمان.
      الكمية المقترحة = max(0, هدف التغطية − الرصيد) حيث الهدف = سرعة × (مهلة + دورة مراجعة).
    """
    from django.utils import timezone as _tz
    from datetime import timedelta as _td
    import math as _math

    branch, branch_options, can_pick_branch = _report_branch(request)

    # نافذة حساب السرعة
    try:
        window = int(request.GET.get('days') or 30)
    except (TypeError, ValueError):
        window = 30
    if window not in (30, 60, 90, 180):
        window = 30
    # مهلة التوريد ودورة المراجعة (أيام) — قابلة للتعديل من الرابط
    try:
        lead = int(request.GET.get('lead') or 14)
    except (TypeError, ValueError):
        lead = 14
    lead = max(1, min(lead, 180))
    coverage_target = lead + 14  # هدف التغطية = مهلة + أسبوعين مراجعة

    now = _tz.localtime()   # بداية اليوم/الشهر بتوقيت المحل مش UTC
    start = now - _td(days=window)

    # المباع لكل منتج في النافذة (فواتير فعلية، مش مرتجع)
    sales = _sales_invoices(request, branch, start, None, include_returns=False)
    sold_map = {
        r['product_id']: (r['q'] or 0)
        for r in SaleInvoiceItem.objects.filter(invoice__in=sales)
        .values('product_id').annotate(q=Sum('quantity'))
    }

    # رصيد المخزون الحالي لكل منتج (مقيّد بالفرع لو مختار)
    inv_rows = Inventory.objects.all()
    if branch is not None:
        inv_rows = inv_rows.filter(branch=branch)
    stock_map = {}
    for r in inv_rows.values('product_id').annotate(q=Sum('quantity')):
        stock_map[r['product_id']] = r['q'] or 0

    # كل المنتجات اللي ليها رصيد أو مبيعات في النافذة
    pids = set(stock_map) | set(sold_map)
    products = {p.id: p for p in Product.objects.filter(id__in=pids)}

    rows = []
    urgent = dead = healthy = 0
    for pid in pids:
        p = products.get(pid)
        if p is None:
            continue
        sold = sold_map.get(pid, 0)
        stock = stock_map.get(pid, 0)
        velocity = (sold / window) if window else 0  # قطعة/يوم
        if velocity > 0:
            cover_days = stock / velocity
            reorder_point = velocity * lead + (p.min_stock_level or 0)
            target = velocity * coverage_target
            suggested = max(0, int(_math.ceil(target - stock)))
        else:
            cover_days = None  # مفيش مبيعات
            reorder_point = (p.min_stock_level or 0)
            suggested = 0

        if velocity > 0 and cover_days is not None and cover_days <= lead:
            status, urgent = 'urgent', urgent + 1
        elif velocity == 0 and stock > 0:
            status, dead = 'dead', dead + 1
        else:
            status, healthy = 'healthy', healthy + 1

        rows.append({
            'id': pid, 'name': p.name, 'sku': p.part_number, 'brand': p.brand,
            'sold': sold, 'stock': stock,
            'velocity': round(velocity, 2),
            'cover_days': (round(cover_days) if cover_days is not None else None),
            'reorder_point': int(round(reorder_point)),
            'suggested': suggested, 'status': status,
        })

    # ترتيب: العاجل الأول (أقل تغطية)، بعدين الراكد، بعدين الباقي
    _order = {'urgent': 0, 'dead': 2, 'healthy': 1}
    rows.sort(key=lambda r: (_order[r['status']],
                             r['cover_days'] if r['cover_days'] is not None else 10**9))

    _exp = export_report(
        request, f"reorder_{window}d", "توقّع الطلب وإعادة الطلب",
        f"نافذة {window} يوم · مهلة توريد {lead} يوم · {branch.name if branch else 'كل الفروع'}",
        [{
            "columns": ["القطعة", "رقم القطعة", "المباع", "الرصيد", "سرعة/يوم",
                        "تغطية (يوم)", "نقطة الطلب", "الكمية المقترحة"],
            "rows": [[r['name'], r['sku'], r['sold'], r['stock'], r['velocity'],
                      (r['cover_days'] if r['cover_days'] is not None else '∞'),
                      r['reorder_point'], r['suggested']] for r in rows],
        }])
    if _exp:
        return _exp

    return render(request, 'inventory/reorder_report.html', {
        'branch': branch, 'branch_options': branch_options, 'can_pick_branch': can_pick_branch,
        'window': window, 'lead': lead, 'rows': rows,
        'urgent': urgent, 'dead': dead, 'healthy': healthy, 'total': len(rows),
    })
