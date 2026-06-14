"""
🤖 Printing AI Studio Views
==============================
AI-powered design generation and smart watermark for printing tenants.
Gated by TenantSubscription + AILimitTracker.
"""
import logging
import base64
import json
import re
from io import BytesIO
from decimal import Decimal
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db import connection
from django.db.models import Sum, Count, Avg, Q, F
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')



# Customer statements, profit reports, quotations workflow.

from .utils import *  # noqa: F401, F403




# =====================================================================
# 📒 8. كشف حساب العميل (Customer Statement)
# =====================================================================

@login_required
def customer_statement(request, customer_id):
    """كشف حساب شامل للعميل: كل الفواتير + المدفوعات + الرصيد الجاري.

    ✓ كل PrintOrder (الفواتير الصادرة للعميل) → debit
    ✓ كل PrintTransaction.transaction_type='in' المربوط بطلباته → credit
    ✓ running balance بعد كل حركة + إجماليات

    يدعم: ?from=YYYY-MM-DD&to=YYYY-MM-DD للفلترة
          ?print=1 للنسخة المخصصة للطباعة
    """
    from printing.models import PrintCustomer, PrintOrder, PrintTransaction

    customer = get_object_or_404(PrintCustomer, pk=customer_id)

    # فلترة بالتاريخ
    date_from = request.GET.get('from', '').strip()
    date_to = request.GET.get('to', '').strip()

    orders_qs = PrintOrder.objects.filter(customer=customer)
    payments_qs = PrintTransaction.objects.filter(
        order__customer=customer, transaction_type='in',
    )

    if date_from:
        orders_qs = orders_qs.filter(date_created__date__gte=date_from)
        payments_qs = payments_qs.filter(date__date__gte=date_from)
    if date_to:
        orders_qs = orders_qs.filter(date_created__date__lte=date_to)
        payments_qs = payments_qs.filter(date__date__lte=date_to)

    # دمج الفواتير + المدفوعات في timeline واحد مرتب بالتاريخ
    events = []
    for o in orders_qs.select_related('branch'):
        events.append({
            'date': o.date_created,
            'type': 'invoice',
            'ref': o.order_number,
            'description': f'فاتورة #{o.order_number}' + (f' — {o.notes[:60]}' if o.notes else ''),
            'debit': o.net_total,   # عليه (مدين)
            'credit': Decimal('0'),
            'status': o.get_status_display(),
            'obj_id': o.pk,
        })
    for p in payments_qs.select_related('treasury', 'order'):
        events.append({
            'date': p.date,
            'type': 'payment',
            'ref': f'#{p.pk}',
            'description': p.description or f'دفعة على فاتورة #{p.order.order_number if p.order else ""}',
            'debit': Decimal('0'),
            'credit': p.amount,    # دفع (دائن)
            'status': p.treasury.name if p.treasury else '',
            'obj_id': p.pk,
        })

    events.sort(key=lambda e: e['date'])

    running = Decimal('0')
    for ev in events:
        running += ev['debit'] - ev['credit']
        ev['balance'] = running

    # إجماليات
    total_invoiced = sum((e['debit'] for e in events), Decimal('0'))
    total_paid = sum((e['credit'] for e in events), Decimal('0'))
    final_balance = total_invoiced - total_paid

    # كل الطلبات للملخص العلوي (بدون فلترة)
    all_orders = PrintOrder.objects.filter(customer=customer)
    summary = {
        'total_orders': all_orders.count(),
        'open_orders': all_orders.exclude(status__in=['delivered', 'cancelled']).count(),
        'delivered_orders': all_orders.filter(status='delivered').count(),
    }

    return render(request, 'printing/customer_statement.html', {
        'customer': customer,
        'events': events,
        'total_invoiced': total_invoiced,
        'total_paid': total_paid,
        'final_balance': final_balance,
        'summary': summary,
        'date_from': date_from,
        'date_to': date_to,
        'print_mode': request.GET.get('print') == '1',
    })


# =====================================================================
# 📊 Order Profit Detail — تحليل ربح/خسارة الطلب
# =====================================================================

