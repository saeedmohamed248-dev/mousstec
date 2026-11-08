"""⚡ شاشات الإدخال السريع: تحصيل من عميل، فاتورة شراء خامات، إضافة عميل (printing/quick_entry.py).

كل شاشة: GET يعرض الفورم، POST يحفظ ويرجّع نفس الصفحة بحالة «اتحفظ» + الخطوة الجاية،
والغلط بيرجّع الفورم بكل اللي اتكتب ورسالة واضحة (مفيش حاجة بتتحفظ نصّها).
"""
from decimal import Decimal

from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import models
from django.http import HttpResponseForbidden
from django.shortcuts import render
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _

from erp_core.decorators import tenant_or_404

__all__ = ['quick_collect', 'quick_purchase', 'quick_customer', 'quick_expense', 'quick_pay_supplier',
           'party_import']


def _ctx(request, **extra):
    ctx = admin.site.each_context(request)
    ctx.update(extra)
    return ctx


def _show_balance(user):
    """رصيد الخزن بيظهر بس للي صلاحياته بتسمح يشوف الخزنة (زي باقي السيستم)."""
    from printing.views.finance import _can_see_money
    return _can_see_money(user, 'can_view_treasury')


def _money_treasuries():
    from printing.models import PrintTreasury
    return list(PrintTreasury.objects.filter(is_active=True, kind__in=PrintTreasury.MONEY_KINDS).order_by('pk'))


def _pick(items, pk):
    return next((t for t in items if str(t.pk) == str(pk or '')), None)


# ─────────────────────────────────────────────────────────────────────
# 💵 تحصيل من عميل
# ─────────────────────────────────────────────────────────────────────
@login_required
@tenant_or_404
def quick_collect(request):
    from printing.models import PrintCustomer, PrintOrder
    from printing.quick_entry import collect_from_customer, open_orders

    if not request.user.has_perm('printing.add_printtransaction'):
        return HttpResponseForbidden(_("لا تملك صلاحية تسجيل تحصيل."))
    treasuries = _money_treasuries()
    data = request.POST if request.method == 'POST' else request.GET
    customer = None
    if data.get('customer'):
        customer = PrintCustomer.objects.filter(pk=data.get('customer') if str(data.get('customer')).isdigit() else None).first()

    errors, done = [], None
    if request.method == 'POST' and customer is not None:
        try:
            txn = collect_from_customer(
                user=request.user, customer_id=customer.pk, amount=data.get('amount'),
                treasury=_pick(treasuries, data.get('treasury')),
                order_id=data.get('order') if data.get('order') not in (None, '', 'account') else None,
                withholding_tax=data.get('withholding_tax') or '0', description=data.get('description', ''))
        except ValidationError as exc:
            errors = exc.messages
        else:
            done = {'txn': txn, 'balance': customer.account_summary()['balance']}
    elif request.method == 'POST':
        errors = [_("اختار العميل.")]

    ctx = {'title': _("💵 تحصيل من عميل"), 'treasuries': treasuries, 'errors': errors, 'done': done,
           'form': data if errors else {}, 'customer': customer}
    if customer is not None and not done:
        ctx['orders'] = open_orders(customer)
        ctx['balance'] = customer.account_summary()['balance']
        wanted = data.get('order')
        ctx['selected_order'] = wanted if wanted else (str(ctx['orders'][0].pk) if ctx['orders'] else 'account')
    elif customer is None:
        # العملاء اللي عليهم فلوس الأول (استعلام واحد على الفواتير المفتوحة)
        due = (PrintOrder.objects.exclude(status__in=PrintOrder.NON_DEBT_STATUSES)
               .annotate(_due=models.F('total_amount') - models.F('discount') + models.F('tax_amount')
                         - models.F('credited_amount') - models.F('paid_amount'))
               .filter(_due__gt=0).values('customer').annotate(total=models.Sum('_due'), n=models.Count('pk')))
        owed = {r['customer']: r for r in due}
        rows = list(PrintCustomer.objects.order_by('name').values('pk', 'name', 'phone')[:1000])
        for r in rows:
            o = owed.get(r['pk'])
            r['owed'], r['n'] = (o['total'], o['n']) if o else (Decimal('0'), 0)
        rows.sort(key=lambda r: -r['owed'])
        ctx['customers'] = rows
    return render(request, 'printing/quick_collect.html', _ctx(request, **ctx))


