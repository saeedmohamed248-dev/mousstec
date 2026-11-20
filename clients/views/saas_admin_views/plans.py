"""💎 لوحة مالك المنصة — الباقات، النسخ، عروض الخصم، أكواد الخصم والإحالة."""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from datetime import timedelta

from django.contrib import messages
from django.db import transaction
from django.db.models import Sum, Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from clients.models import (
    Plan, PlanRevision, PlatformInvoice,
    TenantSubscription, Feature,
)
from clients.permissions import widget_required
from ._common import _log_event, saas_admin_required

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# 💎 Plan Management — list view
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('plans')
def plan_management_list(request):
    """قائمة بكل الـ Plans + إحصائيات سريعة."""
    plans = Plan.objects.all().order_by('industry', 'sort_order')

    # حساب عدد المشتركين بكل plan
    plan_stats = {}
    sub_counts = (
        TenantSubscription.objects
        .filter(is_active=True)
        .values('plan_id')
        .annotate(n=Count('id'))
    )
    for row in sub_counts:
        plan_stats[row['plan_id']] = row['n']

    enriched = []
    for p in plans:
        latest_rev = PlanRevision.objects.filter(plan=p).order_by('-effective_from').first()
        enriched.append({
            'plan': p,
            'subscribers_count': plan_stats.get(p.id, 0),
            'features_count': len(p.entitlements or {}),
            'revisions_count': p.revisions.count(),
            'latest_revision': latest_rev,
            'promo': p.active_promotion(),
        })

    from clients.models import PlanPromotion
    now = timezone.now()
    return render(request, 'clients/saas_admin/plans_list.html', {
        'plans_data': enriched,
        'features_count_total': Feature.objects.filter(is_active=True).count(),
        'running_promos': PlanPromotion.objects.filter(is_active=True, starts_at__lte=now, ends_at__gt=now).count(),
    })


# ─────────────────────────────────────────────────────────────────────
# 💎 Plan Management — edit view (price + entitlements + rollout)
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('plans')
def plan_management_edit(request, plan_id):
    """
    GET  → عرض الفورم مع state حالي
    POST → save:
        - new monthly_price
        - new entitlements (dict)
        - rollout choice: next_renewal | new_subscribers_only | force_apply
        - لو force_apply: لازم confirm_text='CONFIRM'
    """
    plan = get_object_or_404(Plan, pk=plan_id)
    features = Feature.objects.filter(is_active=True).order_by('category', 'sort_order', 'code')
    active_subs_count = TenantSubscription.objects.filter(plan=plan, is_active=True).count()
    revisions = plan.revisions.all().order_by('-effective_from')[:10]

    if request.method == 'POST':
        return _handle_plan_edit_post(request, plan, features)

    # GET — build form context
    current_entitlements = plan.entitlements or {}
    feature_rows = []
    for f in features:
        config = current_entitlements.get(f.code) or {}
        feature_rows.append({
            'feature': f,
            'enabled': bool(config.get('enabled', False)),
            'monthly_limit': config.get('monthly_limit'),
        })

    return render(request, 'clients/saas_admin/plan_edit.html', {
        'plan': plan,
        'feature_rows': feature_rows,
        'active_subs_count': active_subs_count,
        'revisions': revisions,
        'features_text': '\n'.join(plan.features or []),
        'industry_choices': Plan.INDUSTRY_CHOICES,
        'promotions': plan.promotions.filter(ends_at__gt=timezone.now()).order_by('starts_at'),
    })


# 🆕 بيانات الباقة غير السعر والمزايا (كانت بتتعدّل من Django admin بس)
_PLAN_INT_FIELDS = (
    ('max_users', 1, 10000), ('max_branches', 1, 1000), ('max_treasuries', 1, 1000),
    ('monthly_ai_designs_quota', 0, 100000), ('monthly_diagnostics_scans_quota', 0, 100000),
    ('monthly_diagnostics_bot_quota', 0, 100000),
    ('quarterly_discount', 0, 90), ('semi_annual_discount', 0, 90), ('annual_discount', 0, 90),
    ('sort_order', -1000, 1000),
)
_PLAN_LIMIT_FIELDS = ('max_users', 'max_branches', 'max_treasuries')