@login_required
def order_profit_detail(request, order_id):
    """تحليل تكلفة وربحية طلب الطباعة على مستوى المهام.

    لكل PrintJob: machine_cost + ink_cost + designer_cost = full_cost
    على مستوى الطلب: revenue (net_total) − total_cost = gross_profit
    """
    from printing.models import PrintOrder, StaffPermission

    # صلاحية: staff أو can_view_profits
    if not request.user.is_staff:
        try:
            if not request.user.print_permissions.can_view_profits:
                from django.http import HttpResponseForbidden
                return HttpResponseForbidden("لا تملك صلاحية مشاهدة الأرباح.")
        except StaffPermission.DoesNotExist:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden("لا تملك صلاحية مشاهدة الأرباح.")

    order = get_object_or_404(
        PrintOrder.objects.select_related('customer', 'branch'),
        pk=order_id,
    )

    job_rows = []
    sum_machine = sum_ink = sum_designer = sum_revenue = Decimal('0')
    for job in order.jobs.select_related('machine', 'designer', 'designer__user', 'product_type').all():
        mc = job.machine_cost
        ic = job.ink_cost
        dc = job.designer_cost
        fc = mc + ic + dc
        rev = job.total_price
        job_rows.append({
            'job': job,
            'machine_cost': mc,
            'ink_cost': ic,
            'designer_cost': dc,
            'full_cost': fc,
            'revenue': rev,
            'profit': rev - fc,
            'margin_percent': round((rev - fc) / max(rev, Decimal('0.01')) * Decimal('100'), 2) if rev > 0 else Decimal('0'),
        })
        sum_machine += mc
        sum_ink += ic
        sum_designer += dc
        sum_revenue += rev

    total_cost = sum_machine + sum_ink + sum_designer
    net_total = order.net_total
    gross_profit = net_total - total_cost
    margin = round(gross_profit / max(net_total, Decimal('0.01')) * Decimal('100'), 2) if net_total > 0 else Decimal('0')

    # نسب التكلفة
    cost_breakdown = []
    if total_cost > 0:
        for label, value, color in [
            ('تشغيل الماكينات', sum_machine, '#f59e0b'),
            ('الأحبار', sum_ink, '#06b6d4'),
            ('أجور المصممين', sum_designer, '#ec4899'),
        ]:
            cost_breakdown.append({
                'label': label,
                'value': value,
                'color': color,
                'percent': (value / total_cost * Decimal('100')) if total_cost else Decimal('0'),
            })

    return render(request, 'printing/order_profit_detail.html', {
        'order': order,
        'job_rows': job_rows,
        'sum_machine': sum_machine,
        'sum_ink': sum_ink,
        'sum_designer': sum_designer,
        'total_cost': total_cost,
        'net_total': net_total,
        'discount': order.discount,
        'gross_profit': gross_profit,
        'margin': margin,
        'is_profitable': gross_profit > 0,
        'cost_breakdown': cost_breakdown,
    })


# =====================================================================
# 💰 9. عروض الأسعار (Quotations)
# =====================================================================

from django.views.decorators.csrf import csrf_exempt as _csrf_exempt
from django.contrib.auth.decorators import login_required as _login_required


@_login_required
@_csrf_exempt
def quotation_create(request):
    """POST /printing/quotation/create/ — إنشاء عرض سعر سريع."""
    from printing.models import PriceQuotation, QuotationLine, PrintCustomer

    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON غير صالح'}, status=400)

    title = (data.get('title') or '').strip()
    if not title or len(title) < 3:
        return JsonResponse({'error': 'العنوان مطلوب (3 حروف على الأقل)'}, status=400)

    customer_id = data.get('customer_id')
    customer_obj = None
    if customer_id:
        try:
            customer_obj = PrintCustomer.objects.get(pk=int(customer_id))
        except (PrintCustomer.DoesNotExist, ValueError, TypeError):
            return JsonResponse({'error': 'العميل غير موجود'}, status=404)

    lines_data = data.get('lines', [])
    if not lines_data or not isinstance(lines_data, list):
        return JsonResponse({'error': 'أضف بنداً واحداً على الأقل'}, status=400)

    try:
        discount = Decimal(str(data.get('discount', '0') or '0'))
        tax_percent = Decimal(str(data.get('tax_percent', '0') or '0'))
    except (ValueError, ArithmeticError):
        return JsonResponse({'error': 'قيم رقمية غير صالحة'}, status=400)

    quote = PriceQuotation.objects.create(
        customer=customer_obj,
        customer_name=(data.get('customer_name') or '').strip(),
        customer_phone=(data.get('customer_phone') or '').strip(),
        customer_whatsapp=(data.get('customer_whatsapp') or '').strip(),
        title=title,
        notes=(data.get('notes') or '').strip(),
        discount=discount,
        tax_percent=tax_percent,
        created_by=request.user,
        status='draft',
    )

    # Insert lines
    for idx, ln in enumerate(lines_data):
        try:
            qty = Decimal(str(ln.get('quantity', '1')))
            price = Decimal(str(ln.get('unit_price', '0')))
        except (ValueError, ArithmeticError):
            continue
        desc = (ln.get('description') or '').strip()
        if not desc:
            continue
        QuotationLine.objects.create(
            quotation=quote, description=desc[:300],
            quantity=qty, unit_price=price, sort_order=idx,
        )

    quote.recalc_totals()
    quote.refresh_from_db()

    public_url = request.build_absolute_uri(f'/printing/quotation/view/{quote.share_token}/')
    return JsonResponse({
        'success': True,
        'message': 'تم إنشاء العرض بنجاح',
        'quote_id': quote.pk,
        'quote_number': quote.quote_number,
        'total': str(quote.total),
        'share_url': public_url,
        'whatsapp_url': f'https://wa.me/?text={request.build_absolute_uri(public_url)}',
    })


