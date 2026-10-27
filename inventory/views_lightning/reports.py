"""📊 مركز التقارير: المبيعات، العملاء، كشف الحساب، الأصناف، الخدمات.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.db.models import Count, DecimalField, Max, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.shortcuts import redirect, render
from django.urls import reverse
from inventory.models import (
    Customer,
    FinancialTransaction,
    Product,
    SaleInvoice,
    SaleInvoiceItem,
    SaleInvoiceServiceItem,
)
from inventory.views import tenant_required
from inventory.views.utils import module_required, role_required
from inventory.report_export import export_report
from ._common import _report_branch, _report_period


# =====================================================================
# 📊 مركز التقارير — تقارير مبيعات وبنود مفصّلة (عملاء · منتجات · خدمات)
#   بيبني على نفس بنية التصدير الموحّدة (export_report) وفلتر الفرع/الفترة.
# =====================================================================
def _sales_invoices(request, branch, start, end=None, include_returns=True):
    """QuerySet موحّد لفواتير البيع الفعلية (باستثناء عروض الأسعار)."""
    qs = SaleInvoice.objects.exclude(status='quotation')
    if not include_returns:
        qs = qs.filter(is_return=False)
    if branch is not None:
        qs = qs.filter(branch=branch)
    if start is not None:
        qs = qs.filter(date_created__gte=start)
    if end is not None:
        qs = qs.filter(date_created__lt=end)
    return qs


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def reports_hub(request):
    """مركز التقارير — صفحة واحدة توصّل لكل التقارير المفصّلة."""
    branch, branch_options, can_pick_branch = _report_branch(request)
    return render(request, 'inventory/reports_hub.html', {
        'branch': branch, 'branch_options': branch_options,
        'can_pick_branch': can_pick_branch,
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def sales_report(request):
    """تقرير المبيعات المفصّل: مؤشرات + قائمة فواتير الفترة + تحليل بالنوع/القناة."""
    branch, branch_options, can_pick_branch = _report_branch(request)
    start, end, label, period = _report_period(request)

    inv = _sales_invoices(request, branch, start, end)

    # 🔎 بحث شامل: رقم الفاتورة · اسم/هاتف العميل · اسم/رقم قطعة الغيار (بند) ·
    #    اسم الخدمة — مع تطبيع عربي للأسماء عشان يلاقي مهما اختلفت صيغة الحروف.
    q = (request.GET.get('q') or '').strip()
    if q:
        cond = (
            Q(customer__name__icontains=q)
            | Q(customer__phone__icontains=q)
            | Q(items__product__name__icontains=q)
            | Q(items__product__part_number__icontains=q)
            | Q(items__product__barcode__icontains=q)
            | Q(service_items__service__name__icontains=q)
        )
        if q.isdigit():
            cond = cond | Q(id=int(q))
        inv = inv.filter(cond).distinct()

    agg = inv.aggregate(
        sales_g=Sum('total_amount', filter=Q(is_return=False)),
        sales_r=Sum('total_amount', filter=Q(is_return=True)),
        cost_g=Sum('total_cost', filter=Q(is_return=False)),
        cost_r=Sum('total_cost', filter=Q(is_return=True)),
        paid_g=Sum('paid_amount', filter=Q(is_return=False)),
        refunded=Sum('paid_amount', filter=Q(is_return=True)),
        cnt=Count('id', filter=Q(is_return=False)),
        ret_cnt=Count('id', filter=Q(is_return=True)),
    )
    gross = agg['sales_g'] or Decimal('0')
    returns_amt = agg['sales_r'] or Decimal('0')
    net_sales = gross - returns_amt
    cogs = (agg['cost_g'] or Decimal('0')) - (agg['cost_r'] or Decimal('0'))
    profit = net_sales - cogs
    # 🐛 [FIX]: «المدفوع» في المرتجع = فلوس اترجعت للعميل — كانت بتتجمع مع
    #    المحصّل، فالمدفوع يطلع أكبر والآجل بالسالب (مثلاً −1700) لما يبقى فيه مرتجعات.
    paid = (agg['paid_g'] or Decimal('0')) - (agg['refunded'] or Decimal('0'))
    due = net_sales - paid
    margin = (profit / net_sales * Decimal('100')) if net_sales else Decimal('0')
    avg_ticket = (net_sales / agg['cnt']) if agg['cnt'] else Decimal('0')

    # تحليل حسب نوع الفاتورة (بيع / صيانة)
    by_type = list(
        inv.filter(is_return=False)
        .values('invoice_type')
        .annotate(n=Count('id'), total=Sum('total_amount'), prof=Sum('net_profit'))
        .order_by('-total')
    )
    type_label = dict(SaleInvoice.INVOICE_TYPES)
    for r in by_type:
        r['label'] = type_label.get(r['invoice_type'], r['invoice_type'])

    # 📈 اتجاه المبيعات — آخر 12 شهر (مستقل عن فلتر الفترة، بيتقيّد بالفرع فقط)
    from django.utils import timezone as _tz2
    from datetime import timedelta as _td
    trend_start = (_tz2.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0) - _td(days=365))
    trend_qs = (
        _sales_invoices(request, branch, trend_start, None, include_returns=False)
        .annotate(m=TruncMonth('date_created'))
        .values('m')
        .annotate(total=Sum('total_amount'))
        .order_by('m')
    )
    _ar_months = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
                  'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']
    trend = [{
        'label': (f"{_ar_months[r['m'].month - 1]} {r['m'].year}" if r['m'] else '—'),
        'value': float(r['total'] or 0),
    } for r in trend_qs]

    # قائمة الفواتير (أحدث 300) — مع الأصناف/الخدمات (البنود) لعرضها والبحث فيها
    rows = list(
        inv.select_related('customer', 'branch')
        .prefetch_related('items__product', 'service_items__service')
        .order_by('-date_created')[:300]
    )
    # بنبني نص مختصر بأسماء بنود كل فاتورة (قطع غيار + خدمات)
    for inv_ in rows:
        names = [it.product.name for it in inv_.items.all() if it.product_id]
        names += [si.service.name for si in inv_.service_items.all() if si.service_id]
        inv_.items_summary = names

    _exp = export_report(
        request, f"sales_{period}", "تقرير المبيعات",
        f"{label} · {branch.name if branch else 'كل الفروع'}",
        [{
            "columns": ["#", "التاريخ", "العميل", "البنود / الأصناف", "الفرع", "النوع", "الإجمالي", "المدفوع", "المتبقي"],
            "rows": [[
                inv_.id, inv_.date_created.strftime('%Y-%m-%d %H:%M'),
                inv_.customer.name if inv_.customer else '—',
                '، '.join(inv_.items_summary) or '—',
                inv_.branch.name if inv_.branch else '—',
                ('مرتجع' if inv_.is_return else type_label.get(inv_.invoice_type, inv_.invoice_type)),
                inv_.total_amount, inv_.paid_amount, inv_.due_amount,
            ] for inv_ in rows],
            "total": ["", "", "", "", "", "الإجمالي", net_sales, paid, due],
        }])
    if _exp:
        return _exp

    return render(request, 'inventory/sales_report.html', {
        'branch': branch, 'branch_options': branch_options, 'can_pick_branch': can_pick_branch,
        'period': period, 'label': label,
        'gross': gross, 'returns_amt': returns_amt, 'net_sales': net_sales,
        'cogs': cogs, 'profit': profit, 'margin': margin, 'paid': paid, 'due': due,
        'cnt': agg['cnt'] or 0, 'ret_cnt': agg['ret_cnt'] or 0, 'avg_ticket': avg_ticket,
        'by_type': by_type, 'rows': rows, 'type_label': type_label,
        'trend': trend, 'q': q,
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def customers_report(request):
    """تقرير العملاء: لكل عميل — عدد الفواتير · إجمالي المبيعات · المدفوع · المتبقي · آخر شراء."""
    branch, branch_options, can_pick_branch = _report_branch(request)
    start, end, label, period = _report_period(request, default='all')

    # 🐛 [FIX]: المرتجعات ماكانتش بتتخصم — عميل رجّع فاتورته كلها كان بيفضل
    #    ظاهر بإجماليها و«متبقّي» عليه، مع إن رصيده الفعلي اتخصم منه.
    inv = _sales_invoices(request, branch, start, end)
    agg = (
        inv.values('customer_id', 'customer__name', 'customer__phone',
                   'customer__balance', 'customer__is_b2b_company')
        .annotate(
            n=Count('id', filter=Q(is_return=False)),
            sales_g=Sum('total_amount', filter=Q(is_return=False)),
            sales_r=Sum('total_amount', filter=Q(is_return=True)),
            paid_g=Sum('paid_amount', filter=Q(is_return=False)),
            refunded=Sum('paid_amount', filter=Q(is_return=True)),
            profit_g=Sum('net_profit', filter=Q(is_return=False)),
            profit_r=Sum('net_profit', filter=Q(is_return=True)),
            last=Max('date_created'),
        )
        .order_by()   # تجميع لكل عميل — من غير ترتيب يدخل في الـ GROUP BY
    )
    rows = []
    tot_total = tot_paid = tot_due = tot_profit = Decimal('0')
    for r in agg:
        z = Decimal('0')
        total = (r['sales_g'] or z) - (r['sales_r'] or z)
        paid = (r['paid_g'] or z) - (r['refunded'] or z)
        r['profit'] = (r['profit_g'] or z) - (r['profit_r'] or z)
        due = total - paid
        rows.append({
            'id': r['customer_id'], 'name': r['customer__name'] or '—',
            'phone': r['customer__phone'] or '—',
            'is_b2b': r['customer__is_b2b_company'],
            'balance': r['customer__balance'] or Decimal('0'),
            'n': r['n'], 'total': total, 'paid': paid, 'due': due,
            'profit': r['profit'] or Decimal('0'), 'last': r['last'],
        })
        tot_total += total
        tot_paid += paid
        tot_due += due
        tot_profit += (r['profit'] or Decimal('0'))
    rows.sort(key=lambda x: x['total'], reverse=True)

    _exp = export_report(
        request, f"customers_{period}", "تقرير العملاء",
        f"{label} · {branch.name if branch else 'كل الفروع'}",
        [{
            "columns": ["العميل", "الهاتف", "عدد الفواتير", "الإجمالي", "المدفوع", "المتبقي", "الربح"],
            "rows": [[r['name'], r['phone'], r['n'], r['total'], r['paid'], r['due'], r['profit']] for r in rows],
            "total": ["الإجمالي", "", "", tot_total, tot_paid, tot_due, tot_profit],
        }])
    if _exp:
        return _exp

    chart = [{'label': r['name'], 'value': float(r['total'])} for r in rows[:10]]
    return render(request, 'inventory/customers_report.html', {
        'branch': branch, 'branch_options': branch_options, 'can_pick_branch': can_pick_branch,
        'period': period, 'label': label, 'rows': rows,
        'tot_total': tot_total, 'tot_paid': tot_paid, 'tot_due': tot_due, 'tot_profit': tot_profit,
        'customers_count': len(rows), 'chart': chart,
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def customer_statement(request, pk):
    """كشف حساب عميل واحد: بياناته + مؤشراته + فواتيره + أكثر القطع شراءً."""
    customer = Customer.objects.filter(pk=pk).first()
    if customer is None:
        return redirect(reverse('inventory:customers_report') + '?err=notfound')

    branch, branch_options, can_pick_branch = _report_branch(request)
    start, end, label, period = _report_period(request, default='all')

    inv = _sales_invoices(request, branch, start, end).filter(customer=customer)
    agg = inv.aggregate(
        sales_g=Sum('total_amount', filter=Q(is_return=False)),
        sales_r=Sum('total_amount', filter=Q(is_return=True)),
        paid_g=Sum('paid_amount', filter=Q(is_return=False)),
        refunded=Sum('paid_amount', filter=Q(is_return=True)),
        profit_g=Sum('net_profit', filter=Q(is_return=False)),
        profit_r=Sum('net_profit', filter=Q(is_return=True)),
        cnt=Count('id', filter=Q(is_return=False)),
    )
    # 🐛 [FIX]: ربح العميل كان من فواتير البيع بس — المرتجع مابيقلّلوش
    agg['profit'] = (agg['profit_g'] or Decimal('0')) - (agg['profit_r'] or Decimal('0'))
    gross = agg['sales_g'] or Decimal('0')
    returns_amt = agg['sales_r'] or Decimal('0')
    net_sales = gross - returns_amt
    # 🐛 [FIX]: «المدفوع» كان بيجمع مدفوع فواتير المرتجع كمان — وده فلوس
    #    *رجعت* للعميل. بيع 1000 مدفوع واترجع بالكامل كان بيطلع المدفوع 2000
    #    والمتبقي −2000 (كأن المحل مديون للعميل). وكمان التحصيل على الحساب
    #    (من شاشة العملاء، مش على فاتورة) ماكانش بيظهر خالص.
    on_account = FinancialTransaction.objects.filter(
        customer=customer, sale_invoice__isnull=True)
    if branch is not None:
        on_account = on_account.filter(treasury__branch=branch)
    if start is not None:
        on_account = on_account.filter(date__gte=start)
    if end is not None:
        on_account = on_account.filter(date__lt=end)
    acc = on_account.aggregate(i=Sum('amount', filter=Q(transaction_type='in')),
                               o=Sum('amount', filter=Q(transaction_type='out')))
    on_account_paid = (acc['i'] or Decimal('0')) - (acc['o'] or Decimal('0'))
    paid = ((agg['paid_g'] or Decimal('0')) - (agg['refunded'] or Decimal('0'))
            + on_account_paid)
    due = net_sales - paid

    invoices = list(inv.select_related('branch', 'vehicle').order_by('-date_created')[:200])

    # أكثر القطع اللي اشتراها العميل
    from inventory.services.reporting_service import ReportingService
    _net = ReportingService.item_net_annotations()
    top_parts = list(
        SaleInvoiceItem.objects.filter(invoice__in=inv)        # المرتجع بيتطرح
        .values('product__name', 'product__part_number')
        .annotate(qty=_net['qty'], revenue=_net['revenue'])
        .filter(qty__gt=0)
        .order_by('-revenue')[:15]
    )

    _exp = export_report(
        request, f"customer_{customer.id}_{period}",
        f"كشف حساب: {customer.name}",
        f"{label} · {branch.name if branch else 'كل الفروع'}",
        [{
            "name": "الفواتير",
            "columns": ["#", "التاريخ", "الفرع", "النوع", "الإجمالي", "المدفوع", "المتبقي"],
            "rows": [[
                i.id, i.date_created.strftime('%Y-%m-%d'),
                i.branch.name if i.branch else '—',
                ('مرتجع' if i.is_return else i.get_invoice_type_display()),
                i.total_amount, i.paid_amount, i.due_amount,
            ] for i in invoices],
            "total": ["", "", "", "الإجمالي", net_sales, paid, due],
        }, {
            "name": "أكثر القطع شراءً",
            "columns": ["القطعة", "رقم القطعة", "الكمية", "القيمة"],
            "rows": [[p['product__name'], p['product__part_number'], p['qty'], p['revenue']] for p in top_parts],
        }])
    if _exp:
        return _exp

    return render(request, 'inventory/customer_report.html', {
        'customer': customer, 'branch': branch,
        'branch_options': branch_options, 'can_pick_branch': can_pick_branch,
        'period': period, 'label': label,
        'gross': gross, 'returns_amt': returns_amt, 'net_sales': net_sales,
        'paid': paid, 'due': due, 'profit': agg['profit'] or Decimal('0'),
        'cnt': agg['cnt'] or 0, 'invoices': invoices, 'top_parts': top_parts,
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def products_report(request):
    """تقرير قطع الغيار/المنتجات: لكل قطعة — الكمية المباعة · الإيراد · التكلفة · الربح · الهامش."""
    branch, branch_options, can_pick_branch = _report_branch(request)
    start, end, label, period = _report_period(request)

    # 🐛 [FIX]: المرتجعات كانت مستبعدة (الإيراد والربح ماكانوش بيقلّوا بيها)،
    #    والسطور غير المفوترة (ضمان/فحص) كانت بتتحسب إيراد.
    from inventory.services.reporting_service import ReportingService
    inv = _sales_invoices(request, branch, start, end)
    _net = ReportingService.item_net_annotations()
    agg = (
        SaleInvoiceItem.objects.filter(invoice__in=inv)
        .values('product_id', 'product__name', 'product__part_number',
                'product__brand', 'product__part_category')
        .annotate(qty=_net['qty'], revenue=_net['revenue'], cost=_net['cost'])
        .order_by('-revenue')
    )
    rows = []
    tot_qty = 0
    tot_rev = tot_cost = Decimal('0')
    cat_label = dict(getattr(Product, 'PART_CATEGORY_CHOICES', []))
    for r in agg:
        revenue = r['revenue'] or Decimal('0')
        cost = r['cost'] or Decimal('0')
        profit = revenue - cost
        margin = (profit / revenue * Decimal('100')) if revenue else Decimal('0')
        rows.append({
            'id': r['product_id'], 'name': r['product__name'] or '—',
            'sku': r['product__part_number'] or '—',
            'brand': r['product__brand'] or '', 'qty': r['qty'] or 0,
            'category': cat_label.get(r['product__part_category'] or '', ''),
            'revenue': revenue, 'cost': cost, 'profit': profit, 'margin': margin,
        })
        tot_qty += r['qty'] or 0
        tot_rev += revenue
        tot_cost += cost
    tot_profit = tot_rev - tot_cost

    _exp = export_report(
        request, f"products_{period}", "تقرير قطع الغيار / المنتجات",
        f"{label} · {branch.name if branch else 'كل الفروع'}",
        [{
            "columns": ["القطعة", "رقم القطعة", "الماركة", "الكمية", "الإيراد", "التكلفة", "الربح"],
            "rows": [[r['name'], r['sku'], r['brand'], r['qty'], r['revenue'], r['cost'], r['profit']] for r in rows],
            "total": ["الإجمالي", "", "", tot_qty, tot_rev, tot_cost, tot_profit],
        }])
    if _exp:
        return _exp

    chart = [{'label': r['name'], 'value': float(r['revenue'])} for r in rows[:10]]
    return render(request, 'inventory/products_report.html', {
        'branch': branch, 'branch_options': branch_options, 'can_pick_branch': can_pick_branch,
        'period': period, 'label': label, 'rows': rows,
        'tot_qty': tot_qty, 'tot_rev': tot_rev, 'tot_cost': tot_cost, 'tot_profit': tot_profit,
        'products_count': len(rows), 'chart': chart,
    })


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager', 'accountant')
@module_required('reports')
def services_report(request):
    """تقرير البنود/الخدمات: لكل خدمة — عدد المرات · إجمالي الإيراد · متوسط السعر."""
    branch, branch_options, can_pick_branch = _report_branch(request)
    start, end, label, period = _report_period(request)

    inv = _sales_invoices(request, branch, start, end, include_returns=False)
    price_expr = Coalesce('price', Value(Decimal('0')),
                          output_field=DecimalField(max_digits=12, decimal_places=2))
    agg = (
        SaleInvoiceServiceItem.objects.filter(invoice__in=inv)
        .values('service_id', 'service__name')
        .annotate(n=Count('id'), revenue=Sum(price_expr))
        .order_by('-revenue')
    )
    rows = []
    tot_n = 0
    tot_rev = Decimal('0')
    for r in agg:
        revenue = r['revenue'] or Decimal('0')
        n = r['n'] or 0
        rows.append({
            'name': r['service__name'] or '—', 'n': n, 'revenue': revenue,
            'avg': (revenue / n) if n else Decimal('0'),
        })
        tot_n += n
        tot_rev += revenue

    _exp = export_report(
        request, f"services_{period}", "تقرير الخدمات / البنود",
        f"{label} · {branch.name if branch else 'كل الفروع'}",
        [{
            "columns": ["الخدمة", "عدد المرات", "الإيراد", "متوسط السعر"],
            "rows": [[r['name'], r['n'], r['revenue'], r['avg']] for r in rows],
            "total": ["الإجمالي", tot_n, tot_rev, ""],
        }])
    if _exp:
        return _exp

    chart = [{'label': r['name'], 'value': float(r['revenue'])} for r in rows[:10]]
    return render(request, 'inventory/services_report.html', {
        'branch': branch, 'branch_options': branch_options, 'can_pick_branch': can_pick_branch,
        'period': period, 'label': label, 'rows': rows,
        'tot_n': tot_n, 'tot_rev': tot_rev, 'services_count': len(rows), 'chart': chart,
    })
