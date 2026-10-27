"""✏️ صور الفاتورة، تعديل/حذف فواتير البيع وتسوية دفعاتها، التحصيل، اسم العميل النقدي.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal, InvalidOperation
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from inventory.models import (
    Customer,
    EmployeeProfile,
    Inventory,
    InventoryMovement,
    SaleInvoice,
    Treasury,
)
from inventory.views import (
    _get_branch_for_user,
    _json_response_safe,
    _user_can_edit_branch,
    tenant_required,
)
from inventory.views.utils import module_required
from ._common import (
    _RollbackWith,
    _WALK_IN_CREDIT_ERROR,
    _can_edit_invoices,
    _is_walk_in,
    _record_invoice_payments,
)


# =====================================================================
# 📸 صور الفاتورة — صورة القطعة المباعة للمقارنة وقت المرتجع
# =====================================================================
def _get_scoped_invoice(request, pk):
    branch = _get_branch_for_user(request.user)
    qs = SaleInvoice.objects.select_related('customer', 'branch')
    if branch is not None:
        qs = qs.filter(branch=branch)
    return qs.filter(pk=pk).first()


@login_required(login_url='/login/')
@tenant_required
@module_required('invoices')
def sale_invoice_photos(request, pk):
    invoice = _get_scoped_invoice(request, pk)
    if invoice is None:
        return redirect(reverse('inventory:sale_invoice_list') + '?err=notfound')
    return render(request, 'inventory/sale_invoice_photos.html', {
        'invoice': invoice,
        'photos': invoice.photos.all(),
    })


@login_required(login_url='/login/')
@tenant_required
@module_required('invoices')
@require_POST
def sale_invoice_photos_upload(request, pk):
    """رفع صورة أو أكثر لقطع الفاتورة (يدعم الرفع من الـ POS تلقائياً)."""
    from inventory.models import SaleInvoicePhoto
    invoice = _get_scoped_invoice(request, pk)
    if invoice is None:
        wants_json = request.headers.get('X-Requested-With') == 'XMLHttpRequest' \
            or 'application/json' in request.headers.get('Accept', '')
        if wants_json:
            return _json_response_safe({"error": "الفاتورة غير موجودة."}, status=404)
        return redirect(reverse('inventory:sale_invoice_list') + '?err=notfound')
    if not _user_can_edit_branch(request.user, invoice.branch):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return _json_response_safe({"error": "👁 صلاحيتك عرض فقط — مش مسموح بالتعديل."}, status=403)
        return redirect(reverse('inventory:sale_invoice_list') + '?err=perm')
    files = request.FILES.getlist('images') or request.FILES.getlist('photos')
    note = (request.POST.get('note') or '').strip()
    count = 0
    for f in files:
        SaleInvoicePhoto.objects.create(invoice=invoice, image=f, note=note)
        count += 1
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return _json_response_safe({"ok": True, "count": count})
    return redirect(reverse('inventory:sale_invoice_photos', args=[pk]) + '?ok=uploaded')


@login_required(login_url='/login/')
@tenant_required
@module_required('invoices')
@require_POST
def sale_invoice_photo_delete(request, pk, photo_id):
    from inventory.models import SaleInvoicePhoto
    invoice = _get_scoped_invoice(request, pk)
    if invoice is None:
        return redirect(reverse('inventory:sale_invoice_list') + '?err=notfound')
    if not _user_can_edit_branch(request.user, invoice.branch):
        return redirect(reverse('inventory:sale_invoice_list') + '?err=perm')
    SaleInvoicePhoto.objects.filter(pk=photo_id, invoice=invoice).delete()
    return redirect(reverse('inventory:sale_invoice_photos', args=[pk]) + '?ok=deleted')


# =====================================================================
# ✏️🗑️ تعديل / حذف الفواتير + تسوية دفعاتها (خزائن)
# =====================================================================
def _resync_invoice_paid(invoice):
    """يعيد حساب المدفوع = Σ(دفعات in) − Σ(تسويات out) على الفاتورة، ويحفظه.

    ده بيخلّي التسويات (عكس دفعة) والدفعات الجديدة تتجمّع بشكل صحيح من غير
    ما نمسح أي حركة مالية (كل حاجة بتفضل في السجل للمراجعة).
    """
    agg = invoice.payments.aggregate(
        ins=Sum('amount', filter=Q(transaction_type='in')),
        outs=Sum('amount', filter=Q(transaction_type='out')),
    )
    net = (agg['ins'] or Decimal('0')) - (agg['outs'] or Decimal('0'))
    invoice.paid_amount = net if net > 0 else Decimal('0.00')
    last_in = (invoice.payments.filter(transaction_type='in')
               .order_by('-id').first())
    invoice.treasury = last_in.treasury if last_in else None
    invoice.save(update_fields=['paid_amount', 'treasury'])
    return invoice.paid_amount


def _purge_invoice_payments(invoice):
    """يمسح كل دفعات الفاتورة نهائياً ويرجّع أرصدة خزائنها + يشيل قيودها.

    حذف حقيقي (مش حركة تسوية تفضل في السجل) — عشان تعديل/حذف الدفعات يسيب
    السجل نضيف من غير تسويات أو تكرار.
    """
    # 🛡️ رصيد الخزنة بيترجّع تلقائياً عبر signal post_delete — مابنعدّلوش يدوياً،
    #    وقيود كل دفعة بتتشال (أو تتعكس لو فترتها مقفولة) في signal pre_delete.
    for ft in list(invoice.payments.all()):
        ft.delete()


@login_required(login_url='/login/')
@tenant_required
@require_POST
def sale_invoice_delete(request, pk):
    """🗑️ حذف فاتورة بيع مع عكس كل أثرها المالي والمخزوني:
      - يرجّع الكميات للمخزون
      - يسوّي كل الدفعات (يرجّع فلوس الخزائن)
      - يشيل الجزء الآجل من رصيد العميل
      - يعكس عمولة البائع لو اتحسبت
    الحذف للفواتير العادية فقط (مش المرتجعات، ومش فاتورة عليها مرتجع)."""
    if not _can_edit_invoices(request.user):
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=perm")

    branch = _get_branch_for_user(request.user)
    qs = SaleInvoice.objects.select_related('customer', 'branch')
    if branch is not None:
        qs = qs.filter(branch=branch)
    invoice = qs.filter(pk=pk).first()
    if not invoice:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=notfound")
    if not _user_can_edit_branch(request.user, invoice.branch):
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=perm")
    if invoice.is_return:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=cannot_del_return")
    if invoice.return_invoices.exists():
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=has_return")

    try:
        with transaction.atomic():
            from django.db.models import F as _F
            from inventory.models import AccountingEntry, JournalEntry
            from inventory.services.accounting_service import AccountingService
            inv_id = invoice.id
            # 🐛 [FIX]: عرض السعر (مسودة لسه مااتنفذتش) مااتخصمش مخزونه ولا
            #    اتسجّل آجله على العميل — حذفه كان بيرجّع كميات للمخزن مااتصرفتش
            #    (مخزون وهمي) ويخصم من رصيد العميل مديونية مااتسجّلتش (رصيد دائن
            #    وهمي). العكس بيحصل بس للفاتورة اللي اتنفّذت فعلاً.
            applied = invoice.is_applied
            due_before = invoice.due_amount if applied else Decimal('0.00')

            # 1) رجّع المخزون بحركة عكسية واضحة في كارت الصنف
            #    (عمولة البائع على كل سطر بتتعكس مع حذف السطر — signal post_delete)
            #    🐛 [FIX]: كنا بنمسح حركة البيع — فشبكة الأمان (track_movement)
            #    بتلاقي زيادة في الرصيد مالهاش تفسير وتكتب «تعديل يدوي» وهمي،
            #    فكارت الصنف يبان فيه القطعة راجعة مرتين (الرصيد ≠ الحركات).
            #    دلوقتي حركة البيع بتفضل للتاريخ ومعاها حركة إلغاء بنفس الكمية.
            if applied:
                from inventory.services.inventory_service import reaverage_after_restock
                for item in invoice.items.select_related('product').order_by('product_id', 'id'):
                    inv, _created = (Inventory.objects.select_for_update()
                                     .get_or_create(product=item.product, branch=invoice.branch,
                                                    defaults={'quantity': 0}))
                    before = inv.quantity or 0
                    inv.quantity = before + item.quantity
                    inv.save(update_fields=['quantity'])
                    InventoryMovement.objects.create(
                        product=item.product, branch=invoice.branch, reason='adjustment',
                        quantity_change=item.quantity, quantity_before=before,
                        quantity_after=inv.quantity, reference_type='SaleInvoiceDelete',
                        reference_id=inv_id, created_by=request.user,
                        note=f"إلغاء فاتورة بيع #{inv_id} (حذف) — رجوع للمخزن")
                    # 🐛 [FIX]: القطع بترجع بتكلفة بيعها (قيد التكلفة بيتشال بنفس
                    #    القيمة) — المتوسط لازم يتحسب بيها، مش يفضل زي ما هو.
                    reaverage_after_restock(item.product_id, item.quantity,
                                            Decimal(str(item.quantity)) * Decimal(str(item.cost_at_sale or 0)))

                # اعكس عمولات الفنيين اللي اتحسبت على خدمات الفاتورة
                tech_rows = (AccountingEntry.objects
                             .filter(reference__startswith=f"COMM-INV{inv_id}-EMP",
                                     credit__gt=0, journal_entry__isnull=True)
                             .values_list('reference', 'credit'))
                tech_rows = list(tech_rows) + list(
                    JournalEntry.objects.filter(reference__startswith=f"COMM-INV{inv_id}-EMP",
                                                reversal_of__isnull=True)
                    .exclude(status='reversed')
                    .annotate(amt=Sum('lines__credit'))
                    .values_list('reference', 'amt'))
                for ref, amt in tech_rows:
                    emp_part = ref[len(f"COMM-INV{inv_id}-EMP"):].split('-')[0]
                    if emp_part.isdigit() and amt:
                        # قيد العمولة متوازن، فمجموع الدائن = قيمة العمولة
                        EmployeeProfile.objects.filter(pk=int(emp_part)).update(
                            commission_balance=_F('commission_balance') - amt)

            # 2) احذف دفعات الفاتورة نهائياً + امسح قيودها (الرصيد بيترجّع تلقائياً
            #    عبر signal post_delete — مابنعدّلوش يدوياً عشان ما يترجعش مرتين)
            _purge_invoice_payments(invoice)

            # 3) شيل الجزء الآجل من رصيد العميل
            if due_before > Decimal('0.00') and invoice.customer_id:
                Customer.objects.filter(pk=invoice.customer_id).update(
                    balance=_F('balance') - due_before)

            # 4) شيل قيد المبيعات وتسوياته من دفتر الأستاذ (إيراد/تكلفة/مديونية).
            #    🐛 [FIX]: كان بيتمسح حتى لو فترته مقفولة (بيغيّر أرقام اتقفلت)،
            #    وتسويات ما بعد الترحيل كانت بتفضل. دلوقتي: فترة مفتوحة → حذف،
            #    فترة مقفولة → قيد عكسي بتاريخ النهارده.
            AccountingService.unpost(
                JournalEntry.objects.filter(sale_invoice=invoice),
                created_by=request.user, reason=f"حذف فاتورة البيع #{inv_id}")
            AccountingEntry.objects.filter(sale_invoice=invoice, journal_entry__isnull=True).delete()

            # 5) احذف الفاتورة (السطور بتتشال cascade)
            customer_id = invoice.customer_id
            invoice.delete()
            # 🐛 [FIX]: نقاط الولاء اللي اتاخدت على الفاتورة كانت بتفضل مع العميل
            from inventory.services.invoice_service import InvoiceService
            InvoiceService.recalc_loyalty_points([customer_id])
    except Exception:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=del_fail")

    return redirect(f"{reverse('inventory:sale_invoice_list')}?deleted={pk}")


@login_required(login_url='/login/')
@tenant_required
def sale_invoice_edit(request, pk):
    """✏️ تعديل خزائن/دفعات فاتورة بعد إنشائها.

    GET: يعرض الفاتورة ودفعاتها الحالية + خزائن الفرع.
    POST: يسوّي كل الدفعات الحالية ثم يسجّل الدفعات الجديدة (تقسيم كاش/انستا…)،
          ويظبط الجزء الآجل على رصيد العميل. كده تقدر تغيّر الخزنة أو المبلغ
          المدفوع من غير ما تعيد عمل الفاتورة.
    """
    branch = _get_branch_for_user(request.user)
    qs = SaleInvoice.objects.select_related('customer', 'branch', 'treasury')
    if branch is not None:
        qs = qs.filter(branch=branch)
    invoice = qs.filter(pk=pk).first()
    if not invoice:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=notfound")
    if not (_can_edit_invoices(request.user) and _user_can_edit_branch(request.user, invoice.branch)):
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=perm")
    if invoice.is_return:
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=cannot")
    # 🛡️ عرض السعر مالوش آجل على العميل لسه — تسجيل دفعات عليه كان بيخصم من
    #    رصيد العميل مديونية مااتسجّلتش أصلاً. التحصيل بعد التنفيذ/الاعتماد.
    if not invoice.is_applied:
        if request.method == 'POST':
            return _json_response_safe(
                {"error": "الفاتورة لسه عرض سعر — اعتمدها الأول قبل تسجيل الدفعات."}, status=400)
        return redirect(f"{reverse('inventory:sale_invoice_list')}?err=not_applied")

    treasuries = Treasury.objects.filter(is_active=True, branch=invoice.branch).order_by('name')

    if request.method == 'POST':
        import json as _json
        try:
            payload = _json.loads(request.body or b"{}")
        except ValueError:
            return _json_response_safe({"error": "بيانات غير صالحة."}, status=400)
        raw_payments = payload.get("payments") or []
        tenders = []
        for p in raw_payments:
            try:
                amt = Decimal(str(p.get("amount")))
            except (InvalidOperation, TypeError):
                continue
            tid = p.get("treasury_id")
            if tid and amt > 0:
                tenders.append((tid, amt))
        try:
            with transaction.atomic():
                due_before = invoice.due_amount
                # 1) امسح الدفعات القديمة نهائياً (رجّع الخزائن) — من غير تسويات
                _purge_invoice_payments(invoice)
                # 2) سجّل الدفعات الجديدة
                if tenders:
                    _record_invoice_payments(invoice, tenders, request.user)
                # 3) أعِد حساب المدفوع من كل الحركات
                _resync_invoice_paid(invoice)
                invoice.refresh_from_db()
                # 4) ظبط الآجل على رصيد العميل (الفرق بين الآجل القديم والجديد)
                due_after = invoice.due_amount
                delta = due_after - due_before
                # 🐛 [FIX]: الكاشير بيرفض الآجل على «عميل نقدي»، بس هنا كان ممكن
                #    تشيل دفعات فاتورة عميل نقدي فتتحوّل آجل — مديونية محدش يعرف صاحبها.
                if delta > 0 and _is_walk_in(invoice.customer):
                    raise _RollbackWith(_json_response_safe(
                        {"error": _WALK_IN_CREDIT_ERROR}, status=400))
                if delta != 0 and invoice.customer_id:
                    from django.db.models import F as _F
                    Customer.objects.filter(pk=invoice.customer_id).update(
                        balance=_F('balance') + delta)
            return _json_response_safe({
                "ok": True, "invoice_id": invoice.id,
                "total": float(invoice.total_amount),
                "paid": float(invoice.paid_amount),
                "due": float(invoice.due_amount),
            })
        except _RollbackWith as rb:
            return rb.response
        except Exception as exc:  # noqa: BLE001
            return _json_response_safe({"error": f"فشل التعديل: {exc}"}, status=500)

    current = list(invoice.payments.filter(transaction_type='in')
                   .select_related('treasury').order_by('id'))
    return render(request, "inventory/sale_invoice_edit.html", {
        "invoice": invoice,
        "treasuries": treasuries,
        "current_payments": current,
    })


def _pre_tax_discount_for(inv, cut):
    """خصم الفاتورة (قبل الضريبة) اللي بيقلّل إجماليها بـ cut بالظبط (لأقرب قرش).

    إجمالي الفاتورة = (الأصناف + الخدمات + المصنعية − الخصم) + ضريبة على الناتج
    (SaleInvoice.update_total)، فخصم X قبل الضريبة بيشيل X × (١ + النسبة).
    """
    from decimal import ROUND_HALF_UP
    rate = Decimal(str(inv.tax_percentage or 0))
    if rate <= 0:
        return cut
    items, _cost, services, core = inv.billable_breakdown()
    base = (items + services + core + Decimal(str(inv.labor_cost_manual or 0))
            - Decimal(str(inv.discount or 0)))

    def total(sub):
        return sub + (sub * rate / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    target = total(base) - cut
    guess = (cut * Decimal('100') / (Decimal('100') + rate)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    cent = Decimal('0.01')
    return min((guess + k * cent for k in (-1, 0, 1)),
               key=lambda d: (abs(total(base - d) - target), d))


@login_required(login_url='/login/')
@tenant_required
@require_POST
def sale_invoice_pay(request, pk):
    """💵 تحصيل سريع للمتبقّي على فاتورة (دفعة إضافية) و/أو خصم على المتبقّي.

    JSON: {treasury_id, amount, discount}. بيضيف دفعة (مش بيمسح القديم)،
    و/أو يزوّد خصم الفاتورة، ويظبط آجل العميل بفرق المتبقّي. تحصيل سريع من
    قائمة الفواتير من غير ما تفتح الفاتورة.
    """
    import json as _json

    branch = _get_branch_for_user(request.user)
    qs = SaleInvoice.objects.select_related('customer', 'branch')
    if branch is not None:
        qs = qs.filter(branch=branch)
    invoice = qs.filter(pk=pk).first()
    if not invoice:
        return _json_response_safe({"error": "الفاتورة غير موجودة."}, status=404)
    if not (_can_edit_invoices(request.user) and _user_can_edit_branch(request.user, invoice.branch)):
        return _json_response_safe({"error": "صلاحيتك عرض فقط — مش مسموح بالتحصيل."}, status=403)
    if invoice.is_return:
        return _json_response_safe({"error": "لا يمكن التحصيل على فاتورة مرتجع."}, status=400)
    if not invoice.is_applied:
        return _json_response_safe(
            {"error": "الفاتورة لسه عرض سعر — اعتمدها الأول قبل التحصيل."}, status=400)

    try:
        payload = _json.loads(request.body or b"{}")
    except ValueError:
        return _json_response_safe({"error": "بيانات غير صالحة."}, status=400)

    def _dec(v):
        try:
            d = Decimal(str(v))
            return d if d > 0 else Decimal('0')
        except (InvalidOperation, TypeError):
            return Decimal('0')

    amount = _dec(payload.get('amount'))
    discount = _dec(payload.get('discount'))
    treasury_id = payload.get('treasury_id')

    if amount <= 0 and discount <= 0:
        return _json_response_safe({"error": "أدخل مبلغ تحصيل أو خصم."}, status=400)
    if amount > 0 and not treasury_id:
        return _json_response_safe({"error": "اختر الخزنة لتسجيل التحصيل."}, status=400)

    try:
        with transaction.atomic():
            inv = SaleInvoice.objects.select_for_update().get(pk=invoice.pk)
            due_before = inv.due_amount
            # المتبقّي الفعلي بعد مرتجعات الفاتورة — ماينفعش نحصّل اللي المرتجع شاله
            ret_credit = inv.returned_credit

            # 1) خصم على المتبقّي (لا يتجاوز المتبقّي)
            if discount > 0:
                discount = min(discount, max(due_before - ret_credit, Decimal('0')))
                # 🐛 [FIX]: الخصم كان بيتضاف لخصم الفاتورة (قبل الضريبة) زي ما هو، فعلى
                #    فاتورة عليها ضريبة ١٤٪ «خصم ١٣» كان بيشيل 14.82 من المتبقّي — والتحصيل
                #    بعده بيتقصّ على المتبقّي الجديد، فالكاشير ياخد الفلوس كاملة والخزنة
                #    تتسجّل أقل منها. دلوقتي المتبقّي بيقل بالمبلغ المكتوب بالظبط.
                inv.discount = (inv.discount or Decimal('0')) + _pre_tax_discount_for(inv, discount)
                # 🐛 [FIX]: update_total بيحفظ الإجماليات بس — الخصم كان بيتحسب
                #    في الإجمالي ومايتحفظش، فأي إعادة حساب بعد كده (مرتجع/كور/
                #    مراجعة) بترجّع الإجمالي القديم والدفتر يرجع المديونية.
                SaleInvoice.objects.filter(pk=inv.pk).update(discount=inv.discount)
                inv.update_total()
                inv.refresh_from_db()

            # 2) تحصيل دفعة (لا تتجاوز المتبقّي بعد الخصم والمرتجع)
            remaining = max(inv.due_amount - ret_credit, Decimal('0'))
            if amount > 0 and remaining > 0:
                amount = min(amount, remaining)
                _record_invoice_payments(inv, [(treasury_id, amount)], request.user)
                _resync_invoice_paid(inv)
                inv.refresh_from_db()

            # 3) ظبط آجل العميل بفرق المتبقّي
            due_after = inv.due_amount
            delta = due_after - due_before
            if delta != 0 and inv.customer_id:
                from django.db.models import F as _F
                Customer.objects.filter(pk=inv.customer_id).update(
                    balance=_F('balance') + delta)

        return _json_response_safe({
            "ok": True, "invoice_id": inv.id,
            "total": float(inv.total_amount),
            "paid": float(inv.paid_amount),
            "due": float(inv.due_amount),
        })
    except Exception as exc:  # noqa: BLE001
        return _json_response_safe({"error": f"فشل التحصيل: {exc}"}, status=500)


@login_required(login_url='/login/')
@tenant_required
@require_POST
def sale_invoice_set_name(request, pk):
    """✏️ اسم العميل على فاتورة «عميل نقدي» (JSON: {name}) — اسم فاضي بيمسحه.

    🐛 [FIX]: قبل ١١ أكتوبر الاسم المكتوب من غير رقم كان بيضيع خالص، فالفواتير
    القديمة دي بتظهر «عميل نقدي». ده بيخلّي الاسم يتكتب عليها بعد كده. مجرد اسم
    للعرض — مفيش أي أثر على الخزائن أو رصيد العملاء (العميل النقدي بيفضل هو هو).
    """
    import json as _json

    branch = _get_branch_for_user(request.user)
    qs = SaleInvoice.objects.select_related('customer', 'branch')
    if branch is not None:
        qs = qs.filter(branch=branch)
    invoice = qs.filter(pk=pk).first()
    if not invoice:
        return _json_response_safe({"error": "الفاتورة غير موجودة."}, status=404)
    if not (_can_edit_invoices(request.user) and _user_can_edit_branch(request.user, invoice.branch)):
        return _json_response_safe({"error": "صلاحيتك عرض فقط — مش مسموح بالتعديل."}, status=403)
    if not _is_walk_in(invoice.customer):
        return _json_response_safe(
            {"error": "الفاتورة دي على عميل مسجّل — اسمه بيتعدّل من صفحة العميل."}, status=400)
    try:
        payload = _json.loads(request.body or b"{}")
    except ValueError:
        return _json_response_safe({"error": "بيانات غير صالحة."}, status=400)
    if not isinstance(payload, dict):
        return _json_response_safe({"error": "بيانات غير صالحة."}, status=400)

    invoice.set_walk_in_name(str(payload.get("name") or ""))
    # .update مش save: الاسم للعرض بس — مانعيدش تشغيل إشارات الاعتماد/العمولات.
    SaleInvoice.objects.filter(pk=invoice.pk).update(notes=invoice.notes)
    return _json_response_safe({"ok": True, "invoice_id": invoice.id,
                                "name": invoice.customer_display_name})
