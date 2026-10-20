"""الاستيرادات والمساعدات المشتركة بين الشاشات: البحث العربي المُطبَّع، «عميل نقدي»،
تسجيل دفعات الفاتورة وقيدها، صلاحيات الفواتير، وفرع/فترة التقارير.

جزء من inventory/views_lightning (اتقسم من ملف واحد ~٧٠٠٠ سطر).
"""
import re as _re
from decimal import Decimal, InvalidOperation
from django.db.models import Case, CharField, F, Func, IntegerField, Q, Value, When
from django.db.models.functions import Lower, Replace
from inventory.models import (
    Branch,
    Customer,
    EmployeeProfile,
    FinancialTransaction,
    SaleInvoice,
    Treasury,
)
from inventory.views import _get_branch_for_user


WALK_IN_PHONE = "0000000000"


WALK_IN_NAME = "عميل نقدي (Walk-in)"


# =====================================================================
# 🔎 بحث عربي مُطبَّع — يوحّد صيغ الحروف عشان "مساعد" تلاقي "مسـاعد"/"مساعد"
# مهما كانت الهمزات أو التطويل أو التاء المربوطة في الداتا المستوردة.
# =====================================================================
# توحيد الحروف: أ إ آ ٱ → ا | ة → ه | ى → ي | ؤ → و | ئ → ي
_AR_FOLD = [('أ', 'ا'), ('إ', 'ا'), ('آ', 'ا'), ('ٱ', 'ا'),
            ('ة', 'ه'), ('ى', 'ي'), ('ؤ', 'و'), ('ئ', 'ي'), ('ـ', '')]


# إزالة التشكيل والتطويل من نص الاستعلام (بايثون)
_AR_DIACRITICS = _re.compile(r'[ـً-ْٰ]')


def _norm_ar(s):
    """تطبيع نص عربي: توحيد الهمزات/التاء/الألف المقصورة + إزالة التشكيل والتطويل."""
    s = (s or '')
    for a, b in _AR_FOLD:
        s = s.replace(a, b)
    s = _AR_DIACRITICS.sub('', s)
    return s.strip().lower()


def _ar_field_expr(field):
    """تعبير DB يطبّع حقل نصّي بنفس قواعد _norm_ar.

    🐛 [FIX]: التشكيل ماكانش بيتشال من ناحية الداتابيز، فـ«محمد» ماكانتش
    بتلاقي «محمّد» المتسجّل بالشدّة (الاستعلام بيتشال تشكيله بس الاسم لأ).
    """
    expr = F(field)
    for a, b in _AR_FOLD:
        expr = Replace(expr, Value(a), Value(b))
    expr = Func(expr, Value('[\u064B-\u0652\u0670]'), Value(''), Value('g'),
                function='REGEXP_REPLACE', output_field=CharField())
    return Lower(expr)


