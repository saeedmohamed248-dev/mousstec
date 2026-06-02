"""
🎛️ Phase 5: SaaS Super-Admin Control Center
================================================
3 views للسوبر آدمن:

  - plan_management_list  → /superadmin/plans/
  - plan_management_edit  → /superadmin/plans/<id>/edit/
  - revenue_dashboard     → /superadmin/revenue/

كلها محمية بـ user.is_superuser + public schema check.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.db import connection, transaction
from django.db.models import Sum, Count
from django.http import HttpResponseForbidden, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from clients.models import (
    Client, Plan, PlanRevision, PlatformInvoice,
    TenantSubscription, Feature,
)

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# 🛡️ Security gate — public schema + superuser only
# ─────────────────────────────────────────────────────────────────────
def _saas_admin_required(user):
    return (
        user.is_authenticated
        and user.is_superuser
        and connection.schema_name == 'public'
    )

saas_admin_required = user_passes_test(_saas_admin_required, login_url='/secure-portal/login/')


# ─────────────────────────────────────────────────────────────────────
# 💎 Plan Management — list view
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
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
        })

    return render(request, 'clients/saas_admin/plans_list.html', {
        'plans_data': enriched,
        'features_count_total': Feature.objects.filter(is_active=True).count(),
    })


# ─────────────────────────────────────────────────────────────────────
# 💎 Plan Management — edit view (price + entitlements + rollout)
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
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
    })


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
    price_str = (request.POST.get('monthly_price') or '').strip()
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

    if not price_changed and not ent_changed and rollout != 'force_apply':
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
# 📊 Revenue Dashboard
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
def revenue_dashboard(request):
    """أرقام إيرادات المنصة من PlatformInvoice (status=paid)."""
    today = timezone.localdate()
    start_of_month = today.replace(day=1)
    start_of_30d = today - timedelta(days=30)

    paid_qs = PlatformInvoice.objects.filter(status='paid')

    total_all_time = paid_qs.aggregate(s=Sum('total'))['s'] or Decimal('0.00')
    total_this_month = paid_qs.filter(paid_at__date__gte=start_of_month).aggregate(s=Sum('total'))['s'] or Decimal('0.00')
    total_last_30d = paid_qs.filter(paid_at__date__gte=start_of_30d).aggregate(s=Sum('total'))['s'] or Decimal('0.00')

    invoices_count = paid_qs.count()
    pending_count = PlatformInvoice.objects.filter(status='issued').count()

    # Per-plan breakdown
    by_plan = (
        paid_qs.values('plan_revision__plan__slug', 'plan_revision__plan__name')
        .annotate(total=Sum('total'), count=Count('id'))
        .order_by('-total')
    )

    recent_invoices = (
        PlatformInvoice.objects.select_related('tenant', 'plan_revision', 'plan_revision__plan')
        .order_by('-issued_at')[:25]
    )

    return render(request, 'clients/saas_admin/revenue_dashboard.html', {
        'total_all_time': total_all_time,
        'total_this_month': total_this_month,
        'total_last_30d': total_last_30d,
        'invoices_count': invoices_count,
        'pending_count': pending_count,
        'by_plan': by_plan,
        'recent_invoices': recent_invoices,
    })
