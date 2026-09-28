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


def _parse_date(value):
    """YYYY-MM-DD → date, anything else → None (a bad ?from= used to 500)."""
    from datetime import date as _d
    try:
        return _d.fromisoformat((value or '').strip())
    except ValueError:
        return None


def _can_see_money(user, flag):
    """Staff, or a printing employee whose StaffPermission has ``flag``."""
    if user.is_staff or user.is_superuser:
        return True
    perms = getattr(user, 'print_permissions', None)
    try:
        return bool(perms and getattr(perms, flag, False))
    except Exception:
        return False




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

    if not (_can_see_money(request.user, 'can_view_reports')
            or _can_see_money(request.user, 'can_manage_treasury')):
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("لا تملك صلاحية مشاهدة كشوف الحساب.")

    customer = get_object_or_404(PrintCustomer, pk=customer_id)

    # فلترة بالتاريخ
    d_from = _parse_date(request.GET.get('from'))
    d_to = _parse_date(request.GET.get('to'))
    date_from = d_from.isoformat() if d_from else ''
    date_to = d_to.isoformat() if d_to else ''

    # 🐛 [FIX]: الطلبات الملغية والمسودات كانت بتتحسب "فواتير" على العميل،
    #    والمبالغ المستردة له (سحب مربوط بطلبه) ماكانتش بتظهر خالص.
    orders_qs = PrintOrder.objects.filter(customer=customer).exclude(status__in=('draft', 'cancelled'))
    payments_qs = PrintTransaction.objects.filter(order__customer=customer)

    # الرصيد المرحّل من قبل بداية الفترة — عشان الرصيد الجاري يبقى صح.
    opening = Decimal('0')
    if d_from:
        before_orders = orders_qs.filter(date_created__date__lt=d_from)
        before_pay = payments_qs.filter(date__date__lt=d_from)
        opening = (
            sum((o.net_total for o in before_orders), Decimal('0'))
            - (before_pay.filter(transaction_type='in').aggregate(t=Sum('amount'))['t'] or Decimal('0'))
            + (before_pay.filter(transaction_type='out').aggregate(t=Sum('amount'))['t'] or Decimal('0'))
        )
        orders_qs = orders_qs.filter(date_created__date__gte=d_from)
        payments_qs = payments_qs.filter(date__date__gte=d_from)
    if d_to:
        orders_qs = orders_qs.filter(date_created__date__lte=d_to)
        payments_qs = payments_qs.filter(date__date__lte=d_to)

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
        order_no = p.order.order_number if p.order else ''
        is_refund = p.transaction_type == 'out'
        events.append({
            'date': p.date,
            'type': 'refund' if is_refund else 'payment',
            'ref': f'#{p.pk}',
            'description': p.description or (f'استرداد من فاتورة #{order_no}' if is_refund
                                             else f'دفعة على فاتورة #{order_no}'),
            'debit': p.amount if is_refund else Decimal('0'),    # استرداد له → يرجع مدين
            'credit': Decimal('0') if is_refund else p.amount,   # دفع (دائن)
            'status': p.treasury.name if p.treasury else '',
            'obj_id': p.pk,
        })

    events.sort(key=lambda e: e['date'])

    running = opening
    for ev in events:
        running += ev['debit'] - ev['credit']
        ev['balance'] = running

    # إجماليات
    total_invoiced = sum((e['debit'] for e in events), Decimal('0'))
    total_paid = sum((e['credit'] for e in events), Decimal('0'))
    final_balance = opening + total_invoiced - total_paid

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
        'opening_balance': opening,
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
    from printing.models import PrintOrder

    # صلاحية: staff أو can_view_profits
    if not _can_see_money(request.user, 'can_view_profits'):
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("لا تملك صلاحية مشاهدة الأرباح.")

    order = get_object_or_404(
        PrintOrder.objects.select_related('customer', 'branch'),
        pk=order_id,
    )

    job_rows = []
    sum_machine = sum_ink = sum_material = sum_designer = sum_revenue = Decimal('0')
    total_cost = Decimal('0')
    for job in order.jobs.select_related('machine', 'designer', 'designer__user', 'product_type').all():
        mc = job.machine_cost
        ic = job.ink_cost
        mat = job.material_cost
        dc = job.designer_cost
        # 🐛 [FIX]: المهام المكتملة ليها تكلفة مثبّتة (snapshot) وقت الإكمال —
        #    صفحة الربح كانت بتعيد الحساب بأسعار النهارده فتختلف عن لوحة
        #    التحكم وعن ربح الطلب في الأدمن. ودلوقتي الخامات داخلة في التكلفة.
        fc = (job.actual_cost + dc) if (job.is_complete and job.actual_cost) else (mc + ic + mat + dc)
        rev = job.total_price
        job_rows.append({
            'job': job,
            'machine_cost': mc,
            'ink_cost': ic,
            'material_cost': mat,
            'designer_cost': dc,
            'full_cost': fc,
            'revenue': rev,
            'profit': rev - fc,
            'margin_percent': round((rev - fc) / max(rev, Decimal('0.01')) * Decimal('100'), 2) if rev > 0 else Decimal('0'),
        })
        sum_machine += mc
        sum_ink += ic
        sum_material += mat
        sum_designer += dc
        sum_revenue += rev
        total_cost += fc

    revenue = order.revenue
    net_total = order.net_total
    gross_profit = revenue - total_cost
    margin = round(gross_profit / max(revenue, Decimal('0.01')) * Decimal('100'), 2) if revenue > 0 else Decimal('0')

    # نسب التكلفة
    cost_breakdown = []
    parts_total = sum_machine + sum_ink + sum_material + sum_designer
    if parts_total > 0:
        for label, value, color in [
            ('تشغيل الماكينات', sum_machine, '#f59e0b'),
            ('الأحبار', sum_ink, '#06b6d4'),
            ('الخامات', sum_material, '#10b981'),
            ('أجور المصممين', sum_designer, '#ec4899'),
        ]:
            if not value:
                continue
            cost_breakdown.append({
                'label': label,
                'value': value,
                'color': color,
                'percent': value / parts_total * Decimal('100'),
            })

    return render(request, 'printing/order_profit_detail.html', {
        'order': order,
        'job_rows': job_rows,
        'sum_machine': sum_machine,
        'sum_ink': sum_ink,
        'sum_material': sum_material,
        'sum_designer': sum_designer,
        'total_cost': total_cost,
        'revenue': revenue,
        'net_total': net_total,
        'discount': order.discount,
        'tax_amount': order.tax_amount,
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
    if not discount.is_finite() or discount < 0:
        return JsonResponse({'error': 'الخصم لازم يكون صفر أو أكتر'}, status=400)
    if not tax_percent.is_finite() or not (0 <= tax_percent <= 100):
        return JsonResponse({'error': 'نسبة الضريبة لازم تكون بين 0 و 100'}, status=400)

    # 🐛 [FIX]: البنود الغلط (كمية سالبة/صفر، سعر سالب) كانت بتتحفظ وتطلّع
    #    إجمالي سالب، والعرض نفسه كان بيتحفظ حتى لو كل البنود اتشالت.
    clean_lines = []
    for idx, ln in enumerate(lines_data):
        if not isinstance(ln, dict):
            continue
        desc = (ln.get('description') or '').strip()
        if not desc:
            continue
        try:
            qty = Decimal(str(ln.get('quantity', '1')))
            price = Decimal(str(ln.get('unit_price', '0')))
        except (ValueError, ArithmeticError):
            return JsonResponse({'error': f'أرقام البند «{desc[:40]}» غير صالحة'}, status=400)
        if not qty.is_finite() or not price.is_finite() or qty <= 0 or price < 0:
            return JsonResponse({'error': f'البند «{desc[:40]}»: الكمية لازم أكبر من صفر والسعر مش بالسالب'},
                                status=400)
        clean_lines.append((idx, desc[:300], qty, price))
    if not clean_lines:
        return JsonResponse({'error': 'أضف بنداً واحداً على الأقل'}, status=400)

    from django.db import transaction as _txn
    with _txn.atomic():
        quote = _create_quote(request, data, customer_obj, title, discount, tax_percent, clean_lines)

    public_url = request.build_absolute_uri(f'/printing/quotation/view/{quote.share_token}/')
    from urllib.parse import quote as _urlquote
    msg = f'عرض سعر #{quote.quote_number} — {quote.title}\n{public_url}'
    wa_number = ''.join(ch for ch in (quote.customer_whatsapp or quote.customer_phone
                                      or (customer_obj.phone if customer_obj else '') or '') if ch.isdigit())
    if wa_number.startswith('0'):
        wa_number = '2' + wa_number   # رقم مصري محلي 01xxxxxxxxx → 201xxxxxxxxx
    return JsonResponse({
        'success': True,
        'message': 'تم إنشاء العرض بنجاح',
        'quote_id': quote.pk,
        'quote_number': quote.quote_number,
        'total': str(quote.total),
        'share_url': public_url,
        'whatsapp_url': f'https://wa.me/{wa_number}?text={_urlquote(msg)}',
    })


def _create_quote(request, data, customer_obj, title, discount, tax_percent, clean_lines):
    from printing.models import PriceQuotation, QuotationLine
    quote = PriceQuotation.objects.create(
        customer=customer_obj,
        customer_name=(data.get('customer_name') or '').strip()[:150],
        customer_phone=(data.get('customer_phone') or '').strip()[:20],
        customer_whatsapp=(data.get('customer_whatsapp') or '').strip()[:20],
        title=title[:200],
        notes=(data.get('notes') or '').strip(),
        discount=discount,
        tax_percent=tax_percent,
        created_by=request.user,
        status='draft',
    )
    for idx, desc, qty, price in clean_lines:
        QuotationLine.objects.create(
            quotation=quote, description=desc,
            quantity=qty, unit_price=price, sort_order=idx,
        )
    quote.recalc_totals()
    quote.refresh_from_db()
    return quote


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


def convert_quotation(quote, *, user=None):
    """
    Accepted quote → confirmed PrintOrder, one PrintJob per quote line.

    🐛 [FIX]: التحويل كان بيعمل طلب "فاضي" بإجمالي = إجمالي العرض بعد
    الخصم والضريبة، من غير ولا مهمة — فالإنتاج مايعرفش يشتغل على إيه،
    والخصم/الضريبة بيضيعوا (والربح بيتحسب على مبلغ فيه ضريبة). دلوقتي كل
    بند بيبقى مهمة بسعره، والخصم والضريبة بيتنقلوا في خاناتهم.

    Raises ValidationError when the quote can't be converted.
    """
    from django.core.exceptions import ValidationError
    from django.db import transaction as _txn
    from printing.models import PriceQuotation, PrintJob, PrintOrder

    with _txn.atomic():
        quote = PriceQuotation.objects.select_for_update().get(pk=quote.pk)
        if quote.converted_order_id or quote.status == 'converted':
            raise ValidationError('هذا العرض تحوّل لطلب بالفعل')
        if quote.status != 'accepted':
            raise ValidationError('العرض لازم يبقى مقبول الأول')
        if not quote.customer_id:
            raise ValidationError('لازم تربط العرض بعميل مسجل أولاً')

        lines = list(quote.lines.all().order_by('sort_order', 'pk'))
        subtotal = sum((l.line_total for l in lines), Decimal('0'))
        discount = min(quote.discount, subtotal)
        tax = ((subtotal - discount) * quote.tax_percent / Decimal('100')).quantize(Decimal('0.01'))
        order = PrintOrder.objects.create(
            customer=quote.customer,
            total_amount=subtotal,
            discount=discount,
            tax_amount=tax,
            notes=f'تم إنشاؤه من عرض السعر #{quote.quote_number}\n\n{quote.notes}'.strip(),
            status='confirmed',
        )
        for line in lines:
            qty = line.quantity
            if qty == qty.to_integral_value():
                job_qty, job_price, desc = int(qty), line.unit_price, line.description
            else:
                # كمية كسرية (متر، كيلو…) — PrintJob.quantity عدد صحيح، فنسجّلها
                # كمهمة واحدة بسعر البند كامل ونحفظ الكمية في الوصف.
                job_qty, job_price = 1, line.line_total
                desc = f'{line.description} ({qty.normalize()} × {line.unit_price})'
            PrintJob.objects.create(
                order=order, description=desc[:300],
                quantity=job_qty, copies=1, unit_price=job_price,
            )
        order.recalc_total_from_jobs()

        quote.status = 'converted'
        quote.converted_order = order
        quote.save(update_fields=['status', 'converted_order'])
    return order


@_login_required
@require_POST
def quotation_convert_to_order(request, quote_id):
    """POST — تحويل عرض مقبول إلى PrintOrder رسمي."""
    from django.core.exceptions import ValidationError
    from printing.models import PriceQuotation

    if not (_can_see_money(request.user, 'can_create_orders')):
        return JsonResponse({'error': 'لا تملك صلاحية إنشاء طلبات'}, status=403)

    quote = get_object_or_404(PriceQuotation, pk=quote_id)
    try:
        order = convert_quotation(quote, user=request.user)
    except ValidationError as exc:
        quote.refresh_from_db()
        body = {'error': '؛ '.join(exc.messages)}
        if quote.converted_order_id:
            body['order_id'] = quote.converted_order_id
        return JsonResponse(body, status=400)

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

    # 🔐 التقرير فيه دخل ومصروفات المطبعة كلها — كان مفتوح لأي موظف.
    if not (_can_see_money(request.user, 'can_view_reports')
            or _can_see_money(request.user, 'can_view_profits')):
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("لا تملك صلاحية مشاهدة التقارير المالية.")

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
    # 🐛 [FIX]: المتبقي كان = الإجمالي − المدفوع من غير ما يطرح الخصم، وكان
    #    بيحسب الطلبات الملغية والمسودات — فالمتبقي على العملاء بيطلع أكبر من الحقيقة.
    orders_this_month = (PrintOrder.objects
                         .filter(date_created__date__gte=start, date_created__date__lt=end)
                         .exclude(status__in=('draft', 'cancelled')))
    orders_count = orders_this_month.count()
    _agg = orders_this_month.aggregate(
        t=Sum(F('total_amount') - F('discount') + F('tax_amount')), p=Sum('paid_amount'))
    orders_total_amount = _agg['t'] or Decimal('0')
    orders_paid = _agg['p'] or Decimal('0')
    orders_outstanding = max(orders_total_amount - orders_paid, Decimal('0'))

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