def quotation_public_view(request, share_token):
    """GET /printing/quotation/view/<uuid>/ — صفحة عمومية للعميل لمشاهدة العرض."""
    from printing.models import PriceQuotation

    quote = get_object_or_404(PriceQuotation, share_token=share_token)

    # Auto-mark as sent on first view (لو لسه draft)
    if quote.status == 'draft':
        quote.status = 'sent'
        quote.sent_at = timezone.now()
        quote.save(update_fields=['status', 'sent_at'])

    # Auto-expire
    if quote.is_expired and quote.status == 'sent':
        quote.status = 'expired'
        quote.save(update_fields=['status'])

    return render(request, 'printing/quotation_public.html', {
        'quote': quote,
        'lines': quote.lines.all().order_by('sort_order'),
    })


@_csrf_exempt
def quotation_respond(request, share_token):
    """POST /printing/quotation/view/<uuid>/respond/ — العميل يقبل أو يرفض."""
    from printing.models import PriceQuotation

    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON غير صالح'}, status=400)

    action = data.get('action')
    if action not in ('accept', 'reject'):
        return JsonResponse({'error': 'إجراء غير صالح'}, status=400)

    quote = get_object_or_404(PriceQuotation, share_token=share_token)

    if quote.status not in ('sent', 'draft'):
        return JsonResponse({'error': 'لا يمكن الرد على هذا العرض الآن'}, status=400)

    if quote.is_expired:
        quote.status = 'expired'
        quote.save(update_fields=['status'])
        return JsonResponse({'error': 'هذا العرض منتهي الصلاحية'}, status=400)

    quote.status = 'accepted' if action == 'accept' else 'rejected'
    quote.responded_at = timezone.now()
    quote.save(update_fields=['status', 'responded_at'])

    return JsonResponse({
        'success': True,
        'message': 'شكراً! تم تسجيل قبولك للعرض. سيتواصل معك الفريق قريباً.' if action == 'accept'
                   else 'تم تسجيل رفضك للعرض. شكراً لوقتك.',
        'status': quote.status,
    })


@_login_required
@_csrf_exempt
def quotation_convert_to_order(request, quote_id):
    """POST — تحويل عرض مقبول إلى PrintOrder رسمي."""
    from printing.models import PriceQuotation, PrintOrder

    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    quote = get_object_or_404(PriceQuotation, pk=quote_id)

    if quote.status != 'accepted':
        return JsonResponse({'error': 'العرض لازم يبقى مقبول الأول'}, status=400)

    if quote.converted_order_id:
        return JsonResponse({'error': 'هذا العرض تحوّل لطلب بالفعل',
                            'order_id': quote.converted_order_id}, status=400)

    if not quote.customer:
        return JsonResponse({'error': 'لازم تربط العرض بعميل مسجل أولاً'}, status=400)

    order = PrintOrder.objects.create(
        customer=quote.customer,
        total_amount=quote.total,
        notes=f'تم إنشاؤه من عرض السعر #{quote.quote_number}\n\n{quote.notes}',
        status='confirmed',
    )

    quote.status = 'converted'
    quote.converted_order = order
    quote.save(update_fields=['status', 'converted_order'])

    return JsonResponse({
        'success': True,
        'message': f'تم تحويل العرض إلى طلب #{order.order_number}',
        'order_id': order.pk,
        'order_url': f'/secure-portal/printing/printorder/{order.pk}/change/',
    })


# =====================================================================
# 📈 10. تقرير الأرباح والخسائر (P&L Report)
# =====================================================================

