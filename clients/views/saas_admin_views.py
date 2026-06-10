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
    TenantSubscription, Feature, SystemErrorLog,
)
from clients.permissions import get_user_widgets, widget_required

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# 🛡️ Security gate — public schema + superuser only
# ─────────────────────────────────────────────────────────────────────
def _saas_admin_required(user):
    """يسمح بالـ superuser أو أي موظف عنده StaffRole في public schema."""
    if not (user.is_authenticated and connection.schema_name == 'public'):
        return False
    if user.is_superuser:
        return True
    return hasattr(user, 'staff_role')

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


# ─────────────────────────────────────────────────────────────────────
# 🔧 Smart Diagnostics — Cross-Tenant API Spend Analytics
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
def diagnostics_spend_dashboard(request):
    """Aggregate Smart Diagnostics APICallLog across all tenant schemas.

    Walks every tenant via django_tenants schema_context (this is the
    correct way — APICallLog lives per-schema). Caches results for 5min
    so we don't melt the DB if a SuperAdmin refreshes the page.
    """
    from django.core.cache import cache
    from django.db.models import Q
    from django_tenants.utils import schema_context
    from collections import defaultdict
    import json

    CACHE_KEY = 'diag_spend_dashboard_v1'
    CACHE_TTL = 300  # 5 minutes

    cached = cache.get(CACHE_KEY)
    if cached and not request.GET.get('refresh'):
        ctx = cached
    else:
        # Lazy imports — these models live in non-shared apps that may be
        # absent in some deploys.
        try:
            from smart_diagnostics.models import APICallLog
        except Exception:
            return render(request, 'clients/saas_admin/diag_spend.html', {
                'unavailable': True,
            })

        today = timezone.localdate()
        start_of_30d = today - timedelta(days=30)
        start_of_24h = timezone.now() - timedelta(hours=24)

        # Per-tenant aggregates
        per_tenant = []  # list of dicts
        daily_buckets = defaultdict(lambda: {'cost': Decimal('0'), 'calls': 0, 'cache_hits': 0})

        all_tenants = (
            Client.objects.exclude(schema_name='public')
            .filter(status__in=['active', 'trial', 'grace'])
            .only('id', 'schema_name', 'name')
        )

        global_total_cost = Decimal('0')
        global_calls = 0
        global_cache_hits = 0

        for tenant in all_tenants:
            try:
                with schema_context(tenant.schema_name):
                    qs = APICallLog.objects.all()
                    total_cost = qs.aggregate(s=Sum('cost_usd'))['s'] or Decimal('0')
                    calls = qs.count()
                    cache_hits = qs.filter(cache_hit=True).count()
                    last_30d_cost = (
                        qs.filter(timestamp__gte=start_of_30d).aggregate(s=Sum('cost_usd'))['s']
                        or Decimal('0')
                    )
                    last_24h_calls = qs.filter(timestamp__gte=start_of_24h).count()
                    last_call = qs.order_by('-timestamp').values_list('timestamp', flat=True).first()

                    if calls == 0:
                        continue  # tenant never used the diagnostics module

                    per_tenant.append({
                        'tenant_id': tenant.id,
                        'tenant_name': tenant.name,
                        'schema': tenant.schema_name,
                        'total_cost': total_cost,
                        'calls': calls,
                        'cache_hits': cache_hits,
                        'cache_hit_pct': round((cache_hits * 100.0 / calls), 1) if calls else 0,
                        'last_30d_cost': last_30d_cost,
                        'last_24h_calls': last_24h_calls,
                        'last_call': last_call,
                    })

                    global_total_cost += total_cost
                    global_calls += calls
                    global_cache_hits += cache_hits

                    # Daily cost trend (last 30 days, paid calls only)
                    paid_qs = qs.filter(timestamp__gte=start_of_30d, cache_hit=False)
                    for row in paid_qs.values('timestamp__date').annotate(s=Sum('cost_usd'), n=Count('id')):
                        day = row['timestamp__date']
                        daily_buckets[day]['cost'] += row['s'] or Decimal('0')
                        daily_buckets[day]['calls'] += row['n']
            except Exception as e:
                logger.warning(f"[DiagSpend] skip tenant {tenant.schema_name}: {e}")

        per_tenant.sort(key=lambda r: r['total_cost'], reverse=True)
        global_cache_pct = (
            round(global_cache_hits * 100.0 / global_calls, 1) if global_calls else 0
        )

        # Build daily series (fill missing days with 0 for a continuous chart)
        days = [(start_of_30d + timedelta(days=i)) for i in range(31)]
        daily_series = [
            {
                'date': d.isoformat(),
                'cost': float(daily_buckets[d]['cost']),
                'calls': daily_buckets[d]['calls'],
            }
            for d in days
        ]

        ctx = {
            'global_total_cost': global_total_cost,
            'global_calls': global_calls,
            'global_cache_hits': global_cache_hits,
            'global_cache_pct': global_cache_pct,
            'tenants_using': len(per_tenant),
            'top_consumers': per_tenant[:10],
            'all_tenants': per_tenant,
            'daily_series_json': json.dumps(daily_series),
            'unavailable': False,
        }
        cache.set(CACHE_KEY, ctx, CACHE_TTL)

    return render(request, 'clients/saas_admin/diag_spend.html', ctx)