# ─────────────────────────────────────────────────────────────────────
# 🛒 فاتورة شراء خامات
# ─────────────────────────────────────────────────────────────────────
@login_required
@tenant_or_404
def quick_purchase(request):
    from printing.models import PrintMaterial, PrintSupplier
    from printing.quick_entry import MAX_LINES, create_quick_purchase

    if not request.user.has_perm('printing.add_materialpurchase'):
        return HttpResponseForbidden(_("لا تملك صلاحية تسجيل فواتير شراء."))
    treasuries = _money_treasuries()
    errors, done, warnings, form = [], None, [], {}
    if request.method == 'POST':
        P = form = request.POST
        lines = list(zip(P.getlist('line_material'), P.getlist('line_name'), P.getlist('line_unit'),
                         P.getlist('line_qty'), P.getlist('line_cost')))[:MAX_LINES + 1]
        terms = P.get('payment_terms') or 'cash'
        try:
            purchase, warnings = create_quick_purchase(
                user=request.user, lines=lines, supplier_id=P.get('supplier_id') or None,
                supplier_name=P.get('supplier_name', ''), supplier_phone=P.get('supplier_phone', ''),
                payment_terms=terms, treasury=_pick(treasuries, P.get('treasury')) if terms == 'cash' else None,
                receive=P.get('receive') != '0', discount=P.get('discount') or '0', tax=P.get('tax') or '0',
                supplier_invoice_no=P.get('supplier_invoice_no', ''), notes=P.get('notes', ''))
        except ValidationError as exc:
            errors = exc.messages
        else:
            done = purchase
            form = {}
    materials = list(PrintMaterial.objects.order_by('name').values('pk', 'name', 'unit', 'cost_per_unit', 'quantity')[:1000])
    suppliers = list(PrintSupplier.objects.filter(is_active=True).order_by('name').values('pk', 'name', 'phone')[:1000])
    rows = (list(zip(form.getlist('line_material'), form.getlist('line_name'), form.getlist('line_unit'),
                     form.getlist('line_qty'), form.getlist('line_cost'))) if errors else [('', '', '', '', '')])
    return render(request, 'printing/quick_purchase.html', _ctx(
        request, title=_("🛒 فاتورة شراء خامات"), treasuries=treasuries, materials=materials,
        show_balance=_show_balance(request.user),
        suppliers=suppliers, errors=errors, done=done, warnings=warnings, form=form, lines=rows))


# ─────────────────────────────────────────────────────────────────────
# 👤 إضافة عميل
# ─────────────────────────────────────────────────────────────────────
@login_required
@tenant_or_404
def quick_customer(request):
    from printing.quick_entry import create_quick_customer

    if not request.user.has_perm('printing.add_printcustomer'):
        return HttpResponseForbidden(_("لا تملك صلاحية إضافة عملاء."))
    errors, done, form = [], None, {}
    if request.method == 'POST':
        P = form = request.POST
        has_opening = P.get('opening') in ('owes', 'credit')
        try:
            if has_opening and not (P.get('opening_amount') or '').strip().strip('0.'):
                raise ValidationError(_("اكتب المبلغ اللي من قبل السيستم (أو اختار «لأ»)."))
            done = create_quick_customer(
                name=P.get('name', ''), phone=P.get('phone', ''), whatsapp=P.get('whatsapp', ''),
                company=P.get('company', ''), notes=P.get('notes', ''),
                opening_balance=(P.get('opening_amount') or '0') if has_opening else '0',
                opening_direction=P.get('opening') if has_opening else 'owes',
                opening_date=parse_date(P.get('opening_date') or '') if has_opening else None,
                credit_limit=P.get('credit_limit') or '0')
        except ValidationError as exc:
            errors = exc.messages
        else:
            form = {}
    return render(request, 'printing/quick_customer.html', _ctx(
        request, title=_("👤 عميل جديد"), errors=errors, done=done, form=form))


# ─────────────────────────────────────────────────────────────────────
# 💸 مصروف
# ─────────────────────────────────────────────────────────────────────
@login_required
@tenant_or_404
def quick_expense(request):
    from printing.models import PrintTransaction
    from printing.quick_entry import EXPENSE_CATEGORIES, record_expense

    if not request.user.has_perm('printing.add_printtransaction'):
        return HttpResponseForbidden(_("لا تملك صلاحية تسجيل مصروفات."))
    treasuries = _money_treasuries()
    errors, done, form = [], None, {}
    if request.method == 'POST':
        P = form = request.POST
        try:
            done = record_expense(user=request.user, amount=P.get('amount'), category=P.get('category', ''),
                                  treasury=_pick(treasuries, P.get('treasury')), description=P.get('description', ''))
        except ValidationError as exc:
            errors = exc.messages
        else:
            form = {}
            treasuries = _money_treasuries()        # الرصيد بعد الصرف
    labels = dict(PrintTransaction.CATEGORY_CHOICES)
    icons = {'rent': '🏠', 'utilities': '💡', 'maintenance': '🔧', 'shipping': '🚚', 'marketing': '📣',
             'salaries': '👷', 'materials': '📦', 'other_expense': '🧾'}
    categories = [(c, icons.get(c, '🧾'), labels.get(c, c)) for c in EXPENSE_CATEGORIES]
    return render(request, 'printing/quick_expense.html', _ctx(
        request, title=_("💸 مصروف"), treasuries=treasuries, categories=categories,
        show_balance=_show_balance(request.user),
        errors=errors, done=done, form=form))


