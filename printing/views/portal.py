"""🔗 بوابة عميل المطبعة + 🛒 المتجر الأونلاين + 💲 حاسبة الأسعار.

🆕 العميل كان بيستلم روابط متفرّقة (عرض سعر، بروفة) على الواتساب، ومالوش مكان يشوف فيه طلباته
وفواتيره وحسابه — ولا يطلب شغل جديد من غير ما يتصل. زي بوابات shopVOX/Ordant:
* /printing/c/<token>/ — حساب العميل: الرصيد، الطلبات (كل فاتورة بتفتح)، البروفات المستنية رده،
  عروض الأسعار. الرابط سرّي (UUID) وبيتعمل/يتغيّر من صفحة العميل.
* /printing/shop/ — متجر المطبعة: المنتجات اللي عليها «يظهر في المتجر» بأسعارها من الحاسبة ⇒
  الطلب بيتحفظ «عرض سعر» العميل يقبله من رابطه، والموظف يحوّله لطلب طباعة زي أي عرض.
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from erp_core.decorators import tenant_or_404

__all__ = ['customer_portal', 'customer_portal_order', 'customer_portal_link', 'print_shop']

MAX_SHOP_LINES = 10


def _shop_context(request):
    shop = getattr(request, 'tenant', None) or getattr(connection, 'tenant', None)
    logo = ''
    try:
        if getattr(shop, 'logo', None):
            logo = shop.logo.url
    except Exception:
        logo = ''
    from printing.admin import _wa_number
    phone = getattr(shop, 'phone', '') or ''
    return {'shop_name': getattr(shop, 'name', '') or 'Mouss Tec', 'shop_phone': phone,
            'shop_logo': logo, 'shop_wa': _wa_number(phone)}


def _portal_customer(token):
    from printing.models import PrintCustomer
    return get_object_or_404(PrintCustomer, portal_token=token)


@login_required
@tenant_or_404
@require_POST
def customer_portal_link(request, customer_id):
    """زرار «رابط البوابة» في صفحة العميل: يعمل الرابط أول مرة، أو رابط جديد يقفل القديم."""
    from printing.models import PrintCustomer
    if not request.user.has_perm('printing.change_printcustomer'):
        return HttpResponseForbidden(_("لا تملك صلاحية تعديل العملاء."))
    customer = get_object_or_404(PrintCustomer, pk=customer_id)
    regen = request.POST.get('regenerate') == '1'
    customer.get_portal_token(regenerate=regen)
    messages.success(request, _("اتعمل رابط جديد — الرابط القديم مابقاش شغّال.") if regen
                     else _("رابط بوابة العميل جاهز — ابعته له على الواتساب."))
    return redirect(reverse('admin:printing_printcustomer_change', args=[customer.pk]) + '#portal')


@tenant_or_404
def customer_portal(request, token):
    from printing.models import PriceQuotation, PrintOrder, PrintProof
    customer = _portal_customer(token)
    orders = list(customer.printorder_set.exclude(status='cancelled').order_by('-date_created')[:100])
    proofs = [p for p in PrintProof.objects.filter(job__order__customer=customer, status='pending')
              .select_related('job__order').order_by('-created_at')[:20] if p.is_latest]
    quotes = list(PriceQuotation.objects.filter(customer=customer, status__in=('sent', 'accepted'))
                  .order_by('-created_at')[:20])
    summary = customer.account_summary()
    ctx = _shop_context(request)
    ctx.update({
        'customer': customer, 'token': token, 'orders': orders, 'proofs': proofs, 'quotes': quotes,
        'balance': summary['balance'], 'credit': summary.get('credit') or 0,
        'open_orders': sum(1 for o in orders if o.status not in ('delivered',) + PrintOrder.NON_DEBT_STATUSES),
        'has_shop': _public_rules().exists(),
    })
    resp = render(request, 'printing/portal.html', ctx)
    resp['X-Robots-Tag'] = 'noindex, nofollow'
    resp['Referrer-Policy'] = 'no-referrer'
    return resp


@tenant_or_404
def customer_portal_order(request, token, order_id):
    """الفاتورة نفسها (نفس قالب الطباعة) — من غير أزرار الموظفين."""
    from printing.models import PrintOrder
    customer = _portal_customer(token)
    order = get_object_or_404(PrintOrder.objects.select_related('customer', 'branch'), pk=order_id, customer=customer)
    if order.status == 'cancelled':
        raise Http404
    ctx = _shop_context(request)
    shop = getattr(request, 'tenant', None) or getattr(connection, 'tenant', None)
    ctx.update({
        'order': order, 'portal': True, 'portal_url': reverse('printing:customer_portal', args=[token]),
        'jobs': order.jobs.select_related('machine', 'product_type').order_by('pk'),
        'payments': order.printtransaction_set.order_by('date'),
        'ticket': False, 'shop_register': getattr(shop, 'commercial_register', '') or '',
        'printed_at': timezone.localtime(),
    })
    resp = render(request, 'printing/order_invoice.html', ctx)
    resp['X-Robots-Tag'] = 'noindex, nofollow'
    resp['Referrer-Policy'] = 'no-referrer'
    return resp


# =====================================================================
# 🛒 المتجر الأونلاين
# =====================================================================

def _public_rules():
    from printing.models import PrintPriceRule
    return (PrintPriceRule.objects.filter(is_active=True, is_public=True, tiers__isnull=False)
            .distinct().prefetch_related('tiers').order_by('sort_order', 'name'))


def create_shop_request(*, rules_by_id, lines, name, phone, notes, customer=None):
    """[(rule_id, qty)] ⇒ عرض سعر بأسعار الحاسبة (مبعوت — العميل يقبله من رابطه)."""
    from printing.models import PriceQuotation, PrintCustomer, QuotationLine
    clean = []
    for rid, qty in lines:
        if not rid:
            continue
        rule = rules_by_id.get(str(rid))
        if rule is None:
            raise ValidationError(_("المنتج المختار مش متاح."))
        try:
            qty = int(str(qty).strip())
        except (TypeError, ValueError):
            raise ValidationError(_("الكمية لازم تكون رقم صحيح."))
        clean.append((rule, qty, rule.quote(qty)))
    if not clean:
        raise ValidationError(_("اختار منتج واحد على الأقل."))
    if len(clean) > MAX_SHOP_LINES:
        raise ValidationError(_("أقصى عدد بنود %(n)s.") % {'n': MAX_SHOP_LINES})
    if customer is None:
        name, phone = (name or '').strip(), ''.join(ch for ch in (phone or '') if ch.isdigit() or ch == '+')
        if len(name) < 2:
            raise ValidationError(_("اكتب اسمك."))
        if len(phone) < 8:
            raise ValidationError(_("اكتب رقم موبايل صحيح عشان نكلّمك."))
        customer = PrintCustomer.objects.filter(phone=phone).first()
    with transaction.atomic():
        quote = PriceQuotation.objects.create(
            customer=customer,
            customer_name='' if customer else name[:150], customer_phone='' if customer else phone[:20],
            customer_whatsapp='' if customer else phone[:20],
            title=_("طلب من المتجر الأونلاين")[:200],
            notes=("🛒 " + _("طلب أونلاين") + ((":\n" + notes.strip()[:2000]) if (notes or '').strip() else '')),
            status='sent', sent_at=timezone.now(),
        )
        for i, (rule, qty, q) in enumerate(clean):
            QuotationLine.objects.create(quotation=quote, description=f"{rule.name} ({qty} {rule.unit_label})"[:300],
                                         quantity=qty, unit_price=q['unit_price'], sort_order=i)
        quote.recalc_totals()
    return quote


@tenant_or_404
def print_shop(request):
    from print_market.auth import rate_limited
    rules = list(_public_rules())
    if not rules:
        raise Http404("no_public_products")
    rules_by_id = {str(r.pk): r for r in rules}
    customer = None
    token = request.GET.get('c') or request.POST.get('c') or ''
    if token:
        from printing.models import PrintCustomer
        try:
            customer = PrintCustomer.objects.filter(portal_token=token).first()
        except ValidationError:
            customer = None
    errors, form = [], {}
    if request.method == 'POST':
        form = request.POST
        if form.get('website'):          # 🍯 فخ البوتات
            raise Http404
        if rate_limited(request, f'print_shop:{connection.schema_name}', limit=8, window=3600):
            errors = [_("طلبات كتير من نفس الجهاز — جرّب كمان شوية أو كلّمنا واتساب.")]
        else:
            lines = [(r, q) for r, q in zip(form.getlist('rule'), form.getlist('qty')) if r][:MAX_SHOP_LINES + 1]
            try:
                quote = create_shop_request(rules_by_id=rules_by_id, lines=lines, name=form.get('name', ''),
                                            phone=form.get('phone', ''), notes=form.get('notes', ''),
                                            customer=customer)
            except ValidationError as exc:
                errors = exc.messages
            else:
                return redirect(reverse('printing:quotation_public_view', args=[quote.share_token]) + '?new=1')
    ctx = _shop_context(request)
    ctx.update({
        'rules': rules, 'rules_json': json.dumps([r.as_dict() for r in rules], ensure_ascii=False),
        'customer': customer, 'token': token if customer else '', 'errors': errors, 'form': form,
    })
    return render(request, 'printing/shop.html', ctx)
