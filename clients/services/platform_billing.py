"""
💸 Platform billing helpers used by the owner's control panel.

Manual activations and renewals from the super-admin used to only push
``Client.subscription_end_date`` forward: no ``PlatformInvoice``, the
``TenantSubscription`` period/entitlements left behind. Revenue pages built
on invoices then disagreed with MRR built on active subscriptions, and the
tenant's own billing history had a hole.

Everything here goes through the same path as a Paymob payment:
PlatformInvoice → ``mark_paid()`` (extends the subscription, snapshots the
plan revision's entitlements/price, re-activates the tenant).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

PERIOD_DAYS = {1: 30, 3: 90, 6: 180, 12: 365}


def addons_amount(tenant, months):
    """➕ الإضافات المشتراة (موظف/فرع/خزينة) بتتجدد مع الاشتراك بسعرها الشهري.

    🐛 [FIX]: الإضافة كانت بتتدفع مرة واحدة (لحد آخر المدة الحالية) وبعد التجديد تفضل
    مجانية للأبد — مع إنها بتتباع «125 ج.م/شهر»."""
    extras = ((tenant.extra_users_purchased or 0) + (tenant.extra_branches_purchased or 0)
              + (tenant.extra_treasuries_purchased or 0))
    return (Decimal(str(tenant.ADDON_PRICE_PER_MONTH)) * extras * int(months or 1)).quantize(Decimal('0.01'))


def renewal_amount(tenant, plan, months):
    """اللي بيتحصّل فعلاً لمدة اشتراك: الباقة (بخصم المدة) + الإضافات."""
    return plan.price_for_period(months) + addons_amount(tenant, months)


def _revision_for(plan):
    from clients.models import PlanRevision
    rev = PlanRevision.objects.filter(plan=plan).order_by('-effective_from').first()
    if rev is None:
        rev = PlanRevision.create_from_plan(plan, change_reason='Auto-created on admin billing action')
    return rev


@transaction.atomic
def issue_subscription_invoice(tenant, plan, months=1, *, paid=True, gift=False,
                               provider='manual_admin', reference='', notes='', user=None,
                               payment_provider_always=False, kind='subscription'):
    """Issue (and optionally settle) a subscription invoice for ``months``.

    * ``paid``  → ``mark_paid()``: extends the subscription from the later of
      today / current end date, activates the tenant.
    * ``gift``  → total 0 (100% discount), still paid → extends for free but
      shows up as a gift in the billing history instead of a silent date bump.
    * ``paid=False`` → an *issued* invoice the tenant owes (no extension).
    """
    from clients.models import PlatformInvoice, TenantSubscription

    months = int(months or 1)
    if months not in PERIOD_DAYS:
        months = 1
    if paid or gift:
        sub, _ = TenantSubscription.objects.get_or_create(tenant=tenant, defaults={'plan': plan})
    else:
        # فاتورة لسه ماتدفعتش مابتعملش اشتراك (mark_paid بيعمله) — الشركة القديمة اللي فتحت
        # صفحة الدفع ومشيت تفضل محتفظة باستثنائها من قفل المزايا
        sub = TenantSubscription.objects.filter(tenant=tenant).first()
    today = timezone.localdate()
    start = max(tenant.subscription_end_date or today, today)
    end = start + timedelta(days=PERIOD_DAYS[months])

    addons = Decimal('0.00') if gift else addons_amount(tenant, months)
    subtotal = (Decimal(str(plan.monthly_price)) * months).quantize(Decimal('0.01')) + addons
    total = Decimal('0.00') if gift else plan.price_for_period(months) + addons
    discount_amount = (subtotal - total).quantize(Decimal('0.01'))
    plan_subtotal = subtotal - addons
    discount_percent = int(round((discount_amount / plan_subtotal) * 100)) if plan_subtotal else 0

    inv = PlatformInvoice.objects.create(
        tenant=tenant, subscription=sub, plan_revision=_revision_for(plan), kind=kind,
        period_start=start, period_end=end, billing_cycle_months=months,
        subtotal=subtotal, discount_percent=discount_percent, discount_amount=discount_amount,
        addons_amount=addons, total=total, entitlements_snapshot=dict(plan.entitlements or {}),
        status='issued', notes=(('🎁 هدية — ' if gift else '') + (notes or ''))[:1000],
        created_by=user if getattr(user, 'is_authenticated', False) else None,
        # فاتورة مستحقة لطلب على بوابة دفع: المرجع = رقم الطلب عشان الـ callback يلاقيها
        **({'payment_provider': provider, 'payment_reference': reference}
           if (reference or payment_provider_always) and not (paid or gift) else {}),
    )
    if paid or gift:
        inv.mark_paid(payment_provider='gift' if gift else provider,
                      payment_reference=reference or None)
        _sync_legacy_plan(tenant, plan)
    return inv


def _sync_legacy_plan(tenant, plan):
    # Legacy CharField (لسه بتتقري في صفحة الأسعار وكـ fallback لحد الطلبات)
    from tenancy.services.plan_mapping import legacy_plan_value
    tenant.plan = legacy_plan_value(plan.slug)
    tenant.save(update_fields=['plan'])


# ─────────────────────────────────────────────────────────────────────
# 💳 الدفع بالكارت (Paymob): الفاتورة بتتسجّل قبل الدفع، مش في الكاش
# ─────────────────────────────────────────────────────────────────────
# 🐛 [FIX]: بيانات الطلب (الشركة/الباقة/المدة) كانت في الكاش بس لمدة ساعتين —
#    لو Redis اتعمله restart أو إشعار Paymob اتأخر، العميل يدفع والاشتراك
#    مايتفعّلش ومافيش أي أثر للدفعة. دلوقتي فاتورة «مصدرة» بمرجع رقم الطلب.

@transaction.atomic
def open_gateway_invoice(tenant, plan, months, *, provider, reference, user=None):
    """فاتورة مستحقة لطلب دفع جديد — والطلبات القديمة اللي ماتدفعتش بتتلغي
    (لو اتدفعت بعدين برضه بتتسوّى: الفلوس وصلت)."""
    from clients.models import PlatformInvoice
    for old in PlatformInvoice.objects.select_for_update().filter(
            tenant=tenant, status='issued', payment_provider=provider):
        void_invoice(old, reason='طلب دفع أحدث', user=user)
    return issue_subscription_invoice(tenant, plan, months, paid=False, provider=provider,
                                      reference=str(reference), user=user,
                                      notes=f'دفع بالكارت ({provider}) — في انتظار الدفع')


@transaction.atomic
def settle_gateway_invoice(provider, reference, *, paid_cents):
    """يسوّي فاتورة طلب دفع اتأكد (بعد التحقق من التوقيع). بيرجّع (الحالة، الفاتورة):
    paid | already_paid | amount_mismatch | not_found | bad_status."""
    from clients.models import PlatformInvoice
    inv = (PlatformInvoice.objects.select_for_update()
           .filter(payment_provider=provider, payment_reference=str(reference))
           .select_related('tenant', 'plan_revision__plan').first())
    if inv is None:
        return 'not_found', None
    if inv.status == 'paid':
        return 'already_paid', inv          # Paymob بيبعت الإشعار أكتر من مرة
    if inv.status not in ('issued', 'void'):
        return 'bad_status', inv
    expected = int((inv.total * 100).to_integral_value())
    if int(paid_cents or 0) < expected:
        return 'amount_mismatch', inv
    return 'paid', settle_issued_invoice(inv, provider=provider, reference=str(reference))


# ─────────────────────────────────────────────────────────────────────
# ⬆️⬇️ تغيير الباقة في نص المدة — زي الأنظمة العالمية (Stripe/Shopify)
# ─────────────────────────────────────────────────────────────────────
# 🐛 [FIX]: أي دفع لباقة تانية كان بيبدّل الباقة فوراً والفترة المدفوعة تبدأ بعد آخر المدة
#    الحالية: الترقية من Silver لـ Empire وفاضل ٢٥ يوم = ٢٥ يوم Empire ببلاش، والتخفيض بدري
#    = العميل يخسر أيام الباقة الأعلى اللي دافع تمنها فوراً. دلوقتي:
#    • ترقية: فورية، وبتتحصّل فرق السعر عن الأيام الفاضلة بس (تاريخ التجديد مايتغيّرش).
#    • تخفيض: بيتدفع دلوقتي ويتطبّق أول يوم بعد المدة الحالية (apply_scheduled_plan_changes)،
#      وبيترفض لو المستخدمين/الفروع/الخزائن الحالية أكتر من حدود الباقة الجديدة.

def _usage_counts(tenant):
    from django_tenants.utils import schema_context
    from django.contrib.auth import get_user_model
    from inventory.models import Branch, Treasury
    with schema_context(tenant.schema_name):
        return {
            'users': get_user_model().objects.filter(is_active=True).count(),
            'branches': Branch.objects.count(),
            'treasuries': Treasury.objects.count(),
        }


def _downgrade_blockers(tenant, plan, usage=None):
    """اللي لازم يتقفل قبل التخفيض (قائمة رسايل) — فاضية لو الاستخدام جوه الحدود."""
    try:
        used = usage if usage is not None else _usage_counts(tenant)
    except Exception:
        return []   # سكيمة مش جاهزة (اختبارات/شركة بتتعمل) — مانمنعش الدفع بسببها
    if not used:
        return []
    labels = {'users': 'مستخدم نشط', 'branches': 'فرع', 'treasuries': 'خزينة'}
    allowed = {
        'users': plan.max_users + tenant.extra_users_purchased,
        'branches': plan.max_branches + tenant.extra_branches_purchased,
        'treasuries': plan.max_treasuries + tenant.extra_treasuries_purchased,
    }
    return [f"عندك {used[k]} {labels[k]} والباقة الجديدة بتسمح بـ {allowed[k]} — قفّل {used[k] - allowed[k]} الأول."
            for k in labels if allowed[k] > 0 and used[k] > allowed[k]]


def quote_plan_change(tenant, plan, months=1, usage=None, sub=None):
    """تسعيرة الاشتراك في ``plan`` لمدة ``months`` للشركة دي. بترجّع dict:
    kind: new | renewal | upgrade | downgrade، total، period_start، period_end،
    remaining_days، blockers (لو فيها حاجة التخفيض ممنوع)."""
    from clients.models import TenantSubscription
    months = int(months or 1) if int(months or 1) in PERIOD_DAYS else 1
    today = timezone.localdate()
    if sub is None:
        sub = TenantSubscription.objects.filter(tenant=tenant).select_related('plan').first()
    end = tenant.subscription_end_date
    paid_active = (tenant.status == 'active' and end and end > today and sub is not None
                   and sub.plan_id is not None)
    start = max(end or today, today)
    quote = {'kind': 'new', 'months': months, 'total': renewal_amount(tenant, plan, months),
             'period_start': start, 'period_end': start + timedelta(days=PERIOD_DAYS[months]),
             'remaining_days': 0, 'blockers': []}
    if not paid_active:
        return quote
    if sub.plan_id == plan.id:
        quote['kind'] = 'renewal'
        return quote

    current_monthly = sub.effective_monthly_price or sub.plan.monthly_price
    if plan.monthly_price > current_monthly:
        # ترقية: فرق السعر اليومي (بخصم دورة الفوترة الحالية) × الأيام الفاضلة
        cycle = sub.billing_cycle_months if sub.billing_cycle_months in PERIOD_DAYS else 1
        days = PERIOD_DAYS[cycle]
        new_daily = plan.price_for_period(cycle) / days
        old_daily = sub.plan.price_for_period(cycle, monthly_price=current_monthly) / days
        remaining = (end - today).days
        total = ((new_daily - old_daily) * remaining).quantize(Decimal('0.01'))
        quote.update(kind='upgrade', months=cycle, total=max(total, Decimal('0.01')),
                     period_start=today, period_end=end, remaining_days=remaining)
        return quote

    quote['kind'] = 'downgrade'
    quote['blockers'] = _downgrade_blockers(tenant, plan, usage)
    return quote


@transaction.atomic
def open_plan_change_invoice(tenant, plan, months, *, provider, reference='', user=None):
    """فاتورة مستحقة لطلب اشتراك من العميل (كارت أو فودافون/إنستاباي) حسب التسعيرة.
    بترفع ValidationError لو التخفيض ممنوع (الاستخدام أكبر من حدود الباقة)."""
    from django.core.exceptions import ValidationError
    from clients.models import PlatformInvoice, TenantSubscription
    quote = quote_plan_change(tenant, plan, months)
    if quote['blockers']:
        raise ValidationError(quote['blockers'])
    if reference:
        # طلبات الكارت القديمة اللي ماتدفعتش بتتلغي (لو اتدفعت بعدين برضه بتتسوّى)
        for old in PlatformInvoice.objects.select_for_update().filter(
                tenant=tenant, status='issued', payment_provider=provider):
            void_invoice(old, reason='طلب دفع أحدث', user=user)
    if quote['kind'] != 'upgrade':
        return issue_subscription_invoice(
            tenant, plan, quote['months'], paid=False, provider=provider, reference=str(reference or ''),
            user=user, notes=('تخفيض — يبدأ بعد المدة الحالية' if quote['kind'] == 'downgrade' else ''),
            payment_provider_always=True,
            kind='downgrade' if quote['kind'] == 'downgrade' else 'subscription')
    sub = TenantSubscription.objects.get(tenant=tenant)
    return PlatformInvoice.objects.create(
        tenant=tenant, subscription=sub, plan_revision=_revision_for(plan), kind='upgrade',
        period_start=quote['period_start'], period_end=quote['period_end'],
        billing_cycle_months=quote['months'], subtotal=quote['total'], total=quote['total'],
        entitlements_snapshot=dict(plan.entitlements or {}), status='issued',
        payment_provider=provider, payment_reference=str(reference or ''),
        notes=f"ترقية من {sub.plan.name} — فرق {quote['remaining_days']} يوم متبقي",
        created_by=user if getattr(user, 'is_authenticated', False) else None,
    )


def apply_scheduled_plan_changes(today=None):
    """⬇️ التخفيض المدفوع بيتطبّق أول يوم فترته: الباقة + الصلاحيات + الحدود من فاتورته.
    بيشتغل يومياً من orchestrate_billing_and_suspensions."""
    from clients.models import PlatformInvoice
    today = today or timezone.localdate()
    applied = 0
    # 🐛 [FIX]: كانت بتختار «آخر فاتورة بدأت» — تجديد مدفوع بدري قبل ترقية كان بيرجّع الباقة القديمة
    #    أول ما فترته تبدأ، وتفعيل المالك لباقة أعلى كان بيترجع كل يوم. دلوقتي فواتير «تخفيض» بس.
    due = (PlatformInvoice.objects.filter(status='paid', kind='downgrade',
                                          period_start__lte=today, period_end__gt=today)
           .exclude(subscription=None).select_related('subscription', 'plan_revision__plan')
           .order_by('subscription_id', '-period_start', '-pk'))
    seen = set()
    for inv in due:
        if inv.subscription_id in seen:
            continue
        seen.add(inv.subscription_id)
        sub = inv.subscription
        if sub.plan_id == inv.plan_revision.plan_id:
            continue
        # العميل غيّر الباقة تاني بعد ما جدول التخفيض (ترقية مثلاً) — التخفيض القديم مابقاش ساري
        if (PlatformInvoice.objects.filter(subscription=sub, status='paid', paid_at__gt=inv.paid_at)
                .exclude(plan_revision__plan=inv.plan_revision.plan).exists()):
            continue
        with transaction.atomic():
            sub.plan = inv.plan_revision.plan
            sub.billing_cycle_months = inv.billing_cycle_months
            sub.current_period_start, sub.current_period_end = inv.period_start, inv.period_end
            sub.save()   # بيزامن max_* للشركة
            sub.snapshot_from_plan(revision=inv.plan_revision, save=True)
            _sync_legacy_plan(inv.tenant, inv.plan_revision.plan)
        applied += 1
    return applied


@transaction.atomic
def settle_issued_invoice(invoice, *, provider, reference=None):
    """يحصّل فاتورة اشتراك «مستحقة» (إيصال فودافون/إنستاباي، تحصيل يدوي من لوحة
    المنصة، أو إشعار بوابة الدفع) ويمدّ الاشتراك.

    🐛 [FIX]: الفترة كانت بتتحسب وقت إصدار الفاتورة (طلب الدفع) وبتتطبّق زي ما هي
    وقت التأكيد: فاتورتين مستحقتين لنفس الشركة (طلبت التجديد مرتين، أو الأدمن أصدر
    واحدة والشركة دفعت التانية) كانوا بيغطّوا نفس الشهر — بتدفع مرتين وتاخد شهر
    واحد. ولو التأكيد اتأخر بعد ما الاشتراك خلص، الأيام اللي فاتت كانت بتضيع من
    المدة المدفوعة. دلوقتي الفترة بتبدأ من بعد آخر اشتراك (أو النهارده) وقت التحصيل.

    رقم عملية اتسجّل على فاتورة تانية بيترفض برسالة (قبل كده كان قيد التفرّد
    بيبوّظ الـ transaction والتأكيد يتلغي في صمت والأدمن شايف «✅ اتفعّل»).
    """
    from django.core.exceptions import ValidationError
    from clients.models import PlatformInvoice
    inv = (PlatformInvoice.objects.select_for_update()
           .select_related('plan_revision__plan').get(pk=invoice.pk))
    if inv.status == 'paid':
        return inv
    if inv.status not in ('issued', 'void'):
        raise ValidationError(f"الفاتورة {inv.invoice_number} حالتها «{inv.get_status_display()}» — مش مستحقة.")
    reference = (str(reference).strip() or None) if reference else None
    if reference and (PlatformInvoice.objects.filter(payment_provider=provider, payment_reference=reference)
                      .exclude(pk=inv.pk).exists()):
        raise ValidationError(f"رقم العملية «{reference}» اتسجّل على فاتورة تانية قبل كده — راجع التحويل.")
    tenant = type(inv.tenant).objects.select_for_update().get(pk=inv.tenant_id)
    today = timezone.localdate()
    if inv.kind == 'upgrade':
        # الترقية بتغطي الأيام الفاضلة من المدة الحالية — من النهارده لنفس تاريخ الانتهاء.
        # 🐛 [FIX]: لو التأكيد اتأخر (إيصال اتراجع بعد أيام) العميل مايخسرش الأيام اللي دفع فرقها:
        #    على الأقل نفس عدد أيام التسعيرة من يوم التأكيد.
        quoted_days = max((inv.period_end - inv.period_start).days, 0)
        inv.period_start = today
        inv.period_end = max(tenant.subscription_end_date or today, today + timedelta(days=quoted_days))
    else:
        inv.period_start = max(tenant.subscription_end_date or today, today)
        inv.period_end = inv.period_start + timedelta(days=PERIOD_DAYS.get(inv.billing_cycle_months, 30))
    inv.save(update_fields=['period_start', 'period_end'])
    inv.tenant = tenant
    inv.mark_paid(payment_provider=provider, payment_reference=reference)
    sub = inv.subscription
    if sub is None or sub.plan_id == inv.plan_revision.plan_id:   # التخفيض المؤجّل بيتبدّل يوم تطبيقه
        _sync_legacy_plan(tenant, inv.plan_revision.plan)
    return inv


def void_invoice(invoice, *, reason='', user=None):
    """Cancel an unpaid invoice (e.g. its manual receipt was rejected)."""
    if invoice.status != 'issued':
        return False
    invoice.status = 'void'
    stamp = f"[void {timezone.now():%Y-%m-%d} {getattr(user, 'username', '') or ''}] {reason}".strip()
    invoice.notes = ((invoice.notes + '\n') if invoice.notes else '') + stamp
    invoice.save(update_fields=['status', 'notes'])
    return True


@transaction.atomic
def refund_invoice(invoice, *, reason='', user=None, cut_service=False):
    """Mark a paid invoice refunded. ``cut_service`` ends the subscription today
    (the money went back) — otherwise access stays until the period end."""
    if invoice.status != 'paid':
        return False
    invoice.status = 'refunded'
    stamp = f"[refund {timezone.now():%Y-%m-%d} {getattr(user, 'username', '') or ''}] {reason}".strip()
    invoice.notes = ((invoice.notes + '\n') if invoice.notes else '') + stamp
    invoice.save(update_fields=['status', 'notes'])
    if cut_service:
        tenant = invoice.tenant
        today = timezone.localdate()
        if tenant.subscription_end_date and tenant.subscription_end_date > today:
            tenant.subscription_end_date = today
            tenant.save(update_fields=['subscription_end_date'])
        sub = invoice.subscription
        if sub and sub.current_period_end and sub.current_period_end > today:
            sub.current_period_end = today
            sub.save(update_fields=['current_period_end'])
    return True