def _plain_number(raw):
    """🐛 [FIX]: خانة السعر كانت بتتعرض «550,00» (فاصلة عشرية حسب لغة الصفحة) — الحفظ من غير
    تعديل كان بيقول «السعر غير صالح». بنقبل الفاصلة العشرية والأرقام العربية وفاصل الآلاف."""
    s = (raw or '').strip().translate(str.maketrans('٠١٢٣٤٥٦٧٨٩٫٬', '0123456789.,'))
    if ',' in s and '.' not in s and s.count(',') == 1 and len(s.split(',')[1]) <= 2:
        return s.replace(',', '.')
    return s.replace(',', '')


def _parse_plan_details(request, plan):
    """يقرا خانات البيانات الأساسية/الحدود/الحصص من الفورم ويحطها على ``plan`` (من غير حفظ).
    يرجع رسالة خطأ أو None. الفورم القديم من غير الخانات دي بيسيب القيم زي ما هي."""
    if 'name' not in request.POST:
        return None
    name = (request.POST.get('name') or '').strip()[:80]
    if not name:
        return "🛑 اسم الباقة مطلوب."
    plan.name = name
    plan.name_en = (request.POST.get('name_en') or '').strip()[:80]
    for field, lo, hi in _PLAN_INT_FIELDS:
        raw = (request.POST.get(field) or '').strip()
        if raw == '':
            continue
        try:
            value = int(raw)
        except ValueError:
            return f"🛑 القيمة '{raw}' في {field} لازم تكون رقم صحيح."
        if not lo <= value <= hi:
            return f"🛑 {field} لازم يكون بين {lo} و {hi}."
        setattr(plan, field, value)
    aed = _plain_number(request.POST.get('monthly_price_aed')) or '0'
    try:
        plan.monthly_price_aed = Decimal(aed).quantize(Decimal('0.01'))
        if plan.monthly_price_aed < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return f"🛑 سعر الدرهم '{aed}' غير صالح."
    plan.features = [ln.strip()[:200] for ln in (request.POST.get('features_text') or '').splitlines()
                     if ln.strip()][:30]
    plan.is_active = request.POST.get('is_active') == 'on'
    return None


def _push_limits_to_subscribers(plan, *, force):
    """الحدود الجديدة للمشتركين الحاليين: الزيادة بتوصلهم فوراً (زي ما عملنا في Silver/Starter)،
    والتقليل بس مع «تطبيق فوري» — مانقللش موظفين/فروع حد دافع عليهم من غير قرار صريح."""
    changed = 0
    for sub in TenantSubscription.objects.filter(plan=plan, is_active=True).select_related('tenant'):
        tenant = sub.tenant
        updates = {}
        for field in _PLAN_LIMIT_FIELDS:
            new = getattr(plan, field)
            cur = getattr(tenant, field)
            if (force and cur != new) or (not force and new > cur):
                updates[field] = new
        if updates:
            type(tenant).objects.filter(pk=tenant.pk).update(**updates)
            changed += 1
    return changed


