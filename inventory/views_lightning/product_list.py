"""📋 قائمة الأصناف: البحث والفلاتر، التصدير، الإجراءات الجماعية، مزامنة FixIt.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
from decimal import Decimal
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, DecimalField, ExpressionWrapper, F, IntegerField, Q, Sum, When
from django.db.models.functions import Coalesce
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from inventory.models import EmployeeProfile, Inventory, InventoryMovement, Product, SaleInvoiceItem
from inventory.views import _get_branch_for_user, tenant_required
from inventory.views.utils import module_required, role_required
from inventory.report_export import export_report
from ._common import _apply_product_search, _can_bulk_delete_products, _product_price_visibility


def _product_list_qs(request, branch):
    """قائمة الأصناف بنفس فلاتر الصفحة (بحث/حالة المخزون/المؤرشفة) + _stock.

    مشتركة بين العرض والتصدير والإجراءات الجماعية عشان «كل النتائج» تبقى
    نفس اللي المستخدم شايفه بالظبط.
    """
    from django.db.models import F
    stock_filter = (request.GET.get("stock") or request.POST.get("stock") or "").strip()
    qs = Product.objects.filter(is_active=(stock_filter != "archived"))
    if branch is not None:
        from django.db.models import Exists, OuterRef
        qs = qs.filter(Exists(
            Inventory.objects.filter(product=OuterRef("pk"), branch=branch)))
    q = (request.GET.get("q") or request.POST.get("q") or "").strip()
    if q:
        qs = _apply_product_search(qs, q)
    _stock_sum = Sum("inventory__quantity",
                     filter=Q(inventory__branch=branch) if branch is not None else None)
    qs = qs.annotate(_stock=Coalesce(_stock_sum, 0, output_field=IntegerField()))
    if stock_filter == "available":
        qs = qs.filter(_stock__gt=0)
    elif stock_filter == "out":
        qs = qs.filter(_stock__lte=0)
    elif stock_filter == "low":
        # نفس كارت «قرب يخلص» — اللي خلص خالص له فلتر «نفد» لوحده
        qs = qs.filter(_stock__gt=0, _stock__lte=F("min_stock_level"))
    if not q:
        qs = qs.order_by("name")
    return qs, q, stock_filter


def _effective_unit_cost(p):
    """تكلفة الوحدة الفعلية: متوسط التكلفة (شامل الشحن/الجمارك) — ولو صفر سعر الشراء."""
    avg = Decimal(str(p.average_cost or 0))
    return avg if avg > 0 else Decimal(str(p.purchase_price or 0))


def _can_import_products(user):
    if user.is_superuser:
        return True
    prof = getattr(user, 'employee_profile', None)
    if prof is None:
        return False
    return EmployeeProfile.canonical_role(prof.role) in ('admin', 'manager', 'stock')


def _selected_products(request, branch):
    """الأصناف المختارة: ids=1,2,3 — أو all=1 = كل نتائج الفلتر الحالي."""
    data = request.POST if request.method == 'POST' else request.GET
    qs, _q, _sf = _product_list_qs(request, branch)
    if (data.get('all') or '') == '1':
        return qs
    ids = {int(x) for x in str(data.get('ids') or '').split(',') if x.strip().isdigit()}
    return qs.filter(pk__in=ids) if ids else qs.none()


# أعمدة التصدير: key → (العنوان, نوع السعر اللي محتاج صلاحية أو None)
_PRODUCT_EXPORT_COLS = [
    ('sku', 'رقم القطعة (SKU)', None),
    ('barcode', 'الباركود', None),
    ('name', 'الاسم', None),
    ('brand', 'الماركة', None),
    ('car_model', 'الموديل', None),
    ('category', 'التصنيف', None),
    ('condition', 'الحالة', None),
    ('stock', 'المخزون', None),
    ('min_stock', 'حد التنبيه', None),
    ('branches', 'التوزيع على الفروع', None),
    ('purchase_price', 'سعر الشراء', 'cost'),
    ('average_cost', 'متوسط التكلفة', 'cost'),
    ('retail_price', 'سعر البيع (قطاعي)', 'sale'),
    ('wholesale_price', 'سعر الجملة', 'sale'),
    ('value_cost', 'قيمة المخزون (تكلفة)', 'cost'),
    ('value_retail', 'قيمة المخزون (بيع)', 'sale'),
    ('value_wholesale', 'قيمة المخزون (جملة)', 'sale'),
]


_PRODUCT_EXPORT_DEFAULT = ('sku', 'name', 'brand', 'car_model', 'stock', 'retail_price')


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
def product_export(request):
    """📤 تصدير المخزون (Excel/PDF) — المحدد أو كل النتائج، بالأعمدة اللي يختارها المستخدم.

    أعمدة الأسعار بتتحترم صلاحية الموظف (اللي مايشوفش التكلفة مايصدّرهاش).
    """
    branch = _get_branch_for_user(request.user)
    show_cost, show_sale = _product_price_visibility(request, branch)
    allowed = {k for k, _l, need in _PRODUCT_EXPORT_COLS
               if need is None or (need == 'cost' and show_cost) or (need == 'sale' and show_sale)}
    wanted = [c for c in str(request.GET.get('cols') or '').split(',') if c in allowed]
    if not wanted:
        wanted = [c for c in _PRODUCT_EXPORT_DEFAULT if c in allowed]
    cols = [(k, label) for k, label, _n in _PRODUCT_EXPORT_COLS if k in wanted]

    products = (_selected_products(request, branch)
                .prefetch_related('inventory_set__branch'))[:20000]
    cat_label = dict(Product.PART_CATEGORY_CHOICES)
    cond_label = dict(Product.CONDITION_CHOICES)
    rows, sums = [], {'stock': 0, 'value_cost': Decimal('0'), 'value_retail': Decimal('0'),
                      'value_wholesale': Decimal('0')}
    for p in products:
        inv_rows = [r for r in p.inventory_set.all() if branch is None or r.branch_id == branch.id]
        stock = sum(r.quantity for r in inv_rows)
        cost = Decimal(str(p.purchase_price or 0))
        retail = Decimal(str(p.retail_price or 0))
        vals = {
            'sku': p.part_number, 'barcode': p.barcode or '', 'name': p.name,
            'brand': p.brand or '', 'car_model': p.car_model or '',
            'category': str(cat_label.get(p.part_category, '') or ''),
            'condition': str(cond_label.get(p.condition, '') or ''),
            'stock': stock, 'min_stock': p.min_stock_level,
            'branches': '، '.join(f"{r.branch.name}: {r.quantity}" for r in inv_rows if r.quantity),
            'purchase_price': cost, 'average_cost': Decimal(str(p.average_cost or 0)),
            'retail_price': retail, 'wholesale_price': Decimal(str(p.b2b_wholesale_price or 0)),
            'value_cost': _effective_unit_cost(p) * stock, 'value_retail': retail * stock,
            'value_wholesale': Decimal(str(p.b2b_wholesale_price or 0)) * stock,
        }
        sums['stock'] += stock
        sums['value_cost'] += vals['value_cost']
        sums['value_retail'] += vals['value_retail']
        sums['value_wholesale'] += vals['value_wholesale']
        rows.append([vals[k] for k, _l in cols])
    total = [sums.get(k, '') for k, _l in cols]
    label_at = next((i for i, (k, _l) in enumerate(cols) if k not in sums), None)
    if label_at is not None:
        total[label_at] = 'الإجمالي'
    fmt = (request.GET.get('fmt') or 'xlsx').strip().lower()
    if fmt not in ('xlsx', 'pdf'):
        fmt = 'xlsx'
    request.GET = request.GET.copy()
    request.GET['export'] = fmt
    from django.utils import timezone as _tz
    resp = export_report(
        request, f"inventory_{_tz.localdate():%Y%m%d}", "تقرير المخزون",
        f"{branch.name if branch else 'كل الفروع'} · {len(rows)} صنف",
        [{"columns": [label for _k, label in cols], "rows": rows, "total": total}])
    return resp


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
@require_POST
def product_bulk_action(request):
    """🗑️ حذف/أرشفة أو استرجاع أصناف محددة (أو كل نتائج الفلتر).

    حذف صنف فعلياً كان هيمسح معاه بنود فواتير البيع/الشراء وحركات المخزون
    (CASCADE) — فالحذف هنا آمن:
      • صنف عليه رصيد في أي فرع → بيتساب (يتصفّر رصيده الأول بجرد/مرتجع).
      • صنف ليه تاريخ (فواتير/حركات/تحويلات) → يتأرشف (يختفي من القوائم
        والبحث، وتاريخه محفوظ، ويرجع لوحده لو اتشترى تاني).
      • صنف ملوش أي تاريخ (اتعمل غلط) → يتحذف نهائياً.
    """
    from urllib.parse import urlencode
    from inventory.models import (
        PurchaseInvoiceItem, StockTransfer,
        ScrapDismantlingYield, PartReturnGuard, B2BListingRequest,
    )
    back = reverse('inventory:product_list')
    keep = {k: request.POST.get(k) for k in ('q', 'stock') if request.POST.get(k)}
    if not _can_bulk_delete_products(request.user):
        return redirect(f"{back}?{urlencode({**keep, 'bulk': 'perm'})}")
    branch = _get_branch_for_user(request.user)
    action = (request.POST.get('action') or '').strip()
    selected = list(_selected_products(request, branch).values_list('pk', flat=True)[:5000])
    if not selected:
        return redirect(f"{back}?{urlencode({**keep, 'bulk': 'none'})}")

    if action == 'restore':
        n = Product.objects.filter(pk__in=selected, is_active=False).update(is_active=True)
        return redirect(f"{back}?{urlencode({'stock': 'archived', 'bulk': 'restored', 'restored': n})}")
    if action != 'delete':
        return redirect(f"{back}?{urlencode({**keep, 'bulk': 'none'})}")

    with transaction.atomic():
        with_stock = set(Inventory.objects.filter(product_id__in=selected, quantity__gt=0)
                         .values_list('product_id', flat=True))
        candidates = [pk for pk in selected if pk not in with_stock]
        has_history = set()
        for model in (PurchaseInvoiceItem, SaleInvoiceItem, InventoryMovement, StockTransfer,
                      ScrapDismantlingYield, PartReturnGuard, B2BListingRequest):
            has_history |= set(model.objects.filter(product_id__in=candidates)
                               .values_list('product_id', flat=True))
        to_archive = [pk for pk in candidates if pk in has_history]
        to_delete = [pk for pk in candidates if pk not in has_history]
        archived = Product.objects.filter(pk__in=to_archive).update(is_active=False)
        deleted = len(to_delete)
        Product.objects.filter(pk__in=to_delete).delete()
    return redirect(f"{back}?{urlencode({**keep, 'bulk': 'deleted', 'deleted': deleted, 'archived': archived, 'skipped': len(with_stock)})}")


def _expense_share_per_unit(product_ids):
    """{product_id: نصيب القطعة من مصاريف الشراء (شحن/جمارك…)} من آخر استلام ليها.

    = تكلفة الوصول للوحدة − سعر المورد في آخر فاتورة شراء معتمدة فيها الصنف.
    """
    from inventory.models import PurchaseInvoiceItem
    ids = list(product_ids)
    if not ids:
        return {}
    out = {}
    rows = (PurchaseInvoiceItem.objects.filter(product_id__in=ids, invoice__is_applied=True)
            .order_by("product_id", "-invoice__date_created", "-id")
            .values_list("product_id", "landed_unit_cost", "cost_price"))
    seen = set()
    for pid, landed, cost in rows:
        if pid in seen:
            continue
        seen.add(pid)   # آخر استلام بس
        share = Decimal(str(landed or 0)) - Decimal(str(cost or 0))
        if landed and share > 0:
            out[pid] = share
    return out


def _stock_purchase_expenses(stock_rows, branch):
    """(مصاريف شراء محمّلة على اللي في المخزن, مصاريف «على الفترة» لبضاعة لسه موجودة).

    🐛 [FIX]: كان نصيب القطعة من آخر استلام بيتضرب في كل الرصيد (حتى القطع اللي
    جت قبل كده من غير مصاريف)، و«على الفترة» كانت بتعدّ مصاريف أي فاتورة ليها صنف
    واحد في المخزن (حتى لو بضاعتها اتباعت من شهور). دلوقتي بنفترض إن اللي في
    المخزن من أحدث الاستلامات (الأقدم بيتباع الأول)، وكل فاتورة بتتحسب مصاريفها
    على قد قطعها اللي لسه موجودة.
    """
    from collections import defaultdict
    from inventory.models import PurchaseInvoiceExtraCost, PurchaseInvoiceItem
    need = defaultdict(int)
    for pid, q in stock_rows:
        if q and q > 0:
            need[pid] += q
    if not need:
        return Decimal("0"), Decimal("0")
    lines = PurchaseInvoiceItem.objects.filter(product_id__in=list(need), invoice__is_applied=True)
    if branch is not None:
        lines = lines.filter(invoice__branch=branch)
    landed, taken = Decimal("0"), defaultdict(int)
    for pid, iid, qty, ret, unit, cost in (lines.order_by("product_id", "-invoice__date_created", "-id")
                                           .values_list("product_id", "invoice_id", "quantity",
                                                        "returned_quantity", "landed_unit_cost",
                                                        "cost_price")):
        take = min(need[pid], max((qty or 0) - (ret or 0), 0))
        if take <= 0:
            continue
        need[pid] -= take
        taken[iid] += take
        per = Decimal(str(unit or 0)) - Decimal(str(cost or 0))
        if unit and per > 0:
            landed += Decimal(take) * per
    period = Decimal("0")
    if taken:
        expense_kinds = [k for k, b in PurchaseInvoiceExtraCost._DEFAULT_BEHAVIOR.items() if b == 'expense']
        units = dict(PurchaseInvoiceItem.objects.filter(invoice_id__in=list(taken))
                     .values_list("invoice_id").annotate(u=Sum(F("quantity") - F("returned_quantity"))))
        spent = (PurchaseInvoiceExtraCost.objects.filter(invoice_id__in=list(taken))
                 .filter(Q(behavior='expense') | Q(behavior='', kind__in=expense_kinds))
                 .values_list("invoice_id").annotate(s=Sum("amount")))
        for iid, amount in spent:
            if units.get(iid):
                period += Decimal(str(amount or 0)) * Decimal(taken[iid]) / Decimal(units[iid])
    return landed.quantize(Decimal("0.01")), period.quantize(Decimal("0.01"))


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
def product_list(request):
    from django.db.models import F

    branch = _get_branch_for_user(request.user)
    # بحث/حالة المخزون/المؤرشفة — نفس الفلتر اللي بيستخدمه التصدير والحذف الجماعي
    qs, q, stock_filter = _product_list_qs(request, branch)

    page = Paginator(qs, 30).get_page(request.GET.get("page"))
    shares = _expense_share_per_unit([p.pk for p in page.object_list])

    # توزيع القطعة على الفروع (اسم الفرع : الكمية) — يظهر خصوصاً في وضع «كل الفروع»
    products_view = []
    for p in page.object_list:
        inv_rows = list(p.inventory_set.select_related("branch").all())
        if branch is not None:
            inv_rows = [r for r in inv_rows if r.branch_id == branch.id]
        stock = sum(r.quantity for r in inv_rows)
        # توزيع الكميات على الفروع (بس الفروع اللي فيها كمية)
        by_branch = [(r.branch.name, r.quantity) for r in inv_rows if r.quantity]
        by_branch.sort(key=lambda x: (-x[1], x[0]))
        is_low = stock <= (p.min_stock_level or 0)
        unit_cost = _effective_unit_cost(p)
        line_value = Decimal(str(stock)) * unit_cost
        share = min(shares.get(p.pk, Decimal("0")), unit_cost)
        # 💹 ربح القطعة وهامش الربح % (من سعر البيع) — زي أي ERP عالمي
        retail = Decimal(str(p.retail_price or 0))
        profit = retail - unit_cost
        # نص جاهز (14.7) — الـ locale العربي كان بيعرضه «14,7»
        margin = f"{(profit * 100 / retail):.1f}" if retail > 0 else None
        products_view.append({"product": p, "stock": stock, "is_low": is_low,
                              "unit_cost": unit_cost, "value": line_value, "by_branch": by_branch,
                              "expense_share": share, "goods_cost": unit_cost - share,
                              "profit": profit, "margin": margin})

    # 📊 KPI summary across the WHOLE catalogue (not just this page) — a
    # professional stock overview: units on hand, capital tied up (cost),
    # retail value, and low/out counts.
    inv_qs = Inventory.objects.filter(product__is_active=True)
    if branch is not None:
        inv_qs = inv_qs.filter(branch=branch)
    money = DecimalField(max_digits=16, decimal_places=2)
    # 🐛 [FIX]: قيمة المخزون بالتكلفة كانت بـ«آخر سعر شراء» — لكن تكلفة البضاعة
    #    المباعة (والربح) بتتحسب بمتوسط التكلفة اللي فيه الشحن والجمارك.
    #    دلوقتي بمتوسط التكلفة، ولو لسه صفر (صنف قديم) بسعر الشراء.
    unit_cost = Case(When(product__average_cost__gt=0, then=F("product__average_cost")),
                     default=F("product__purchase_price"), output_field=money)
    agg = inv_qs.aggregate(
        units=Coalesce(Sum("quantity"), 0),
        capital=Coalesce(Sum(ExpressionWrapper(F("quantity") * unit_cost, output_field=money)), Decimal("0")),
        retail=Coalesce(Sum(ExpressionWrapper(F("quantity") * F("product__retail_price"), output_field=money)), Decimal("0")),
        wholesale=Coalesce(Sum(ExpressionWrapper(F("quantity") * F("product__b2b_wholesale_price"), output_field=money)), Decimal("0")),
    )
    in_stock = inv_qs.filter(quantity__gt=0)
    # ⚠️ أصناف في المخزون سعر بيعها أقل من تكلفتها (أو صفر) — بتنزّل القيمة البيعية
    below_cost = (in_stock.annotate(_uc=unit_cost)
                  .filter(product__retail_price__lt=F("_uc"))
                  .values("product_id").distinct().count())
    no_wholesale = (in_stock.filter(product__b2b_wholesale_price__lte=0)
                    .values("product_id").distinct().count())
    prod_stock = Product.objects.filter(is_active=True)
    if branch is not None:
        # 🐛 [FIX]: من غير الفلتر ده «نفد» كان بيعدّ كل الكتالوج (أصناف الفروع
        #    التانية رصيدها هنا صفر) — 1820 نافد في فرع فيه 43 صنف بس.
        from django.db.models import Exists, OuterRef
        prod_stock = prod_stock.filter(Exists(
            Inventory.objects.filter(product=OuterRef("pk"), branch=branch)))
    stock_sum = Sum("inventory__quantity", filter=Q(inventory__branch=branch) if branch is not None else None)
    prod_stock = prod_stock.annotate(_stock=Coalesce(stock_sum, 0, output_field=IntegerField()))
    # 🚢 الجزء من قيمة المخزون اللي هو مصاريف شراء (شحن/جمارك…) محمّلة على القطع،
    #    ومصاريف الشراء اللي اتسجّلت «على الفترة» (سفر/إعاشة) ومادخلتش التكلفة.
    stock_rows = list(in_stock.values_list("product_id", "quantity"))
    landed_in_stock, period_expenses = _stock_purchase_expenses(stock_rows, branch)
    capital = agg["capital"] or Decimal("0")
    landed_in_stock = min(landed_in_stock, capital)
    summary = {
        # نفس نطاق باقي الكروت (كل الأصناف النشطة) — كان بيتغيّر مع فلتر البحث
        "products": prod_stock.count(),
        "units": agg["units"] or 0,
        "capital": capital,
        "landed_in_stock": landed_in_stock,
        "goods_in_stock": capital - landed_in_stock,
        "period_purchase_expenses": period_expenses,
        "retail": agg["retail"] or Decimal("0"),
        "wholesale": agg["wholesale"] or Decimal("0"),
        "below_cost": below_cost,
        "no_wholesale": no_wholesale,
        "out_count": prod_stock.filter(_stock__lte=0).count(),
        "low_count": prod_stock.filter(_stock__gt=0, _stock__lte=F("min_stock_level")).count(),
    }

    # 💰 التحكّم الدقيق في الأسعار الظاهرة حسب صلاحية الموظف والفرع النشط
    show_cost, show_sale = _product_price_visibility(request, branch)

    from inventory.services import fixit_sync
    return render(request, "inventory/product_list.html", {
        "page": page,
        "rows": products_view,
        "q": q,
        "stock_filter": stock_filter,
        "branch": branch,
        "summary": summary,
        "show_cost": show_cost,
        "show_sale": show_sale,
        "fixit_sync_configured": fixit_sync.is_configured(),
        "fixit_sync_enabled": fixit_sync.is_enabled(),
        "sync_notice": (request.GET.get("sync") or "").strip(),
        "sync_n": (request.GET.get("n") or "").strip(),
        "sync_img": (request.GET.get("img") or "").strip(),
        "sync_err": (request.GET.get("err") or "").strip(),
        "total_matching": page.paginator.count,
        # أعمدة التصدير المسموحة للموظف ده (اللي مايشوفش التكلفة مايصدّرهاش)
        "export_cols": [
            (k, label, k in _PRODUCT_EXPORT_DEFAULT) for k, label, need in _PRODUCT_EXPORT_COLS
            if need is None or (need == 'cost' and show_cost) or (need == 'sale' and show_sale)],
        "can_delete": _can_bulk_delete_products(request.user),
        "can_import": _can_import_products(request.user),
        "bulk": (request.GET.get("bulk") or "").strip(),
        "bulk_n": {k: request.GET.get(k, "") for k in ("deleted", "archived", "skipped", "restored")},
    })


@login_required(login_url='/login/')
@tenant_required
@module_required('inventory')
@role_required('admin', 'manager')
@require_POST
def fixit_sync_now(request):
    """زر «تحديث الموقع الآن» — مزامنة كاملة لكل المنتجات (اسم/سعر/صورة/مخزون)
    مع موقع FixIt، في الخلفية عشان الصفحة ترجع فوراً.

    ملحوظة: التحديثات العادية (إضافة/تعديل منتج أو حركة مخزون) بتتزامن تلقائياً
    عبر الـ signals — الزر ده للمزامنة الكاملة/إعادة الرفع عند الحاجة.
    """
    from inventory.services import fixit_sync

    if not fixit_sync.is_configured():
        return redirect(f"{reverse('inventory:product_list')}?sync=notconfigured")
    if not fixit_sync.is_enabled():
        return redirect(f"{reverse('inventory:product_list')}?sync=notbranch")

    prune = request.POST.get("prune") == "1"
    # نشغّلها synchronously في نفس الطلب (سياق الفرع صح هنا) عشان نرجّع للمستخدم
    # النتيجة الحقيقية: اتبعت كام، اترفض كام وليه — بدل ما نبلع الخطأ في الخلفية.
    from urllib.parse import urlencode
    try:
        res = fixit_sync.push_all_products(prune=prune)
    except Exception as exc:  # noqa: BLE001
        params = urlencode({"sync": "error", "err": str(exc)[:180]})
        return redirect(f"{reverse('inventory:product_list')}?{params}")
    params = {
        "sync": "done" if not res.get("batches_failed") else "partial",
        "n": res.get("items", 0),
        "img": res.get("with_image", 0),
    }
    if res.get("errors"):
        params["err"] = (res["errors"][0])[:180]
    return redirect(f"{reverse('inventory:product_list')}?{urlencode(params)}")