def _apply_product_search(qs, q):
    """بحث ذكي بالتقارب (fuzzy) على المنتجات — زي محرّكات البحث العالمية.

    الفكرة: بدل ما نطلب إن *كل* كلمات الاستعلام تظهر في الاسم (اللي كان بيرجّع
    "مفيش نتائج" لو العميل كتب كلمة زيادة)، بنحسب "درجة تطابق" = عدد كلمات
    الاستعلام اللي ظهرت في اسم المنتج المُطبَّع، وبنسمح بغياب كلمة واحدة.

    مثال: "فلتر زيت فتيس" (٣ كلمات) بيلاقي منتج اسمه "فلتر فتيس" (تطابق ٢/٣)،
    والنتائج بتترتّب بالأعلى تطابقاً الأول.

    - الاسم بيتطبّع على مستوى DB (توحيد الهمزات/التاء) ويتقارن بالكلمات المُطبَّعة.
    - الكود/الباركود/الماركة/الموديل بتتبحث بالنص الخام كمان (icontains) وبتتحسب
      تطابق كامل (أولوية قصوى).
    """
    q = (q or '').strip()
    if not q:
        return qs
    qs = qs.annotate(_nname=_ar_field_expr('name'))
    qn = _norm_ar(q)
    tokens = [t for t in qn.split() if t]

    # تطابق مباشر بالكود/الباركود/الماركة/الموديل أو الاسم الكامل — أولوية قصوى
    code_cond = (
        Q(part_number__icontains=q) | Q(barcode__icontains=q) |
        Q(brand__icontains=q) | Q(car_model__icontains=q) |
        Q(_nname__contains=qn)
    )
    if not tokens:
        return qs.filter(code_cond)

    # درجة التطابق = كام كلمة من الاستعلام ظهرت في الاسم المُطبَّع
    score = Value(0, output_field=IntegerField())
    for tok in tokens:
        score = score + Case(
            When(_nname__contains=tok, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
    qs = qs.annotate(_score=score)

    # نسمح بغياب كلمة واحدة كحد أقصى (tolerance)، مع حد أدنى كلمة واحدة
    threshold = max(1, len(tokens) - 1)
    match = Q(_score__gte=threshold) | code_cond
    # الأعلى تطابقاً الأول، وبعدين ترتيب أبجدي للاسم
    return qs.filter(match).order_by('-_score', 'name')


def _finite_dec(raw, default=None):
    """رقم من فورم/JSON (بيشيل فواصل الآلاف) — أو ``default`` لو مش رقم صحيح.

    🐛 [FIX]: «NaN»/«Infinity» كانوا بيعدّوا Decimal() وبعدين أي مقارنة (> 0)
    بترمي InvalidOperation ⇒ صفحة خطأ 500 بدل رسالة.
    """
    if raw is None or str(raw).strip() == '':
        return default
    try:
        d = Decimal(str(raw).replace(',', '').replace('٬', '').strip())
    except (InvalidOperation, ValueError, TypeError):
        return default
    return d if d.is_finite() else default


class _RollbackWith(Exception):
    """Raised inside ``transaction.atomic()`` to undo every write and answer
    with ``response``.

    🐛 [FIX]: ``return`` من جوه ``with transaction.atomic()`` بيعمل commit
    لكل اللي اتكتب قبله. في شاشة الشراء (وضع التعديل) ده كان معناه إن
    الفاتورة القديمة تتعكس (مخزون/مورد/دفعات) وتتمسح بنودها، وبعدين خطأ
    «رصيد الخزنة غير كافٍ» يرجع للمستخدم — والداتابيز فاضلة نص متعدلة.
    """

    def __init__(self, response):
        super().__init__('rollback')
        self.response = response


def _walk_in_customer():
    """Single shared walk-in row — phone is unique on Customer.

    🛡️ We look up by the *normalized* form because Customer.save() rewrites
    the phone (e.g. '0000000000' → '+00000000'). Looking up by the raw form
    misses the existing row, then the create attempt races into a UNIQUE
    constraint violation and the whole request 500s.
    """
    normalized = Customer.normalize_phone(WALK_IN_PHONE)
    cust, _ = Customer.objects.get_or_create(
        phone=normalized,
        defaults={"name": WALK_IN_NAME},
    )
    return cust


def _normalize_tenders(treasury_id, paid_amount_raw, payments):
    """يوحّد صيغة الدفع: يقبل إما (خزنة واحدة + مبلغ) أو قائمة دفعات مقسّمة.

    بيرجّع list of (treasury_id, Decimal amount) للمبالغ الموجبة فقط.
    ده اللي بيسمح بالدفع المقسّم (جزء كاش + جزء انستا) على نفس الفاتورة.
    """
    tenders = []
    if payments:
        for p in payments:
            try:
                amt = Decimal(str(p.get("amount")))
            except (InvalidOperation, TypeError):
                continue
            tid = p.get("treasury_id")
            if tid and amt > 0:
                tenders.append((tid, amt))
    else:
        try:
            amt = Decimal(str(paid_amount_raw)) if paid_amount_raw not in (None, "") else Decimal("0")
        except InvalidOperation:
            amt = Decimal("0")
        if treasury_id and amt > 0:
            tenders.append((treasury_id, amt))
    return tenders


def _record_invoice_payments(invoice, tenders, request_user):
    """يسجّل دفعة (أو أكثر) لفاتورة بيع بشكل ذرّي — يدعم الدفع المقسّم:
      - كل دفعة = FinancialTransaction(in) على خزنتها (الـ signal بيزوّد الرصيد)
      - invoice.paid_amount = مجموع كل الدفعات (الباقي بيفضل آجل على العميل)
      - invoice.treasury = أول خزنة (للتوافق مع الطباعة/التقارير القديمة)
    بيرجّع إجمالي المدفوع (Decimal). لازم يتنادى جوه transaction.atomic.
    """
    # 🛡️ حماية من الغلط: المدفوع لا يزيد عن إجمالي الفاتورة (يمنع كتابة صفر زيادة
    # زي 13500 على فاتورة 1350 — اللي بيضخّم الخزنة بفرق وهمي).
    total_tender = sum((amt for _tid, amt in tenders), Decimal("0"))
    inv_total = Decimal(str(invoice.total_amount or 0))
    if inv_total > 0 and total_tender > inv_total + Decimal("0.01"):
        raise ValueError(
            f"المدفوع ({total_tender:.2f}) أكبر من إجمالي الفاتورة ({inv_total:.2f}). "
            f"راجع المبلغ."
        )
    total_paid = Decimal("0")
    primary = None
    for tid, amt in tenders:
        treasury = (Treasury.objects.select_for_update()
                    .filter(id=tid, branch=invoice.branch, is_active=True).first())
        if treasury is None:
            raise ValueError("الخزنة المختارة غير متاحة في هذا الفرع.")
        # 🐛 [DOUBLE-COUNT FIX] لا نزوّد الرصيد يدوياً: إنشاء الـ
        # FinancialTransaction بيطلق signal (update_balance) اللي بيزوّد الخزنة.
        FinancialTransaction.objects.create(
            treasury=treasury,
            transaction_type="in",
            amount=amt,
            description=f"دفعة فاتورة #{invoice.id} ({treasury.name}) — {invoice.customer_display_name}",
            sale_invoice=invoice,
            customer=invoice.customer,
        )
        total_paid += amt
        if primary is None:
            primary = treasury
    if total_paid <= 0:
        return Decimal("0")
    invoice.paid_amount = total_paid
    invoice.treasury = primary
    invoice.save(update_fields=["paid_amount", "treasury"])
    return total_paid


def _record_invoice_payment(invoice, treasury_id, paid_amount_raw, request_user):
    """توافق خلفي — دفعة واحدة بخزنة واحدة (يستدعي المسجّل المقسّم)."""
    tenders = _normalize_tenders(treasury_id, paid_amount_raw, None)
    return _record_invoice_payments(invoice, tenders, request_user)


def _post_sale_to_ledger(invoice, request_user):
    """يقيّد الفاتورة في دفتر الأستاذ (إيراد/تكلفة/مديونية) بعد ضبط الأصناف.

    البيع السريع (POS وأمر الشغل) بينشئ الفاتورة قبل الأصناف فمابيتقيّدش
    محاسبياً وقتها؛ فبنستدعي المحرّك هنا بعد update_total. الاستدعاء idempotent
    (قيد مبيعات واحد للفاتورة) وأي خطأ محاسبي بيتسجّل ومابيوقفش البيع.
    """
    try:
        from inventory.services.accounting_service import AccountingService
        AccountingService.post_sale_invoice(invoice, created_by=request_user)
    except Exception as exc:  # noqa: BLE001 — قيد الدفتر لا يوقف الفاتورة أبداً
        import logging as _l
        _l.getLogger('mouss_tec_core').warning(
            "[GL] post_sale_invoice failed for INV #%s: %s", invoice.id, exc)


def _resolve_customer(name, phone):
    """Find-or-create by phone (the unique natural key). Blank phone → walk-in.

    🛡️ Normalize before querying — Customer.save() rewrites phones so a
    raw-form lookup would miss and race the UNIQUE constraint.
    """
    raw = (phone or "").strip()
    name = (name or "").strip()
    if not raw:
        return _walk_in_customer()
    normalized = Customer.normalize_phone(raw)
    cust, created = Customer.objects.get_or_create(
        phone=normalized,
        defaults={"name": name or f"عميل {raw}"},
    )
    if not created and name and cust.name != name and cust.name == WALK_IN_NAME:
        cust.name = name
        cust.save(update_fields=["name"])
    return cust


def _is_walk_in(customer):
    return customer is not None and customer.is_walk_in


def _named_walk_in(customer, typed_name):
    """🐛 [FIX]: اسم مكتوب من غير رقم كان بيضيع والفاتورة تتسجّل «عميل نقدي».

    لو الاسم مطابق لعميل واحد مسجّل → نربط بيه. غير كده نسيب «عميل نقدي» (الرقم
    مطلوب لإنشاء عميل) ونرجّع ملاحظة بالاسم تتكتب على الفاتورة.
    Returns (customer, note_or_None).
    """
    typed = (typed_name or "").strip()
    if not _is_walk_in(customer) or not typed or typed == WALK_IN_NAME:
        return customer, None
    matches = list(Customer.objects.filter(name__iexact=typed)[:2])
    if len(matches) == 1:
        return matches[0], None
    return customer, SaleInvoice.WALK_IN_NAME_PREFIX + SaleInvoice.clean_walk_in_name(typed)


_WALK_IN_CREDIT_ERROR = ("البيع الآجل (جزء مش مدفوع) محتاج رقم موبايل العميل — من غيره "
                         "المديونية بتتسجّل على «عميل نقدي» ومحدش يعرف مين عليه الفلوس.")


def _can_edit_invoices(user):
    """صلاحية تعديل/حذف الفواتير والمصاريف — أدمن/مدير/محاسب أو من مُنح الصلاحية."""
    if user.is_superuser:
        return True
    prof = getattr(user, 'employee_profile', None)
    if not prof:
        return False
    return (prof.is_manager_or_above or prof.role == 'accountant'
            or prof.can_edit_posted_invoices)


def _is_finance_manager(user):
    """أدمن/مدير/محاسب (نفس بوابة role_required لإلغاء الدفعات)."""
    if user.is_superuser:
        return True
    role = getattr(getattr(user, 'employee_profile', None), 'role', None)
    role = {'owner': 'admin', 'supervisor': 'manager'}.get(role, role)
    return role in ('admin', 'manager', 'accountant')


def _product_price_visibility(request, branch):
    """(show_cost, show_sale) حسب صلاحية الموظف والفرع النشط."""
    prof = getattr(request.user, 'employee_profile', None)
    if request.user.is_superuser or prof is None:
        return True, True
    if branch is None:
        from inventory.models import EmployeeProfile
        show_cost = (prof.can_see_costs
                     or EmployeeProfile.canonical_role(prof.role)
                     in ('admin', 'manager', 'accountant'))
        return show_cost, True
    pv = prof.price_view_for_branch(branch.id)
    return pv in ('both', 'cost'), pv in ('both', 'sale')


def _can_bulk_delete_products(user):
    """الحذف/الأرشفة الجماعية بتأثر على الكتالوج كله — أدمن/مدير بس."""
    if user.is_superuser:
        return True
    prof = getattr(user, 'employee_profile', None)
    if prof is None:
        return False
    return EmployeeProfile.canonical_role(prof.role) in ('admin', 'manager')


def _report_branch(request):
    """يحدد فرع التقرير: الأدمن (وضع كل الفروع) يقدر يفلتر بأي فرع عبر ?branch=،
    وموظف الفرع محصور بفرعه. بيرجّع (الفرع المختار أو None, قائمة الفروع للفلتر, هل يقدر يشوف الكل)."""
    scoped = _get_branch_for_user(request.user)  # None = أدمن شايف كل الفروع
    if scoped is not None:
        # محصور بفرعه — الفلتر مقفول على فرعه
        return scoped, [scoped], False
    chosen = None
    bid = (request.GET.get('branch') or '').strip()
    if bid.isdigit():
        chosen = Branch.objects.filter(id=int(bid)).first()
    return chosen, list(Branch.objects.all().order_by('name')), True


def _report_period(request, default='month'):
    """يحلّل ?period= إلى (start, end, label, key).

    القيم: today · week · month · last_month · year · all · custom
    (custom مع ?from=YYYY-MM-DD&to=YYYY-MM-DD — «إلى» شاملة اليوم كله).
    end=None يعني حتى الآن. تُستخدم في كل التقارير لتوحيد فلتر الفترة.
    """
    from django.utils import timezone as _tz
    from datetime import timedelta, datetime as _dt
    now = _tz.localtime()   # بداية اليوم/الشهر بتوقيت المحل مش UTC
    p = (request.GET.get('period') or default).strip()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = midnight.replace(day=1)
    if p == 'today':
        return midnight, None, "اليوم", 'today'
    if p == 'week':
        # 🐛 [FIX]: كان midnight − 7 أيام = 8 أيام بالنهارده
        return midnight - timedelta(days=6), None, "آخر 7 أيام", 'week'
    if p == 'last_month':
        prev_start = (month_start - timedelta(days=1)).replace(day=1)
        return prev_start, month_start, "الشهر اللي فات", 'last_month'
    if p == 'year':
        return midnight.replace(month=1, day=1), None, "هذه السنة", 'year'
    if p == 'all':
        return None, None, "كل الفترات", 'all'
    if p == 'custom':
        def _day(key):
            try:
                d = _dt.strptime((request.GET.get(key) or '').strip(), '%Y-%m-%d')
            except ValueError:
                return None
            return _tz.make_aware(d, _tz.get_current_timezone())
        d_from, d_to = _day('from'), _day('to')
        if d_from and d_to and d_to < d_from:
            d_from, d_to = d_to, d_from
        if d_from or d_to:
            label = "من {} إلى {}".format(
                d_from.strftime('%Y-%m-%d') if d_from else '…',
                d_to.strftime('%Y-%m-%d') if d_to else 'النهارده')
            return d_from, (d_to + timedelta(days=1)) if d_to else None, label, 'custom'
    return month_start, None, "هذا الشهر", 'month'
