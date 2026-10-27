"""🧾 قائمة فواتير البيع + المرتجع + التسليم.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal, InvalidOperation
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from inventory.models import Customer, SaleInvoice, Treasury
from inventory.views import _get_branch_for_user, _user_can_edit_branch, tenant_required
from inventory.views.utils import module_required
from ._common import WALK_IN_PHONE, _can_edit_invoices


# =====================================================================
# 5. MODERN LIST VIEWS — replace the Django admin changelist for daily ops
# =====================================================================
@login_required(login_url='/login/')
@tenant_required
@module_required('invoices')
def sale_invoice_list(request):
    branch = _get_branch_for_user(request.user)
    qs = (SaleInvoice.objects
          .select_related("customer", "vehicle", "branch", "original_invoice")
          .order_by("-date_created"))
    if branch is not None:
        qs = qs.filter(branch=branch)

    q = (request.GET.get("q") or "").strip()
    if q:
        # notes: اسم «العميل النقدي» المكتوب من غير رقم بيتحفظ في الملاحظات
        cond = (Q(customer__name__icontains=q) | Q(customer__phone__icontains=q)
                | Q(notes__icontains=q) | Q(original_invoice__notes__icontains=q))
        if q.isdigit():
            cond |= Q(id=int(q))
        qs = qs.filter(cond)

    status = (request.GET.get("status") or "").strip()
    if status:
        qs = qs.filter(status=status)

    inv_type = (request.GET.get("type") or "").strip()
    if inv_type in ("sale", "maintenance"):
        qs = qs.filter(invoice_type=inv_type)

    # 💰 فلتر الحالة المالية (due_amount خاصية مش حقل → نفلتر بـ total/paid)
    #    المتبقّي = الإجمالي − المدفوع − اللي مرتجعاتها خصمته من حساب العميل
    pay = (request.GET.get("pay") or "").strip()
    if pay in ("unpaid", "partial", "paid", "due"):
        money = DecimalField(max_digits=14, decimal_places=2)
        ret_credit = Coalesce(Sum(
            ExpressionWrapper(F("return_invoices__total_amount") - F("return_invoices__paid_amount"),
                              output_field=money),
            filter=Q(return_invoices__status="posted")), Decimal("0"), output_field=money)
        qs = qs.filter(is_return=False).annotate(
            _settled=ExpressionWrapper(F("paid_amount") + ret_credit, output_field=money))
    if pay == "returns":
        qs = qs.filter(is_return=True)
    elif pay == "unpaid":
        qs = qs.filter(paid_amount__lte=0, total_amount__gt=F("_settled"))
    elif pay == "partial":
        qs = qs.filter(paid_amount__gt=0, total_amount__gt=F("_settled"))
    elif pay == "paid":
        qs = qs.filter(_settled__gte=F("total_amount"))
    elif pay == "due":  # عليها متبقّي (آجل) — كامل أو جزئي
        qs = qs.filter(total_amount__gt=F("_settled"))

    page = Paginator(qs.prefetch_related("items__product", "return_invoices"), 25).get_page(request.GET.get("page"))
    # 🧾 ملخّص الأصناف لكل فاتورة (اسم أول قطعة + عدد الباقي) — يظهر في القائمة
    #    عشان يبان محتوى الفاتورة زي الأنظمة العالمية، مش بس المركبة.
    for _inv in page.object_list:
        _its = list(_inv.items.all())
        _inv.first_part = _its[0].product.name if _its else ""
        _inv.extra_parts_count = max(0, len(_its) - 1)
    treasury_qs = Treasury.objects.filter(is_active=True)
    if branch is not None:
        treasury_qs = treasury_qs.filter(branch=branch)
    return render(request, "inventory/sale_invoice_list.html", {
        "page": page,
        "q": q,
        "status": status,
        "inv_type": inv_type,
        "pay": pay,
        "status_choices": SaleInvoice.STATUS_CHOICES,
        "type_choices": SaleInvoice.INVOICE_TYPES,
        "branch": branch,
        "treasuries": treasury_qs.order_by("name"),
        "can_return": _can_process_returns(request.user) and _user_can_edit_branch(request.user, branch),
        "flash_delivered": _flash_int(request.GET.get("delivered")),
        "flash_msg": (request.GET.get("msg") or "")[:200],
        "can_edit": _can_edit_invoices(request.user) and _user_can_edit_branch(request.user, branch),
        "flash_returned": request.GET.get("returned"),
        "flash_ret_id": _flash_int(request.GET.get("ret_id")),
        "flash_refund": _flash_money(request.GET.get("refund")),
        "flash_credit": _flash_money(request.GET.get("credit")),
        "flash_deleted": request.GET.get("deleted"),
        "flash_err": request.GET.get("err"),
        "walk_in_phone": Customer.normalize_phone(WALK_IN_PHONE),
    })


def _flash_int(raw):
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _flash_money(raw):
    try:
        val = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return val if val.is_finite() and val > 0 else None


def _can_process_returns(user):
    if user.is_superuser:
        return True
    prof = getattr(user, 'employee_profile', None)
    if not prof:
        return False
    return (prof.is_manager_or_above or prof.role in ('accountant', 'cashier')
            or prof.can_edit_posted_invoices)


def _get_returnable_invoice(request, pk):
    """يجيب الفاتورة الأصلية القابلة للإرجاع مع فحص الفرع، أو None."""
    branch = _get_branch_for_user(request.user)
    qs = SaleInvoice.objects.select_related('customer', 'branch')
    if branch is not None:
        qs = qs.filter(branch=branch)
    invoice = qs.filter(pk=pk).first()
    if not invoice:
        return None
    if not _user_can_edit_branch(request.user, invoice.branch):
        return None
    return invoice


def _return_unit_estimates(invoice, rows):
    """قيمة الرد التقريبية للقطعة الواحدة في كل سطر — بنفس معادلة المرتجع:
    (السعر − نصيب خصم السطر + تأمين الكور) × (1 − نسبة خصم الفاتورة) × (1 + الضريبة)."""
    items_net, _cost, services, core_total = invoice.billable_breakdown()
    base = (items_net + services + core_total
            + Decimal(str(invoice.labor_cost_manual or 0)))
    inv_disc = Decimal(str(invoice.discount or 0))
    ratio = (inv_disc / base) if base > 0 and inv_disc > 0 else Decimal('0')
    tax = Decimal(str(invoice.tax_percentage or 0)) / Decimal('100')
    for row in rows:
        it = row['item']
        if not it.is_billable:
            row['unit_refund'] = Decimal('0.00')
            continue
        qty = Decimal(str(it.quantity or 1)) or Decimal('1')
        core = Decimal('0') if it.is_core_returned else Decimal(str(it.core_charge_applied or 0))
        unit = Decimal(str(it.unit_price)) - Decimal(str(it.discount or 0)) / qty + core
        row['unit_refund'] = (unit * (1 - ratio) * (1 + tax)).quantize(Decimal('0.01'))
    return rows


@login_required(login_url='/login/')
@tenant_required
@module_required('invoices')
def sale_invoice_return(request, pk):
    """♻️ مرتجع كامل أو جزئي لفاتورة معتمدة — يرجّع المخزون ويرد المبلغ.

    * GET  → شاشة اختيار البنود والكميات المراد إرجاعها.
    * POST → ينشئ المرتجع بالبنود المختارة (أو كل المتبقّي) ثم يعتمده
             (status=posted) فتشتغل execute_sale اللي بتزوّد المخزون
             وتسجّل سحب رد المبلغ من الخزنة المختارة.

    يدعم عدة مرتجعات جزئية على نفس الفاتورة: كل مرة نرجّع بند/كمية،
    والنظام بيمنع تجاوز الكمية الأصلية عبر مجموع المرتجعات.
    """
    from inventory.services.invoice_service import InvoiceService
    from django.core.exceptions import ValidationError

    if not _can_process_returns(request.user):
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=perm")

    invoice = _get_returnable_invoice(request, pk)
    if invoice is None:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=notfound")

    # لا مرتجع لفاتورة مرتجع أو غير معتمدة
    if invoice.is_return or invoice.status != 'posted':
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=cannot")

    returnable = _return_unit_estimates(invoice, InvoiceService.get_returnable_items(invoice))
    total_remaining = sum(r['remaining'] for r in returnable)
    treasuries = Treasury.objects.filter(is_active=True, branch=invoice.branch).order_by('name')
    prior_refunds = (invoice.return_invoices.filter(is_return=True)
                     .aggregate(t=Sum('paid_amount'))['t'] or Decimal('0'))
    refundable_cash = max(Decimal(str(invoice.paid_amount)) - prior_refunds, Decimal('0'))

    def _form(error=None, status=200):
        posted = request.POST if request.method == 'POST' else {}
        for row in returnable:
            row['entered'] = (posted.get(f"qty_{row['item'].pk}") or '').strip() if posted else ''
        return render(request, 'inventory/sale_invoice_return_form.html', {
            'invoice': invoice,
            'rows': returnable,
            'total_remaining': total_remaining,
            'prior_returns': invoice.return_invoices.all().order_by('id'),
            'treasuries': treasuries,
            'selected_treasury': str(posted.get('treasury_id') or invoice.treasury_id or ''),
            'note': (posted.get('note') or '') if posted else '',
            'refundable_cash': refundable_cash,
            'error': error,
        }, status=status)

    # ----- GET: اعرض شاشة الاختيار -----
    if request.method != 'POST':
        return _form()

    # ----- POST: نفّذ المرتجع -----
    if total_remaining <= 0:
        return _form("كل بنود الفاتورة دي مُرتجعة بالكامل — مفيش حاجة تترجع.")

    # اقرأ الكميات المختارة: qty_<item_id> لكل بند. لو دوس "إرجاع الكل"
    # (return_all=1) نبعت return_items=None فيرجّع المتبقّي كله.
    return_all = request.POST.get('return_all') == '1'
    return_items = []
    if not return_all:
        for row in returnable:
            item = row['item']
            raw = (request.POST.get(f'qty_{item.pk}', '') or '').strip()
            if not raw:
                continue
            try:
                qty = int(raw)
            except (TypeError, ValueError):
                return _form(f"«{item.product.name}»: الكمية لازم تكون رقم صحيح.")
            if qty <= 0:
                continue
            if qty > row['remaining']:
                return _form(f"«{item.product.name}»: المتبقّي للإرجاع {row['remaining']} بس.")
            return_items.append({'item_id': item.pk, 'quantity': qty})

        if not return_items:
            return _form("اختار بند واحد على الأقل بكمية أكبر من صفر، أو دوس «إرجاع الكل المتبقّي».")

    treasury = None
    if request.POST.get('treasury_id'):
        treasury = treasuries.filter(pk=request.POST.get('treasury_id')).first()
        if treasury is None:
            return _form("الخزنة المختارة مش موجودة في فرع الفاتورة.")
    note = (request.POST.get('note') or '').strip()[:200]

    try:
        with transaction.atomic():
            ret = InvoiceService.create_return_invoice(
                invoice, return_items=return_items or None, treasury=treasury, note=note,
            )
            refund = Decimal(str(ret.paid_amount or 0))
            if refund > 0:
                # 💵 الرد النقدي بيطلع من خزنة حقيقية — ولو مفيش فيها المبلغ نوقف
                #    بدل ما رصيدها يبقى بالسالب من غير ما حد ياخد باله.
                #    (ValidationError جوه atomic → كل اللي اتكتب بيترجع.)
                if ret.treasury_id is None:
                    raise ValidationError("اختار الخزنة اللي هيترد منها المبلغ للعميل.")
                till = Treasury.objects.select_for_update().get(pk=ret.treasury_id)
                if till.balance < refund:
                    raise ValidationError(
                        f"رصيد خزنة «{till.name}» ({till.balance:.2f}) مش كفاية لرد "
                        f"{refund:.2f} للعميل — اختار خزنة تانية.")
            ret.status = 'posted'
            ret.save()  # يطلق execute_sale → إرجاع المخزون + سحب رد المبلغ
    except ValidationError as exc:
        return _form('؛ '.join(exc.messages))
    except Exception:
        import logging
        logging.getLogger('mouss_tec_core').exception("[RETURN] failed for INV #%s", invoice.pk)
        return _form("حصل خطأ أثناء تنفيذ المرتجع — ماتسجّلش حاجة. جرّب تاني.", status=500)

    credit = max(Decimal(str(ret.total_amount)) - refund, Decimal('0'))
    return redirect(f"{reverse('inventory:sale_invoice_list')}?returned={invoice.id}&ret_id={ret.id}"
                    f"&refund={refund:.2f}&credit={credit:.2f}")


@login_required(login_url='/login/')
@tenant_required
@module_required('invoices')
@require_POST
def sale_invoice_deliver(request, pk):
    """✔ تسليم/إقفال أمر شغل — يحوّله «تم التسليم والاعتماد».

    🐛 [FIX]: أوامر الشغل كانت بتفضل «قيد التنفيذ» للأبد — مفيش في النظام زرار
    يقفلها (غير لوحة Django Admin)، فمكانش ينفع يتعمل عليها مرتجع ولا كانت
    بتظهر في كشف الحساب المطبوع.
    """
    from django.core.exceptions import ValidationError
    list_url = reverse('inventory:sale_invoice_list')
    if not _can_process_returns(request.user):
        return redirect(f"{list_url}?err=perm")
    invoice = _get_returnable_invoice(request, pk)
    if invoice is None:
        return redirect(f"{list_url}?err=notfound")
    if invoice.is_return or invoice.status not in ('in_progress', 'quality_check', 'ready'):
        return redirect(f"{list_url}?err=cannot_deliver")
    try:
        with transaction.atomic():
            locked = SaleInvoice.objects.select_for_update().get(pk=invoice.pk)
            locked.status = 'posted'
            # مطبّق (أمر شغل اتحفظ): المخزون والفلوس اتسجّلوا وقتها، والإشارة
            # بتستحق عمولات الفنيين/البائعين وتحدّث تذكيرات الصيانة بس.
            # مش مطبّق: execute_sale بيسجّل المخزون والدفع والآجل.
            locked.save(update_fields=['status'] if locked.is_applied else None)
    except ValidationError as exc:
        from urllib.parse import quote
        return redirect(f"{list_url}?err=deliver_fail&msg={quote('؛ '.join(exc.messages)[:200])}")
    return redirect(f"{list_url}?delivered={invoice.pk}")