def _handle_plan_edit_post(request, plan, features):
    """يـ process الـ form POST من plan_management_edit."""
    rollout = request.POST.get('rollout', 'next_renewal')
    valid_rollouts = ('next_renewal', 'new_subscribers_only', 'force_apply')
    if rollout not in valid_rollouts:
        messages.error(request, "🛑 خيار الـ rollout غير معروف.")
        return redirect('saas_plan_edit', plan_id=plan.id)

    # 🔴 Force-apply guardrail: لازم نص confirm='CONFIRM' حرفياً
    if rollout == 'force_apply':
        confirm_text = (request.POST.get('confirm_text') or '').strip()
        if confirm_text != 'CONFIRM':
            messages.error(
                request,
                "🛑 لتطبيق التغيير على كل المشتركين الحاليين، لازم تكتب CONFIRM "
                "في خانة التأكيد بحروف كبيرة بالظبط.",
            )
            return redirect('saas_plan_edit', plan_id=plan.id)

    # ── Parse السعر ──
    price_str = _plain_number(request.POST.get('monthly_price'))
    try:
        new_price = Decimal(price_str).quantize(Decimal('0.01'))
        if new_price < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        messages.error(request, f"🛑 السعر '{price_str}' غير صالح.")
        return redirect('saas_plan_edit', plan_id=plan.id)

    # ── Parse الـ entitlements من الـ form ──
    new_entitlements = {}
    for f in features:
        is_enabled = request.POST.get(f'feat_{f.code}_enabled') == 'on'
        if not is_enabled:
            continue
        config = {'enabled': True}
        if f.is_quantitative:
            limit_str = (request.POST.get(f'feat_{f.code}_limit') or '').strip()
            if limit_str:
                try:
                    config['monthly_limit'] = int(limit_str)
                    if config['monthly_limit'] < 0:
                        raise ValueError
                except ValueError:
                    messages.error(
                        request,
                        f"🛑 الحد الشهري للـ '{f.name_ar}' لازم يكون رقم صحيح ≥ 0 (دخلت '{limit_str}').",
                    )
                    return redirect('saas_plan_edit', plan_id=plan.id)
        new_entitlements[f.code] = config

    change_reason = (request.POST.get('change_reason') or '').strip()[:240]
    if not change_reason:
        change_reason = f"Updated via SaaS admin UI ({rollout})"

    # ── Apply ──
    price_changed = plan.monthly_price != new_price
    ent_changed = (plan.entitlements or {}) != new_entitlements

    _detail_fields = ['name', 'name_en', 'monthly_price_aed', 'features', 'is_active'] + [
        f for f, _lo, _hi in _PLAN_INT_FIELDS]
    before = {f: getattr(plan, f) for f in _detail_fields}
    error = _parse_plan_details(request, plan)
    if error:
        messages.error(request, error)
        return redirect('saas_plan_edit', plan_id=plan.id)
    details_changed = any(getattr(plan, f) != before[f] for f in _detail_fields)
    limits_changed = any(getattr(plan, f) != before[f] for f in _PLAN_LIMIT_FIELDS)

    if not price_changed and not ent_changed and not details_changed and rollout != 'force_apply':
        messages.info(request, "ℹ️ مفيش تغييرات لتطبيقها.")
        return redirect('saas_plan_edit', plan_id=plan.id)

    try:
        with transaction.atomic():
            plan.monthly_price = new_price
            plan.entitlements = new_entitlements
            # Validation عبر Plan.clean() — يـ raise ValidationError لو في code غلط
            plan.full_clean()
            # نـ inject author info للـ revision signal
            plan._changed_by = request.user
            plan._change_reason = change_reason
            plan.save()

            # 🔴 Force-apply rollout — يـ re-snapshot كل الـ active subs على الـ revision الجديد
            forced_count = 0
            if rollout == 'force_apply':
                latest_rev = PlanRevision.objects.filter(plan=plan).order_by('-effective_from').first()
                active_subs = TenantSubscription.objects.filter(plan=plan, is_active=True)
                for sub in active_subs:
                    sub.snapshot_from_plan(revision=latest_rev, save=True)
                    forced_count += 1
            limits_pushed = (_push_limits_to_subscribers(plan, force=rollout == 'force_apply')
                             if limits_changed or rollout == 'force_apply' else 0)
            _log_event('other', user=request.user,
                       description=f"💎 تعديل باقة «{plan.name}» ({plan.slug}) — {change_reason}"[:500])
        if limits_pushed:
            messages.info(request, f"👥 الحدود الجديدة اتطبّقت على {limits_pushed} شركة مشتركة في الباقة.")

        if rollout == 'force_apply':
            messages.success(
                request,
                f"🔴 تم تطبيق التغييرات على {forced_count} مشترك نشط على الفور (Force-apply).",
            )
        else:
            label = 'التجديد القادم' if rollout == 'next_renewal' else 'المشتركين الجدد فقط'
            messages.success(
                request,
                f"✅ تم حفظ التغييرات. هتسري على {label}؛ المشتركين الحاليين locked على الأسعار/المزايا القديمة.",
            )
    except Exception as e:
        logger.exception(f"[SaaS Admin] Plan edit failed for plan_id={plan.id}: {e}")
        messages.error(request, f"🛑 حصل خطأ أثناء الحفظ: {e}")

    return redirect('saas_plan_edit', plan_id=plan.id)