# ─────────────────────────────────────────────────────────────────────
# 🏭 سداد لمورد
# ─────────────────────────────────────────────────────────────────────
@login_required
@tenant_or_404
def quick_pay_supplier(request):
    from printing.models import PrintSupplier
    from printing.quick_entry import open_supplier_docs, pay_supplier

    if not request.user.has_perm('printing.add_printtransaction'):
        return HttpResponseForbidden(_("لا تملك صلاحية تسجيل سداد."))
    treasuries = _money_treasuries()
    data = request.POST if request.method == 'POST' else request.GET
    sid = str(data.get('supplier', ''))
    supplier = PrintSupplier.objects.filter(pk=sid).first() if sid.isdigit() else None

    errors, done = [], None
    if request.method == 'POST' and supplier is not None:
        try:
            txn = pay_supplier(user=request.user, supplier_id=supplier.pk, amount=data.get('amount'),
                               treasury=_pick(treasuries, data.get('treasury')), doc=data.get('doc', ''),
                               withholding_tax=data.get('withholding_tax') or '0',
                               description=data.get('description', ''))
        except ValidationError as exc:
            errors = exc.messages
        else:
            done = {'txn': txn, 'balance': supplier.account_summary()['balance']}
    elif request.method == 'POST':
        errors = [_("اختار المورد.")]

    ctx = {'title': _("🏭 سداد لمورد"), 'treasuries': treasuries, 'errors': errors, 'done': done,
           'show_balance': _show_balance(request.user),
           'form': data if errors else {}, 'supplier': supplier}
    if supplier is not None and not done:
        ctx['docs'] = [{'key': f'{kind}:{d.pk}', 'kind': kind, 'd': d} for kind, d in open_supplier_docs(supplier)]
        ctx['balance'] = supplier.account_summary()['balance']
        ctx['selected_doc'] = data.get('doc') or (ctx['docs'][0]['key'] if ctx['docs'] else 'account')
    elif supplier is None:
        rows = []
        for s in PrintSupplier.objects.filter(is_active=True).order_by('name')[:1000]:
            rows.append({'pk': s.pk, 'name': s.name, 'phone': s.phone, 'owed': s.account_summary()['balance']})
        rows.sort(key=lambda r: -r['owed'])
        ctx['suppliers'] = rows
    return render(request, 'printing/quick_pay_supplier.html', _ctx(request, **ctx))


# ─────────────────────────────────────────────────────────────────────
# 👥 استيراد عملاء/موردين بأرصدتهم الافتتاحية (printing/party_import.py)
# ─────────────────────────────────────────────────────────────────────
_PARTY_TEMPLATE_ROWS = {
    'customer': [['شركة النور للدعاية', '01012345678', '3500', 'النور'], ['أحمد علي', '01198765432', '-200', '']],
    'supplier': [['مخازن الورق الحديثة', '01022223333', '12000', ''], ['أحبار الدلتا', '', '4500', '']],
}


@login_required
@tenant_or_404
def party_import(request):
    import csv

    from django.http import HttpResponse

    from printing.party_import import HEADERS, import_print_parties

    kind = request.POST.get('kind') or request.GET.get('kind') or 'customer'
    if kind not in ('customer', 'supplier'):
        kind = 'customer'
    perm = 'printing.add_printcustomer' if kind == 'customer' else 'printing.add_printsupplier'
    if not request.user.has_perm(perm):
        return HttpResponseForbidden(_("لا تملك صلاحية إضافة عملاء أو موردين."))
    ctx = {'kind': kind}
    if request.method == 'GET' and request.GET.get('template'):
        resp = HttpResponse(content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="mousstec_print_{kind}s_template.csv"'
        resp.write('\ufeff')   # BOM عشان العربي يظهر صح في Excel
        w = csv.writer(resp)
        w.writerow(HEADERS)
        for r in _PARTY_TEMPLATE_ROWS[kind]:
            w.writerow(r)
        return resp
    if request.method == 'POST':
        up = request.FILES.get('file')
        if not up:
            ctx['error'] = _("اختر ملف Excel أو CSV أولاً.")
        else:
            from inventory.views_lightning.party_import import _load_dataset
            try:
                ds = _load_dataset(up)
            except Exception:  # noqa: BLE001
                ctx['error'] = _("تعذّر قراءة الملف. المدعوم: Excel (.xlsx/.xls)، CSV، ODS.")
            else:
                try:
                    ctx['result'] = import_print_parties(kind, ds)
                except ValueError as e:
                    ctx['error'] = str(e)
    return render(request, 'printing/party_import.html', _ctx(
        request, title=_("👥 استيراد العملاء والموردين بأرصدتهم"), **ctx))
