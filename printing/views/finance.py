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
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from erp_core.decorators import tenant_or_404
from django.views.decorators.http import require_POST
from django.db import connection
from django.db.models import Sum, Count, Avg, Q, F
from django.utils import timezone
from django.utils.translation import gettext as _

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
    """Superuser; or the employee's StaffPermission ``flag``; or staff with no StaffPermission row.

    🐛 [FIX]: كل موظف بيدخل لوحة المطبعة لازم يبقى is_staff — فالشرط القديم
    («staff يشوف كل حاجة») كان بيتجاهل صلاحيات الموظف اللي صاحب المطبعة
    ضبطها: الكاشير يشوف الخزنة والأرباح حتى لو مقفولة عليه.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    from django.core.exceptions import ObjectDoesNotExist
    try:
        perms = user.print_permissions
    except (ObjectDoesNotExist, AttributeError):
        perms = None
    if perms is not None:
        return bool(getattr(perms, flag, False))
    return bool(user.is_staff)




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
    from printing.models import PrintCreditNote, PrintCustomer, PrintOrder, PrintTransaction

    if not (_can_see_money(request.user, 'can_view_reports')
            or _can_see_money(request.user, 'can_manage_treasury')):
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden(_("لا تملك صلاحية مشاهدة كشوف الحساب."))

    customer = get_object_or_404(PrintCustomer, pk=customer_id)

    # فلترة بالتاريخ
    d_from = _parse_date(request.GET.get('from'))
    d_to = _parse_date(request.GET.get('to'))
    date_from = d_from.isoformat() if d_from else ''
    date_to = d_to.isoformat() if d_to else ''

    # 🐛 [FIX]: الطلبات الملغية والمسودات كانت بتتحسب "فواتير" على العميل،
    #    والمبالغ المستردة له (سحب مربوط بطلبه) ماكانتش بتظهر خالص.
    orders_qs = PrintOrder.objects.filter(customer=customer).exclude(status__in=('draft', 'cancelled'))
    # 💳 دفعات على طلباته + دفعات على حسابه من غير طلب (رصيد مقدّم).
    payments_qs = customer.payments_qs()
    # ↩️ مرتجعات/خصم/إعدام (إشعارات دائنة) بتنزل من مديونيته — على طلب أو على حسابه.
    credits_qs = PrintCreditNote.objects.filter(customer=customer)
    # 📖 الرصيد الافتتاحي (قبل السيستم) — موجب عليه، سالب ليه.
    opening_amount = customer.opening_balance or Decimal('0')
    opening_in_range = bool(opening_amount)

    # الرصيد المرحّل من قبل بداية الفترة — عشان الرصيد الجاري يبقى صح.
    opening = Decimal('0')
    if d_from:
        before_orders = orders_qs.filter(date_created__date__lt=d_from)
        before_pay = payments_qs.filter(date__date__lt=d_from)
        opening = (
            sum((o.net_total for o in before_orders), Decimal('0'))
            - (before_pay.filter(transaction_type='in').aggregate(t=Sum('amount'))['t'] or Decimal('0'))
            + (before_pay.filter(transaction_type='out').aggregate(t=Sum('amount'))['t'] or Decimal('0'))
            - sum((c.total for c in credits_qs.filter(date__lt=d_from)), Decimal('0'))
        )
        if opening_amount and customer.opening_date < d_from:
            opening += opening_amount
            opening_in_range = False
        orders_qs = orders_qs.filter(date_created__date__gte=d_from)
        payments_qs = payments_qs.filter(date__date__gte=d_from)
        credits_qs = credits_qs.filter(date__gte=d_from)
    if d_to:
        orders_qs = orders_qs.filter(date_created__date__lte=d_to)
        payments_qs = payments_qs.filter(date__date__lte=d_to)
        credits_qs = credits_qs.filter(date__lte=d_to)
        if customer.opening_date > d_to:
            opening_in_range = False

    # دمج الفواتير + المدفوعات في timeline واحد مرتب بالتاريخ
    events = []
    for o in orders_qs.select_related('branch'):
        events.append({
            'date': o.date_created,
            'type': 'invoice',
            'ref': o.order_number,
            'description': _('فاتورة #%(no)s') % {'no': o.order_number} + (f' — {o.notes[:60]}' if o.notes else ''),
            'debit': o.net_total,   # عليه (مدين)
            'credit': Decimal('0'),
            'status': o.get_status_display(),
            'obj_id': o.pk,
        })
    for p in payments_qs.select_related('treasury', 'order'):
        order_no = p.order.order_number if p.order else ''
        is_refund = p.transaction_type == 'out'
        if not p.order_id:
            default_desc = _('رد من الرصيد المقدّم') if is_refund else _('دفعة على الحساب (رصيد مقدّم)')
        else:
            default_desc = ((_('استرداد من فاتورة #%(no)s') if is_refund
                             else _('دفعة على فاتورة #%(no)s')) % {'no': order_no})
        events.append({
            'date': p.date,
            'type': 'refund' if is_refund else 'payment',
            'ref': order_no or f'#{p.pk}',
            'description': p.description or default_desc,
            'debit': p.amount if is_refund else Decimal('0'),    # استرداد له → يرجع مدين
            'credit': Decimal('0') if is_refund else p.amount,   # دفع (دائن)
            'status': p.treasury.name if p.treasury else '',
            'obj_id': p.pk,
        })

    def _day(d):
        return timezone.make_aware(timezone.datetime.combine(d, timezone.datetime.min.time()))

    if opening_in_range:
        events.append({
            'date': _day(customer.opening_date), 'type': 'opening', 'ref': '—',
            'description': _('رصيد افتتاحي (قبل السيستم)'),
            'debit': max(opening_amount, Decimal('0')), 'credit': max(-opening_amount, Decimal('0')),
            'status': '', 'obj_id': customer.pk,
        })
    for cn in credits_qs.select_related('order'):
        where = (_('على فاتورة #%(no)s') % {'no': cn.order.order_number}) if cn.order_id else _('على الحساب')
        events.append({
            'date': _day(cn.date),
            'type': 'credit_note',
            'ref': cn.number,
            'description': f'{cn.get_kind_display()} {where} — {cn.reason}',
            'debit': Decimal('0'),
            'credit': cn.total,
            'status': '',
            'obj_id': cn.pk,
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
        'available_credit': customer.account_summary()['credit'],
        'credit_limit': customer.credit_limit,
    }

    if request.GET.get('export') == 'xlsx':
        from printing.views.accounting import xlsx_response
        rows = [(ev['date'], ev['ref'], ev['description'], ev['debit'] or None, ev['credit'] or None, ev['balance'])
                for ev in events]
        if date_from:
            rows.insert(0, (None, '', _('رصيد مرحّل من قبل'), None, None, opening))
        return xlsx_response(f'customer-{customer.pk}-statement',
                             _('كشف حساب العميل %(n)s') % {'n': customer.name},
                             [_('التاريخ'), _('المرجع'), _('البيان'), _('عليه'), _('له'), _('الرصيد')], rows,
                             totals=(None, '', _('الإجمالي'), total_invoiced, total_paid, final_balance))
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
        return HttpResponseForbidden(_("لا تملك صلاحية مشاهدة الأرباح."))

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
        return JsonResponse({'error': _('JSON غير صالح')}, status=400)

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


@tenant_or_404
def quotation_public_view(request, share_token):
    """GET /printing/quotation/view/<uuid>/ — صفحة عمومية للعميل لمشاهدة العرض."""
    from printing.models import PriceQuotation

    quote = get_object_or_404(PriceQuotation, share_token=share_token)

    # Auto-mark as sent on first view (لو لسه draft)
    # 🐛 [FIX]: الموظف لما يفتح «معاينة رابط العميل» كان العرض بيتعلّم «اتبعت»
    # قبل ما يتبعت فعلاً. الزيارة اللي بتحسب هي زيارة العميل (مش حد مسجّل دخول).
    is_staff_preview = getattr(request.user, 'is_authenticated', False) and request.user.is_staff
    if quote.status == 'draft' and not is_staff_preview:
        quote.status = 'sent'
        quote.sent_at = timezone.now()
        quote.save(update_fields=['status', 'sent_at'])

    # Auto-expire
    if quote.is_expired and quote.status == 'sent':
        quote.status = 'expired'
        quote.save(update_fields=['status'])

    shop = getattr(request, 'tenant', None) or getattr(connection, 'tenant', None)
    shop_logo = ''
    try:
        if getattr(shop, 'logo', None):
            shop_logo = shop.logo.url
    except Exception:
        shop_logo = ''
    shop_phone = getattr(shop, 'phone', '') or ''
    from printing.admin import _wa_number
    return render(request, 'printing/quotation_public.html', {
        'quote': quote,
        'lines': quote.lines.all().order_by('sort_order'),
        'shop_name': getattr(shop, 'name', '') or 'Mouss Tec',
        'shop_logo': shop_logo,
        'shop_phone': shop_phone,
        'shop_whatsapp': _wa_number(shop_phone),
        'wa_text': _('بخصوص عرض السعر رقم %(no)s') % {'no': quote.quote_number},
    })


@_csrf_exempt
@tenant_or_404
def quotation_respond(request, share_token):
    """POST /printing/quotation/view/<uuid>/respond/ — العميل يقبل أو يرفض."""
    from printing.models import PriceQuotation

    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': _('JSON غير صالح')}, status=400)

    action = data.get('action')
    if action not in ('accept', 'reject'):
        return JsonResponse({'error': _('إجراء غير صالح')}, status=400)

    quote = get_object_or_404(PriceQuotation, share_token=share_token)

    if quote.status not in ('sent', 'draft'):
        return JsonResponse({'error': _('لا يمكن الرد على هذا العرض الآن')}, status=400)

    if quote.is_expired:
        quote.status = 'expired'
        quote.save(update_fields=['status'])
        return JsonResponse({'error': _('هذا العرض منتهي الصلاحية')}, status=400)

    quote.status = 'accepted' if action == 'accept' else 'rejected'
    quote.responded_at = timezone.now()
    quote.save(update_fields=['status', 'responded_at'])

    return JsonResponse({
        'success': True,
        'message': _('شكراً! تم تسجيل قبولك للعرض. سيتواصل معك الفريق قريباً.') if action == 'accept'
                   else _('تم تسجيل رفضك للعرض. شكراً لوقتك.'),
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
            # 🐛 [FIX]: العرض معمول أصلاً لعميل مش مسجّل (اسم + تليفون) — والتحويل
            # كان بيرفض لحد ما حد يسجّله بإيده. بنلاقيه بالتليفون أو نسجّله.
            from printing.models import PrintCustomer
            name = (quote.customer_name or '').strip()
            phone = (quote.customer_phone or quote.customer_whatsapp or '').strip()
            if not name:
                raise ValidationError('اكتب اسم العميل في العرض الأول (أو اختار عميل مسجّل)')
            customer = PrintCustomer.objects.filter(phone=phone).first() if phone else None
            if customer is None:
                customer = PrintCustomer.objects.create(
                    name=name, phone=phone, whatsapp=(quote.customer_whatsapp or phone).strip())
            quote.customer = customer
            quote.save(update_fields=['customer'])

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
        # 💳 العميل اللي عدّى حد الائتمان: الطلب بيتعمل مسودة لحد ما يدفع.
        order.credit_hold_message = order.apply_credit_hold(previous_status=None) or ''

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

    message = f'تم تحويل العرض إلى طلب #{order.order_number}'
    if order.credit_hold_message:
        message += f' — {order.credit_hold_message}'
    return JsonResponse({
        'success': True,
        'message': message,
        'credit_hold': bool(order.credit_hold_message),
        'order_id': order.pk,
        'order_url': reverse('admin:printing_printorder_change', args=[order.pk]),
    })


# =====================================================================
# 📈 10. تقرير الأرباح والخسائر (P&L Report)
# =====================================================================

@_login_required
def profit_loss_report(request):
    """قائمة الدخل الشهرية بالاستحقاق — من الدفتر العام.

    🐛 [FIX]: التقرير كان نقدي: أي فلوس داخلة الخزنة «إيراد» (حتى عربون طلب لسه
    ماتسلّمش، أو رأس مال حطّه المالك)، وأي فلوس طالعة «مصروف» (حتى شراء خامات
    لسه في المخزن أو سداد مورد أو سلفة). دلوقتي: الإيراد = الطلبات اللي اتسلّمت
    في الشهر، والتكلفة = الخامات اللي اتصرفت عليها، والمصروفات = اللي استحقّت في
    الشهر. حركة النقدية بتظهر لوحدها للمقارنة.

    Query: ?year=2026&month=6 (افتراضي: الشهر الحالي)
    """
    from datetime import date as _date, timedelta as _td
    from django.db.models import Sum
    from inventory.services.accounting_reports import AccountingReportService
    from printing import ledger
    from printing.models import PrintOrder, PrintTransaction

    # 🔐 التقرير فيه دخل ومصروفات المطبعة كلها — كان مفتوح لأي موظف.
    if not (_can_see_money(request.user, 'can_view_reports')
            or _can_see_money(request.user, 'can_view_profits')):
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden(_("لا تملك صلاحية مشاهدة التقارير المالية."))
    ledger.ensure_bootstrapped()

    # 🐛 [FIX]: كان تاريخ UTC — أول يوم في الشهر من ١٢ لـ ٣ الفجر كان بيفتح الشهر اللي فات
    today = timezone.localdate()
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
        if month < 1 or month > 12: month = today.month
        if year < 2020 or year > 2100: year = today.year
    except (ValueError, TypeError):
        year, month = today.year, today.month

    def month_range(y, m):
        start = _date(y, m, 1)
        end = _date(y + 1, 1, 1) if m == 12 else _date(y, m + 1, 1)
        return start, end

    # 🏢 فلتر الفرع: ?branch=<id> لفرع واحد، ?branch=none للقيود العامة (من غير فرع)
    from printing.models import PrintBranch, PrintTreasury, PrintTreasuryTransfer
    branch_param = (request.GET.get('branch') or '').strip()
    branch_obj = None
    if branch_param == 'none':
        branch_filter = None
    elif branch_param.isdigit():
        branch_obj = PrintBranch.objects.filter(pk=int(branch_param)).first()
        branch_filter = branch_obj.pk if branch_obj else ''
    else:
        branch_filter = ''

    def income(d_from, d_to):
        if branch_filter == '':
            return AccountingReportService.income_statement(d_from, d_to)
        return ledger.branch_income_statement(d_from, d_to, branch_filter)

    start, end = month_range(year, month)
    pnl = income(start, end - _td(days=1))
    cogs_code = ledger.PRINT_ACCOUNTS['cogs'][0]
    total_income = pnl['revenue']['total']
    total_expense = pnl['expenses']['total']
    net_profit = pnl['net_profit']
    cogs = sum((i['amount'] for i in pnl['expenses']['items'] if i['code'] == cogs_code), Decimal('0'))
    gross_profit = total_income - cogs

    palette = ['#8b5cf6', '#f59e0b', '#06b6d4', '#10b981', '#ef4444', '#3b82f6', '#ec4899', '#64748b']
    operating = [i for i in pnl['expenses']['items'] if i['code'] != cogs_code]
    operating_total = sum((i['amount'] for i in operating), Decimal('0'))
    expense_cats_list = [{
        'name': i['name'], 'code': i['code'], 'total': i['amount'],
        'color': palette[n % len(palette)],
        'percent': (i['amount'] / operating_total * 100) if operating_total else Decimal('0'),
    } for n, i in enumerate(sorted(operating, key=lambda i: i['amount'], reverse=True))]

    # 💵 حركة النقدية للمقارنة (من غير التحويلات بين الخزائن)
    # 🐛 [FIX]: الشيك أو الخصم من المنبع مش فلوس في الخزنة — النقدية = الخزن والبنوك بس،
    #    والشيك بيدخل النقدية يوم ما يتصرف (تحويل من الحافظة للبنك).
    money = PrintTreasury.MONEY_KINDS
    cash = (PrintTransaction.objects.filter(date__date__gte=start, date__date__lt=end, treasury__kind__in=money)
            .exclude(category='transfer'))
    settled = PrintTreasuryTransfer.objects.filter(date__date__gte=start, date__date__lt=end)
    if branch_obj is not None:
        cash = cash.filter(treasury__branch=branch_obj)
        settled = settled.filter(Q(from_treasury__branch=branch_obj) | Q(to_treasury__branch=branch_obj))
    cash_in = ((cash.filter(transaction_type='in').aggregate(t=Sum('amount'))['t'] or Decimal('0'))
               + (settled.filter(to_treasury__kind__in=money).exclude(from_treasury__kind__in=money)
                  .aggregate(t=Sum('amount'))['t'] or Decimal('0')))
    cash_out = ((cash.filter(transaction_type='out').aggregate(t=Sum('amount'))['t'] or Decimal('0'))
                + (settled.filter(from_treasury__kind__in=money).exclude(to_treasury__kind__in=money)
                   .aggregate(t=Sum('amount'))['t'] or Decimal('0')))

    # 📦 الطلبات اللي اتسلّمت في الشهر (= الإيراد المعترف بيه)
    delivered = PrintOrder.objects.filter(status='delivered', date_delivered__date__gte=start,
                                          date_delivered__date__lt=end)
    if branch_obj is not None:
        delivered = delivered.filter(branch=branch_obj)
    _agg = delivered.aggregate(t=Sum(F('total_amount') - F('discount') + F('tax_amount')), p=Sum('paid_amount'))
    orders_total_amount = _agg['t'] or Decimal('0')
    orders_paid = _agg['p'] or Decimal('0')

    # 6-month trend (للرسم البياني) — بالاستحقاق
    trend = []
    for offset in range(5, -1, -1):
        m_month, m_year = month - offset, year
        while m_month < 1:
            m_month += 12
            m_year -= 1
        m_start, m_end = month_range(m_year, m_month)
        m = income(m_start, m_end - _td(days=1))
        trend.append({
            'label': f'{m_year}/{m_month:02d}',
            'income': float(m['revenue']['total']),
            'expense': float(m['expenses']['total']),
            'net': float(m['net_profit']),
        })
    peak = max([max(t['income'], t['expense']) for t in trend] + [1])
    for t in trend:
        t['income_pct'] = round(t['income'] / peak * 100)
        t['expense_pct'] = round(t['expense'] / peak * 100)

    months_ar = ['يناير', 'فبراير', 'مارس', 'إبريل', 'مايو', 'يونيو',
                 'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']

    return render(request, 'printing/profit_loss.html', {
        'year': year, 'month': month, 'month_name': months_ar[month - 1],
        'branches': PrintBranch.objects.all(), 'branch_param': branch_param, 'branch_obj': branch_obj,
        'revenue_items': pnl['revenue']['items'],
        'total_income': total_income,
        'cogs': cogs,
        'gross_profit': gross_profit,
        'total_expense': total_expense,
        'operating_total': operating_total,
        'net_profit': net_profit,
        'is_profit': net_profit >= 0,
        'profit_margin': (net_profit / total_income * 100) if total_income else Decimal('0'),
        'expense_cats': expense_cats_list,
        'cash_in': cash_in,
        'cash_out': cash_out,
        'cash_net': cash_in - cash_out,
        'orders_count': delivered.count(),
        'orders_total_amount': orders_total_amount,
        'orders_paid': orders_paid,
        'orders_outstanding': max(orders_total_amount - orders_paid, Decimal('0')),
        'trend': trend,
    })


# =====================================================================
# 🧾 11. فاتورة / إيصال الطلب + أمر الشغل للإنتاج
# =====================================================================

@login_required
@tenant_or_404
def order_invoice(request, order_id):
    """A4 invoice/receipt for the customer — or (``?ticket=1``) a job ticket for production.

    🆕 الطلب ماكانش ليه أي مستند: لا فاتورة تتسلّم للعميل ولا أمر شغل للإنتاج.
    """
    from printing.models import PrintOrder

    order = get_object_or_404(PrintOrder.objects.select_related('customer', 'branch'), pk=order_id)
    shop = getattr(request, 'tenant', None) or getattr(connection, 'tenant', None)
    shop_logo = ''
    try:
        if getattr(shop, 'logo', None):
            shop_logo = shop.logo.url
    except Exception:
        shop_logo = ''
    return render(request, 'printing/order_invoice.html', {
        'order': order,
        'jobs': order.jobs.select_related('machine', 'product_type').order_by('pk'),
        'payments': order.printtransaction_set.select_related('treasury').order_by('date'),
        'ticket': request.GET.get('ticket') == '1',
        'shop_name': getattr(shop, 'name', '') or 'Mouss Tec',
        'shop_phone': getattr(shop, 'phone', '') or '',
        'shop_register': getattr(shop, 'commercial_register', '') or '',
        'shop_logo': shop_logo,
        'printed_at': timezone.localtime(),
    })