# ─────────────────────────────────────────────────────────────────────
# 🆕 باقة جديدة — نسخة من باقة موجودة (مقفولة لحد ما المالك يراجعها ويفعّلها)
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('plans')
@require_POST
def plan_management_clone(request, plan_id):
    from django.utils.text import slugify
    src = get_object_or_404(Plan, pk=plan_id)
    base = slugify(src.slug)[:30] or 'plan'
    slug, n = f"{base}-copy", 2
    while Plan.objects.filter(slug=slug).exists():
        slug, n = f"{base}-copy{n}", n + 1
    fields = {f.name: getattr(src, f.name) for f in Plan._meta.concrete_fields
              if f.name not in ('id', 'slug', 'name', 'name_en', 'is_active')}
    plan = Plan(slug=slug, name=f"{src.name} (نسخة)", name_en='', is_active=False, **fields)
    plan._changed_by = request.user
    plan._change_reason = f"نسخة من {src.slug}"
    plan.save()
    _log_event('other', user=request.user, description=f"🆕 باقة جديدة «{plan.name}» نسخة من {src.slug}")
    messages.success(request, "✅ اتعملت نسخة من الباقة — مقفولة لحد ما تعدّلها وتعلّم «ظاهرة للعملاء».")
    return redirect('saas_plan_edit', plan_id=plan.id)


# ─────────────────────────────────────────────────────────────────────
# 🏷️ عروض الخصم المؤقتة (يوم / أسبوع / 10 أيام / شهر / لحد تاريخ)
# ─────────────────────────────────────────────────────────────────────
_PROMO_DURATIONS = (
    ('1d', 'يوم', timedelta(days=1)), ('3d', '3 أيام', timedelta(days=3)),
    ('7d', 'أسبوع', timedelta(days=7)), ('10d', '10 أيام', timedelta(days=10)),
    ('14d', 'أسبوعين', timedelta(days=14)), ('30d', 'شهر', timedelta(days=30)),
    ('90d', '3 شهور', timedelta(days=90)),
)


def _parse_local_datetime(raw):
    """قيمة datetime-local من الفورم (بتوقيت القاهرة) ⇒ datetime aware."""
    from datetime import datetime
    try:
        naive = datetime.strptime(raw.strip(), '%Y-%m-%dT%H:%M')
    except (ValueError, AttributeError):
        return None
    return timezone.make_aware(naive, timezone.get_current_timezone())


@saas_admin_required
@widget_required('plans')
def promotions_list(request):
    from clients.models import PlanPromotion
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'stop':
            promo = get_object_or_404(PlanPromotion, pk=request.POST.get('promo_id'))
            now = timezone.now()
            PlanPromotion.objects.filter(pk=promo.pk).update(
                is_active=False, ends_at=min(promo.ends_at, now) if promo.starts_at < now else promo.ends_at)
            _log_event('other', user=request.user, description=f"🏷️ إيقاف عرض «{promo}»")
            messages.success(request, "⏹️ العرض اتوقف — الأسعار رجعت زي ما كانت.")
            return redirect('saas_promotions')
        return _create_promotion(request)

    now = timezone.now()
    promos = list(PlanPromotion.objects.select_related('plan', 'created_by').order_by('-created_at')[:100])
    return render(request, 'clients/saas_admin/promotions.html', {
        'running': [p for p in promos if p.is_running],
        'scheduled': [p for p in promos if p.is_active and p.starts_at > now],
        'ended': [p for p in promos if not p.is_running and not (p.is_active and p.starts_at > now)][:30],
        'plans': Plan.objects.filter(is_active=True).order_by('industry', 'sort_order'),
        'industry_choices': Plan.INDUSTRY_CHOICES,
        'durations': _PROMO_DURATIONS,
        'now_local': timezone.localtime(now).strftime('%Y-%m-%dT%H:%M'),
    })