@_login_required
def profit_loss_report(request):
    """تقرير شهري للإيرادات vs المصروفات + صافي الربح.

    Query: ?year=2026&month=6 (افتراضي: الشهر الحالي)
    """
    from printing.models import PrintTransaction, PrintOrder
    from django.db.models import Sum
    from datetime import date as _date

    today = timezone.now().date()
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
        if month < 1 or month > 12: month = today.month
        if year < 2020 or year > 2100: year = today.year
    except (ValueError, TypeError):
        year, month = today.year, today.month

    # Range
    start = _date(year, month, 1)
    if month == 12:
        end = _date(year + 1, 1, 1)
    else:
        end = _date(year, month + 1, 1)

    # حركات الشهر — نستخدم __date lookup عشان نقارن جزء التاريخ فقط
    # بدل من تمرير python date لحقل DateTimeField (يطلع RuntimeWarning عن
    # naive datetime ويزود فرصة باج timezone مستقبلاً).
    in_txns = PrintTransaction.objects.filter(
        transaction_type='in', date__date__gte=start, date__date__lt=end,
    )
    out_txns = PrintTransaction.objects.filter(
        transaction_type='out', date__date__gte=start, date__date__lt=end,
    )

    total_income = in_txns.aggregate(t=Sum('amount'))['t'] or Decimal('0')
    total_expense = out_txns.aggregate(t=Sum('amount'))['t'] or Decimal('0')
    net_profit = total_income - total_expense

    # تصنيف المصروفات بسيط من الـ description (keyword matching)
    def categorize(desc):
        d = (desc or '').lower()
        if any(k in d for k in ('راتب', 'مرتب', 'سلف', 'بونص', 'salary', 'payroll')):
            return ('💼 رواتب وعمولات', '#8b5cf6')
        if any(k in d for k in ('كهرب', 'كهرباء', 'فاتورة كهرباء', 'electricity')):
            return ('💡 كهرباء ومرافق', '#f59e0b')
        if any(k in d for k in ('ايجار', 'إيجار', 'rent')):
            return ('🏢 إيجارات', '#06b6d4')
        if any(k in d for k in ('ورق', 'حبر', 'خام', 'paper', 'ink', 'material')):
            return ('📦 خامات', '#10b981')
        if any(k in d for k in ('صيان', 'تصليح', 'maintain', 'repair')):
            return ('🔧 صيانة', '#ef4444')
        if any(k in d for k in ('شحن', 'توصيل', 'مواصلات', 'delivery', 'transport')):
            return ('🚚 شحن ومواصلات', '#3b82f6')
        return ('📌 أخرى', '#64748b')

    expense_cats = {}
    for txn in out_txns.values('description', 'amount'):
        cat, color = categorize(txn['description'])
        if cat not in expense_cats:
            expense_cats[cat] = {'name': cat, 'color': color, 'total': Decimal('0'), 'count': 0}
        expense_cats[cat]['total'] += txn['amount']
        expense_cats[cat]['count'] += 1
    expense_cats_list = sorted(expense_cats.values(), key=lambda c: c['total'], reverse=True)
    for c in expense_cats_list:
        c['percent'] = (c['total'] / total_expense * 100) if total_expense else Decimal('0')

    # طلبات الشهر
    orders_this_month = PrintOrder.objects.filter(date_created__date__gte=start, date_created__date__lt=end)
    orders_count = orders_this_month.count()
    orders_total_amount = orders_this_month.aggregate(t=Sum('total_amount'))['t'] or Decimal('0')
    orders_paid = orders_this_month.aggregate(t=Sum('paid_amount'))['t'] or Decimal('0')
    orders_outstanding = orders_total_amount - orders_paid

    # 6-month trend (للرسم البياني)
    trend = []
    for offset in range(5, -1, -1):
        m_month = month - offset
        m_year = year
        while m_month < 1:
            m_month += 12; m_year -= 1
        m_start = _date(m_year, m_month, 1)
        if m_month == 12:
            m_end = _date(m_year + 1, 1, 1)
        else:
            m_end = _date(m_year, m_month + 1, 1)
        m_in = PrintTransaction.objects.filter(transaction_type='in', date__date__gte=m_start, date__date__lt=m_end).aggregate(t=Sum('amount'))['t'] or Decimal('0')
        m_out = PrintTransaction.objects.filter(transaction_type='out', date__date__gte=m_start, date__date__lt=m_end).aggregate(t=Sum('amount'))['t'] or Decimal('0')
        trend.append({
            'label': f'{m_year}/{m_month:02d}',
            'income': float(m_in),
            'expense': float(m_out),
            'net': float(m_in - m_out),
        })

    # محاسب: تليفون التحقق
    months_ar = ['يناير', 'فبراير', 'مارس', 'إبريل', 'مايو', 'يونيو',
                 'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']

    return render(request, 'printing/profit_loss.html', {
        'year': year, 'month': month, 'month_name': months_ar[month - 1],
        'total_income': total_income,
        'total_expense': total_expense,
        'net_profit': net_profit,
        'is_profit': net_profit >= 0,
        'profit_margin': (net_profit / total_income * 100) if total_income else Decimal('0'),
        'expense_cats': expense_cats_list,
        'orders_count': orders_count,
        'orders_total_amount': orders_total_amount,
        'orders_paid': orders_paid,
        'orders_outstanding': orders_outstanding,
        'trend': trend,
        'in_count': in_txns.count(),
        'out_count': out_txns.count(),
    })