# ─────────────────────────────────────────────────────────────────────
# 🏢 Tenants Management — Soft Delete / Restore / Force Delete
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('tenants')
def tenants_list(request):
    """قائمة كل المستأجرين — مع تبويب للأحياء والمحذوفين."""
    show = request.GET.get('show', 'alive')   # alive | deleted | all
    qs = Client.all_objects.exclude(schema_name='public')
    if show == 'alive':
        qs = qs.filter(is_deleted=False)
    elif show == 'deleted':
        qs = qs.filter(is_deleted=True)
    qs = qs.order_by('-created_on')

    return render(request, 'clients/saas_admin/tenants_list.html', {
        'tenants': qs,
        'show': show,
        'count_alive': Client.all_objects.exclude(schema_name='public').filter(is_deleted=False).count(),
        'count_deleted': Client.all_objects.exclude(schema_name='public').filter(is_deleted=True).count(),
    })


@saas_admin_required
def tenant_soft_delete(request, tenant_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    tenant = get_object_or_404(Client.all_objects, pk=tenant_id)
    if tenant.schema_name == 'public':
        return HttpResponseForbidden("Cannot delete public schema.")
    reason = request.POST.get('reason', '').strip()[:255]
    tenant.soft_delete(user=request.user, reason=reason)
    logger.warning("Tenant SOFT-DELETED id=%s name=%s by=%s reason=%s",
                   tenant.id, tenant.name, request.user.username, reason)
    messages.success(request, f"تم إخفاء المستأجر «{tenant.name}» (Soft Delete). البيانات التاريخية محفوظة.")
    return redirect('saas_tenants_list')


@saas_admin_required
def tenant_restore(request, tenant_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    tenant = get_object_or_404(Client.all_objects, pk=tenant_id)
    tenant.restore()
    logger.warning("Tenant RESTORED id=%s name=%s by=%s", tenant.id, tenant.name, request.user.username)
    messages.success(request, f"تم استعادة «{tenant.name}». الحالة الآن: «معلق» — فعّله يدوياً.")
    return redirect('saas_tenants_list')


@saas_admin_required
def tenant_force_delete(request, tenant_id):
    """
    💀 خطر: حذف فعلي. متاح لـ is_superuser فقط (أو staff_role.can_force_delete=True).
    يتطلب تأكيد بـ POST + كتابة اسم المستأجر.
    """
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    # 🛡️ Force-Delete gate صارم: superuser فقط أو موظف معطى can_force_delete=True
    u = request.user
    role_obj = getattr(u, 'staff_role', None)
    can_force = u.is_superuser or (role_obj and role_obj.can_force_delete)
    if not can_force:
        return HttpResponseForbidden("Force Delete restricted to ultimate owner.")
    tenant = get_object_or_404(Client.all_objects, pk=tenant_id)
    if tenant.schema_name == 'public':
        return HttpResponseForbidden("Cannot delete public schema.")
    confirm = request.POST.get('confirm_name', '').strip()
    if confirm != tenant.name:
        messages.error(request, "اسم التأكيد لا يطابق. لم يتم الحذف.")
        return redirect('saas_tenants_list')
    name = tenant.name
    logger.critical("Tenant FORCE-DELETED id=%s name=%s by=%s", tenant.id, name, request.user.username)
    tenant.force_delete(user=request.user)
    messages.warning(request, f"تم الحذف النهائي لـ «{name}» من قاعدة البيانات.")
    return redirect('saas_tenants_list')


# ─────────────────────────────────────────────────────────────────────
# 🚨 System Error Log Viewer
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('errors')
def system_errors_list(request):
    qs = SystemErrorLog.objects.all()
    show = request.GET.get('show', 'open')  # open | resolved | all
    if show == 'open':
        qs = qs.filter(is_resolved=False)
    elif show == 'resolved':
        qs = qs.filter(is_resolved=True)

    level = request.GET.get('level')
    if level in ('warning', 'error', 'critical'):
        qs = qs.filter(level=level)

    tenant_schema = request.GET.get('tenant')
    if tenant_schema:
        qs = qs.filter(tenant_schema=tenant_schema)

    qs = qs.order_by('-created_at')[:200]

    return render(request, 'clients/saas_admin/system_errors.html', {
        'errors': qs,
        'show': show,
        'level': level,
        'tenant_schema': tenant_schema,
        'count_open': SystemErrorLog.objects.filter(is_resolved=False).count(),
        'count_critical': SystemErrorLog.objects.filter(is_resolved=False, level='critical').count(),
    })


@saas_admin_required
def system_error_resolve(request, error_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    err = get_object_or_404(SystemErrorLog, pk=error_id)
    err.is_resolved = True
    err.resolved_by = request.user
    err.resolved_at = timezone.now()
    err.save(update_fields=['is_resolved', 'resolved_by', 'resolved_at'])
    messages.success(request, "تم تعليم الخطأ كمحلول.")
    return redirect('saas_system_errors')