def _create_promotion(request):
    from django.core.exceptions import ValidationError as _VE
    from clients.models import PlanPromotion
    target = (request.POST.get('target') or '').strip()
    plan, industry = None, ''
    if target.startswith('plan:'):
        plan = Plan.objects.filter(pk=target[5:] if target[5:].isdigit() else None).first()
        if plan is None:
            messages.error(request, "🛑 الباقة مش موجودة.")
            return redirect('saas_promotions')
    elif target.startswith('industry:'):
        industry = target[9:]
        if industry not in dict(Plan.INDUSTRY_CHOICES):
            messages.error(request, "🛑 القطاع غير معروف.")
            return redirect('saas_promotions')
    try:
        percent = int(request.POST.get('percent_off') or 0)
    except ValueError:
        percent = 0
    starts = _parse_local_datetime(request.POST.get('starts_at') or '') or timezone.now()
    duration = dict((k, d) for k, _label, d in _PROMO_DURATIONS).get(request.POST.get('duration'))
    ends = starts + duration if duration else _parse_local_datetime(request.POST.get('ends_at') or '')
    if ends is None:
        messages.error(request, "🛑 اختار مدة العرض أو تاريخ النهاية.")
        return redirect('saas_promotions')
    promo = PlanPromotion(plan=plan, industry=industry, percent_off=max(percent, 0),
                          label=(request.POST.get('label') or '').strip()[:80],
                          starts_at=starts, ends_at=ends, created_by=request.user)
    try:
        promo.full_clean()
    except _VE as e:
        messages.error(request, "🛑 " + " ".join(m for msgs in e.message_dict.values() for m in msgs))
        return redirect('saas_promotions')
    promo.save()
    _log_event('other', user=request.user,
               description=f"🏷️ عرض {promo.percent_off}% على {promo.target_label} من "
                           f"{timezone.localtime(starts):%Y-%m-%d %H:%M} لـ {timezone.localtime(ends):%Y-%m-%d %H:%M}")
    messages.success(request, f"✅ العرض اتعمل: خصم {promo.percent_off}% على {promo.target_label}.")
    return redirect('saas_promotions')


# ─────────────────────────────────────────────────────────────────────
# 🎟️ أكواد الخصم + برنامج الإحالة
# ─────────────────────────────────────────────────────────────────────
def pb_referral_percent():
    from clients.services.platform_billing import REFERRAL_DISCOUNT_PERCENT
    return REFERRAL_DISCOUNT_PERCENT


@saas_admin_required
@widget_required('plans')
def coupons_list(request):
    from django.core.exceptions import ValidationError as _VE
    from clients.models import Coupon, ReferralReward
    if request.method == 'POST':
        if request.POST.get('action') == 'toggle':
            c = get_object_or_404(Coupon, pk=request.POST.get('coupon_id'))
            Coupon.objects.filter(pk=c.pk).update(is_active=not c.is_active)
            _log_event('other', user=request.user, description=f"🎟️ {'إيقاف' if c.is_active else 'تفعيل'} الكود {c.code}")
            messages.success(request, f"{'⏸️ اتوقف' if c.is_active else '✅ اتفعّل'} الكود {c.code}.")
            return redirect('saas_coupons')
        if request.POST.get('action') == 'min_months':
            # 🎟️ تغيير أقل مدة لكود موجود (مثال: كود الإطلاق يبقى على 3 شهور أو أكتر بس)
            c = get_object_or_404(Coupon, pk=request.POST.get('coupon_id'))
            m = int(request.POST.get('min_months') or 1) if (request.POST.get('min_months') or '').isdigit() else 1
            if m not in dict(Coupon.MIN_MONTHS_CHOICES):
                m = 1
            Coupon.objects.filter(pk=c.pk).update(min_months=m)
            _log_event('other', user=request.user, description=f"🎟️ الكود {c.code}: أقل مدة {m} شهر")
            messages.success(request, f"✅ الكود {c.code} بقى على: {dict(Coupon.MIN_MONTHS_CHOICES)[m]}.")
            return redirect('saas_coupons')
        target = (request.POST.get('target') or '').strip()
        plan = Plan.objects.filter(pk=target[5:]).first() if target.startswith('plan:') and target[5:].isdigit() else None
        industry = target[9:] if target.startswith('industry:') and target[9:] in dict(Plan.INDUSTRY_CHOICES) else ''
        kind = request.POST.get('kind', 'percent')
        try:
            value = Decimal(_plain_number(request.POST.get('value')) or '0')
            max_uses = int(request.POST.get('max_redemptions') or 0)
        except (InvalidOperation, ValueError):
            messages.error(request, "🛑 القيمة غير صحيحة.")
            return redirect('saas_coupons')
        coupon = Coupon(code=(request.POST.get('code') or '').strip().upper(),
                        label=(request.POST.get('label') or '').strip()[:80], plan=plan, industry=industry,
                        percent_off=int(value) if kind == 'percent' else 0,
                        amount_off=value if kind == 'amount' else 0,
                        starts_at=_parse_local_datetime(request.POST.get('starts_at') or '') or timezone.now(),
                        ends_at=_parse_local_datetime(request.POST.get('ends_at') or ''),
                        max_redemptions=max(max_uses, 0),
                        new_customers_only=request.POST.get('new_customers_only') == 'on',
                        min_months=int(request.POST.get('min_months')) if (request.POST.get('min_months') or '') in ('1', '3', '6', '12') else 1,
                        created_by=request.user)
        if Coupon.objects.filter(code=coupon.code).exists():
            messages.error(request, "🛑 الكود ده موجود قبل كده.")
            return redirect('saas_coupons')
        try:
            coupon.full_clean(exclude=['referrer'])
        except _VE as e:
            messages.error(request, "🛑 " + " ".join(m for msgs in e.message_dict.values() for m in msgs)
                           if hasattr(e, 'message_dict') else "🛑 " + " ".join(e.messages))
            return redirect('saas_coupons')
        coupon.save()
        _log_event('other', user=request.user, description=f"🎟️ كود خصم جديد {coupon.code}")
        messages.success(request, f"✅ الكود {coupon.code} جاهز — ابعته لعملائك أو حطه في إعلانك.")
        return redirect('saas_coupons')

    coupons = (Coupon.objects.filter(referrer__isnull=True).select_related('plan')
               .annotate(uses=Count('invoices', filter=Q(invoices__status='paid')),
                         saved=Sum('invoices__coupon_discount', filter=Q(invoices__status='paid')))
               .order_by('-created_at')[:200])
    top_referrers = (ReferralReward.objects.values('referrer__name', 'referrer__schema_name')
                     .annotate(n=Count('id')).order_by('-n')[:20])
    referral_paid = PlatformInvoice.objects.filter(status='paid', coupon__referrer__isnull=False)
    return render(request, 'clients/saas_admin/coupons.html', {
        'coupons': coupons, 'top_referrers': top_referrers,
        'min_months_choices': Coupon.MIN_MONTHS_CHOICES,
        'referral_stats': {'rewards': ReferralReward.objects.count(), 'paid_invoices': referral_paid.count(),
                           'revenue': referral_paid.aggregate(s=Sum('total'))['s'] or Decimal('0')},
        'plans': Plan.objects.filter(is_active=True).order_by('industry', 'sort_order'),
        'industry_choices': Plan.INDUSTRY_CHOICES,
        'now_local': timezone.localtime().strftime('%Y-%m-%dT%H:%M'),
        'referral_percent': pb_referral_percent(),
    })
