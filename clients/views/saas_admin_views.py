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
from django.db import models
from django.db.models import Sum, Count, Q
from django.http import HttpResponseForbidden, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from clients.models import (
    Client, Plan, PlanRevision, PlatformInvoice,
    TenantSubscription, Feature, SystemErrorLog,
    PartListing, DisputeTicket, PlatformEvent, BroadcastCampaign,
)
from clients.permissions import get_user_widgets, widget_required

logger = logging.getLogger('mouss_tec_core')


def _log_event(event_type, *, tenant=None, user=None, description=''):
    """تسجيل حدث في PlatformEvent بأمان — لا يفشل أبداً."""
    try:
        PlatformEvent.objects.create(
            event_type=event_type,
            tenant_schema=getattr(tenant, 'schema_name', '') or '',
            tenant_name=getattr(tenant, 'name', '') or '',
            user_name=getattr(user, 'username', '') or '',
            description=description[:500],
        )
    except Exception:
        logger.exception("PlatformEvent log failed (event_type=%s)", event_type)


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
    _log_event(
        'suspension', tenant=tenant, user=request.user,
        description=f"Soft-delete «{tenant.name}» — السبب: {reason or 'لم يُذكر'}",
    )
    messages.success(request, f"تم إخفاء المستأجر «{tenant.name}» (Soft Delete). البيانات التاريخية محفوظة.")
    return redirect('saas_tenants_list')


@saas_admin_required
def tenant_restore(request, tenant_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    tenant = get_object_or_404(Client.all_objects, pk=tenant_id)
    tenant.restore()
    logger.warning("Tenant RESTORED id=%s name=%s by=%s", tenant.id, tenant.name, request.user.username)
    _log_event(
        'other', tenant=tenant, user=request.user,
        description=f"استعادة «{tenant.name}» من Soft-delete",
    )
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
    _log_event(
        'suspension', tenant=tenant, user=request.user,
        description=f"💀 Force-delete نهائي لـ «{name}» من قاعدة البيانات",
    )
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


# ─────────────────────────────────────────────────────────────────────
# 🛡️ Part Listings — moderation queue (admin approves before going live)
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
def parts_moderation_queue(request):
    """قائمة كل قطع الغيار اللي بتنتظر مراجعة الإدارة."""
    show = request.GET.get('show', 'pending')  # pending | rejected | all
    qs = PartListing.objects.filter(is_deleted=False).select_related(
        'car_make', 'seller_customer', 'seller_tenant',
    ).order_by('-created_at')
    if show == 'pending':
        qs = qs.filter(moderation_status='pending_approval')
    elif show == 'rejected':
        qs = qs.filter(moderation_status='rejected')
    # `all` → no extra filter
    return render(request, 'clients/saas_admin/parts_moderation_queue.html', {
        'listings': qs[:200],
        'show': show,
        'count_pending': PartListing.objects.filter(
            is_deleted=False, moderation_status='pending_approval',
        ).count(),
    })


@saas_admin_required
def parts_moderation_approve(request, listing_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    listing = get_object_or_404(
        PartListing.objects.filter(is_deleted=False), pk=listing_id,
    )
    listing.approve(by_user=request.user)
    _log_event(
        'other', tenant=getattr(listing, 'seller_tenant', None), user=request.user,
        description=f"اعتماد قطعة غيار «{listing.title[:60]}» (id={listing.id})",
    )
    messages.success(request, f"تم اعتماد القطعة «{listing.title[:40]}».")
    return redirect('saas_parts_moderation_queue')


# ─────────────────────────────────────────────────────────────────────
# 🛡️ Part Listings — Active/Live marketplace control (Phase 3 #1)
#   Lists every approved & non-deleted listing with row actions:
#     • Edit  (admin override)
#     • Suspend (approved → suspended)
#     • Soft Delete (is_deleted=True)
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
def parts_active_listings(request):
    """Live marketplace — every listing currently visible to buyers."""
    q = (request.GET.get('q') or '').strip()
    qs = (
        PartListing.objects.filter(
            is_deleted=False, moderation_status='approved',
        )
        .select_related('car_make', 'seller_customer', 'seller_tenant')
        .order_by('-created_at')
    )
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(part_number__icontains=q)
            | Q(seller_tenant__name__icontains=q)
        )

    total_active = PartListing.objects.filter(
        is_deleted=False, moderation_status='approved',
    ).count()
    total_suspended = PartListing.objects.filter(
        is_deleted=False, moderation_status='suspended',
    ).count()
    return render(request, 'clients/saas_admin/parts_active_listings.html', {
        'listings': qs[:300],
        'q': q,
        'total_active': total_active,
        'total_suspended': total_suspended,
    })


@saas_admin_required
def parts_listing_suspend(request, listing_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    listing = get_object_or_404(
        PartListing.objects.filter(is_deleted=False), pk=listing_id,
    )
    reason = (request.POST.get('reason') or '').strip()[:240]
    listing.moderation_status = 'suspended'
    listing.moderated_by = request.user
    listing.moderated_at = timezone.now()
    if hasattr(listing, 'rejection_reason'):
        listing.rejection_reason = reason or 'Suspended by admin'
    listing.save(update_fields=[
        'moderation_status', 'moderated_by', 'moderated_at', 'rejection_reason',
    ])
    logger.warning(
        "PartListing SUSPENDED id=%s by=%s reason=%s",
        listing.id, request.user.username, reason,
    )
    _log_event(
        'suspension', tenant=getattr(listing, 'seller_tenant', None), user=request.user,
        description=f"تعليق قطعة «{listing.title[:60]}» (id={listing.id}) — {reason or 'بدون سبب'}",
    )
    messages.success(request, f"تم تعليق القطعة «{listing.title[:40]}».")
    return redirect('saas_parts_active_listings')


@saas_admin_required
def parts_listing_soft_delete(request, listing_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    listing = get_object_or_404(
        PartListing.objects.filter(is_deleted=False), pk=listing_id,
    )
    if hasattr(listing, 'soft_delete'):
        listing.soft_delete(user=request.user, reason='Admin removed via active-listings table')
    else:
        listing.is_deleted = True
        listing.save(update_fields=['is_deleted'])
    logger.warning("PartListing SOFT-DELETED id=%s by=%s", listing.id, request.user.username)
    _log_event(
        'suspension', tenant=getattr(listing, 'seller_tenant', None), user=request.user,
        description=f"حذف قطعة «{listing.title[:60]}» (id={listing.id}) من السوق",
    )
    messages.success(request, f"تم حذف القطعة «{listing.title[:40]}».")
    return redirect('saas_parts_active_listings')


@saas_admin_required
def parts_listing_edit(request, listing_id):
    """Admin-side edit form for a single listing — covers the critical fields
    most often abused (title/price/qty/description). Heavier brand/model edits
    still go through the regular seller workflow."""
    listing = get_object_or_404(
        PartListing.objects.filter(is_deleted=False), pk=listing_id,
    )
    EDITABLE_FIELDS = ('title', 'part_number', 'price_egp', 'warranty_days', 'description', 'condition', 'city')

    if request.method == 'POST':
        for f in EDITABLE_FIELDS:
            if f in request.POST and hasattr(listing, f):
                raw = request.POST.get(f, '').strip()
                if f == 'price_egp':
                    try:
                        setattr(listing, f, Decimal(raw))
                    except (InvalidOperation, ValueError):
                        messages.error(request, f"🛑 قيمة السعر غير صالحة: {raw}")
                        return redirect('saas_parts_listing_edit', listing_id=listing.id)
                elif f == 'warranty_days':
                    try:
                        setattr(listing, f, int(raw))
                    except ValueError:
                        messages.error(request, f"🛑 فترة الضمان غير صالحة: {raw}")
                        return redirect('saas_parts_listing_edit', listing_id=listing.id)
                else:
                    setattr(listing, f, raw)
        try:
            listing.full_clean()
            listing.save()
            messages.success(request, "تم حفظ التعديلات.")
            return redirect('saas_parts_active_listings')
        except Exception as e:
            messages.error(request, f"🛑 خطأ في الحفظ: {e}")
            return redirect('saas_parts_listing_edit', listing_id=listing.id)

    return render(request, 'clients/saas_admin/parts_listing_edit.html', {
        'listing': listing,
        'editable_fields': EDITABLE_FIELDS,
    })


# ─────────────────────────────────────────────────────────────────────
# 🔧 OBD / Diagnostics-Room paid add-on — grant UI (Phase 3 #3)
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
def obd_access_list(request):
    """List tenants and their OBD-access state, with quick filters."""
    show = request.GET.get('show', 'all')  # all | active | inactive | expiring
    qs = Client.objects.exclude(schema_name='public').filter(is_deleted=False)
    now = timezone.now()
    if show == 'active':
        qs = qs.filter(has_obd_access=True).filter(
            models.Q(obd_access_expiry__isnull=True)
            | models.Q(obd_access_expiry__gt=now)
        )
    elif show == 'inactive':
        from django.db.models import Q
        qs = qs.filter(
            Q(has_obd_access=False)
            | Q(obd_access_expiry__lte=now, obd_access_expiry__isnull=False)
        )
    elif show == 'expiring':
        soon = now + timedelta(days=7)
        qs = qs.filter(
            has_obd_access=True,
            obd_access_expiry__gt=now,
            obd_access_expiry__lte=soon,
        )
    qs = qs.order_by('-has_obd_access', 'obd_access_expiry', 'name')

    rows = []
    for c in qs[:300]:
        if c.has_obd_access and c.obd_access_expiry is None:
            state = 'lifetime'
        elif c.obd_access_is_valid:
            state = 'active'
        elif c.has_obd_access and c.obd_access_expiry and c.obd_access_expiry <= now:
            state = 'expired'
        else:
            state = 'inactive'
        rows.append({'client': c, 'state': state})

    return render(request, 'clients/saas_admin/obd_access_list.html', {
        'rows': rows,
        'show': show,
        'now': now,
    })


_OBD_DURATIONS = {
    '1d':  timedelta(days=1),
    '1w':  timedelta(days=7),
    '1m':  timedelta(days=30),
    '3m':  timedelta(days=90),
    '6m':  timedelta(days=180),
    '1y':  timedelta(days=365),
    'lifetime': None,
}


@saas_admin_required
def obd_access_grant(request, tenant_id):
    """Grant or extend OBD access. Quick-duration via POST['duration']."""
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    tenant = get_object_or_404(
        Client.objects.exclude(schema_name='public'), pk=tenant_id,
    )
    key = (request.POST.get('duration') or '').strip()
    if key not in _OBD_DURATIONS:
        messages.error(request, f"🛑 المدة غير معروفة: {key}")
        return redirect('saas_obd_access_list')
    delta = _OBD_DURATIONS[key]
    tenant.grant_obd_access(delta, by_user=request.user)
    label = 'مدى الحياة' if delta is None else key
    logger.warning(
        "OBD access GRANTED tenant=%s duration=%s by=%s",
        tenant.schema_name, label, request.user.username,
    )
    _log_event(
        'other', tenant=tenant, user=request.user,
        description=f"🎁 منح OBD لـ «{tenant.name}» لمدة {label}",
    )
    messages.success(request, f"✅ تم منح وصول OBD لـ «{tenant.name}» ({label}).")
    return redirect('saas_obd_access_list')


@saas_admin_required
def obd_access_revoke(request, tenant_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    tenant = get_object_or_404(
        Client.objects.exclude(schema_name='public'), pk=tenant_id,
    )
    tenant.revoke_obd_access(by_user=request.user)
    logger.warning(
        "OBD access REVOKED tenant=%s by=%s",
        tenant.schema_name, request.user.username,
    )
    _log_event(
        'suspension', tenant=tenant, user=request.user,
        description=f"سحب وصول OBD من «{tenant.name}»",
    )
    messages.success(request, f"تم سحب وصول OBD من «{tenant.name}».")
    return redirect('saas_obd_access_list')


@saas_admin_required
def parts_moderation_reject(request, listing_id):
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    listing = get_object_or_404(
        PartListing.objects.filter(is_deleted=False), pk=listing_id,
    )
    import json as _json
    try:
        body = _json.loads(request.body)
        reason = (body.get('reason') or '').strip()
    except Exception:
        reason = (request.POST.get('reason') or '').strip()
    listing.reject(by_user=request.user, reason=reason)
    _log_event(
        'other', tenant=getattr(listing, 'seller_tenant', None), user=request.user,
        description=f"رفض قطعة «{listing.title[:60]}» (id={listing.id}) — {reason or 'بدون سبب'}",
    )
    if request.headers.get('Content-Type', '').startswith('application/json') or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        from django.http import JsonResponse
        return JsonResponse({'message': f"تم رفض القطعة «{listing.title[:40]}»."})
    messages.success(request, f"تم رفض القطعة «{listing.title[:40]}».")
    return redirect('saas_parts_moderation_queue')


# ─────────────────────────────────────────────────────────────────────
# ⚖️ Dispute resolution centre
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
def disputes_queue(request):
    """All open / under-review dispute tickets."""
    show = request.GET.get('show', 'open')
    qs = DisputeTicket.objects.filter(is_deleted=False).select_related(
        'order', 'order__listing', 'opened_by_customer', 'opened_by_tenant',
    ).order_by('-opened_at')
    if show == 'open':
        qs = qs.filter(status__in=['open', 'under_review'])
    elif show == 'resolved':
        qs = qs.filter(status__in=['resolved_refund', 'resolved_release', 'resolved_split'])
    return render(request, 'clients/saas_admin/disputes_queue.html', {
        'tickets': qs[:200],
        'show': show,
        'count_open': DisputeTicket.objects.filter(
            is_deleted=False, status__in=['open', 'under_review'],
        ).count(),
    })


@saas_admin_required
def dispute_resolve(request, ticket_id):
    """POST endpoint — action ∈ {refund, release, split, cancel}."""
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")
    ticket = get_object_or_404(
        DisputeTicket.objects.filter(is_deleted=False).select_related('order'),
        pk=ticket_id,
    )
    action = (request.POST.get('action') or '').strip()
    notes = (request.POST.get('notes') or '').strip()
    from clients.services import disputes as dispute_svc
    from django.core.exceptions import ValidationError as DjVE
    try:
        if action == 'refund':
            reason = (request.POST.get('return_reason') or 'defective').strip()
            dispute_svc.resolve_with_refund(
                ticket, return_reason=reason, by_user=request.user, notes=notes,
            )
        elif action == 'release':
            dispute_svc.resolve_with_release(
                ticket, by_user=request.user, notes=notes,
            )
        elif action == 'split':
            from decimal import Decimal as _D
            amt = _D(request.POST.get('refund_amount') or '0')
            reason = (request.POST.get('return_reason') or 'not_as_described').strip()
            dispute_svc.resolve_with_split(
                ticket, refund_amount=amt, return_reason=reason,
                by_user=request.user, notes=notes,
            )
        elif action == 'cancel':
            dispute_svc.cancel_dispute(ticket, by_role=ticket.opened_by_role, notes=notes)
        else:
            messages.error(request, f"إجراء غير معروف: {action}")
            return redirect('saas_disputes_queue')
    except DjVE as exc:
        messages.error(request, '; '.join(exc.messages) if hasattr(exc, 'messages') else str(exc))
        return redirect('saas_disputes_queue')
    _log_event(
        'other', tenant=getattr(ticket, 'opened_by_tenant', None), user=request.user,
        description=f"⚖️ نزاع #{ticket.id} → إجراء «{action}»" + (f" — {notes}" if notes else ''),
    )
    messages.success(request, f"تم تنفيذ '{action}' على التذكرة.")
    return redirect('saas_disputes_queue')


# ─────────────────────────────────────────────────────────────────────
# 🛍️ Marketplace Requests — full management (all statuses)
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
def marketplace_requests_list(request):
    """قائمة كل طلبات السوق مع فلتر بالحالة + بحث."""
    from clients.models import ServiceRequest
    from django.http import JsonResponse
    import json

    status_filter = request.GET.get('status', '')
    sector_filter = request.GET.get('sector', '')
    q = request.GET.get('q', '').strip()

    qs = ServiceRequest.objects.select_related('customer').order_by('-created_at')
    if status_filter:
        qs = qs.filter(status=status_filter)
    if sector_filter:
        qs = qs.filter(sector=sector_filter)
    if q:
        qs = qs.filter(
            Q(title__icontains=q) | Q(customer__full_name__icontains=q) | Q(customer__phone__icontains=q)
        )

    # Handle AJAX status-change
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except Exception:
            data = {}
        action = data.get('action')
        req_id = data.get('id')
        if not req_id:
            return JsonResponse({'error': 'id مطلوب'}, status=400)
        svc = get_object_or_404(ServiceRequest, pk=req_id)
        from datetime import timedelta as td
        from django.utils import timezone as tz

        if action == 'approve':
            svc.status = 'open'
            svc.is_approved = True
            expiry_map = {'urgent': 1, 'soon': 3, 'normal': 7}
            days = expiry_map.get(svc.urgency, 7)
            svc.expires_at = tz.now() + td(days=days)
            svc.save(update_fields=['status', 'is_approved', 'expires_at'])
            return JsonResponse({'status': 'ok', 'message': 'تم الموافقة ونشره للتجار.'})
        elif action == 'reject':
            svc.status = 'rejected_by_admin'
            svc.admin_notes = data.get('reason', '')
            svc.save(update_fields=['status', 'admin_notes'])
            return JsonResponse({'status': 'ok', 'message': 'تم رفض الطلب.'})
        elif action == 'close':
            svc.status = 'closed'
            svc.save(update_fields=['status'])
            return JsonResponse({'status': 'ok', 'message': 'تم إغلاق الطلب.'})
        elif action == 'reopen':
            svc.status = 'pending_approval'
            svc.save(update_fields=['status'])
            return JsonResponse({'status': 'ok', 'message': 'تم إعادة فتح الطلب للمراجعة.'})
        elif action == 'set_open':
            svc.status = 'open'
            svc.is_approved = True
            svc.save(update_fields=['status', 'is_approved'])
            return JsonResponse({'status': 'ok', 'message': 'تم تفعيل الطلب مباشرة.'})
        else:
            return JsonResponse({'error': 'إجراء غير معروف'}, status=400)

    counts = {
        'all': ServiceRequest.objects.count(),
        'pending_approval': ServiceRequest.objects.filter(status='pending_approval').count(),
        'open': ServiceRequest.objects.filter(status='open').count(),
        'accepted': ServiceRequest.objects.filter(status='accepted').count(),
        'completed': ServiceRequest.objects.filter(status='completed').count(),
        'rejected': ServiceRequest.objects.filter(status='rejected_by_admin').count(),
        'closed': ServiceRequest.objects.filter(status='closed').count(),
        'expired': ServiceRequest.objects.filter(status='expired').count(),
    }

    return render(request, 'clients/saas_admin/marketplace_requests.html', {
        'requests': qs[:200],
        'counts': counts,
        'status_filter': status_filter,
        'sector_filter': sector_filter,
        'q': q,
        'status_choices': [
            ('', 'كل الطلبات'),
            ('pending_approval', 'في انتظار الموافقة'),
            ('open', 'مفتوح للتجار'),
            ('accepted', 'تم قبول عرض'),
            ('completed', 'مكتمل'),
            ('rejected_by_admin', 'مرفوض'),
            ('closed', 'مغلق'),
            ('expired', 'منتهي الصلاحية'),
        ],
    })


# ─────────────────────────────────────────────────────────────────────
# 📜 Audit Log — full PlatformEvent viewer
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
def audit_log_list(request):
    """
    سجل أحداث المنصة الكامل (PlatformEvent) — فلاتر + بحث + pagination.
    GET params:
      - event_type, tenant, user → exact match
      - q → بحث في description
      - from, to → نطاق زمني (YYYY-MM-DD)
      - page
    """
    from django.core.paginator import Paginator
    from datetime import datetime

    qs = PlatformEvent.objects.all().order_by('-timestamp')

    event_type = (request.GET.get('event_type') or '').strip()
    tenant = (request.GET.get('tenant') or '').strip()
    user_q = (request.GET.get('user') or '').strip()
    q = (request.GET.get('q') or '').strip()
    date_from = (request.GET.get('from') or '').strip()
    date_to = (request.GET.get('to') or '').strip()

    if event_type:
        qs = qs.filter(event_type=event_type)
    if tenant:
        qs = qs.filter(tenant_schema__iexact=tenant)
    if user_q:
        qs = qs.filter(user_name__icontains=user_q)
    if q:
        qs = qs.filter(
            Q(description__icontains=q)
            | Q(tenant_name__icontains=q)
            | Q(user_name__icontains=q)
        )
    if date_from:
        try:
            d = datetime.strptime(date_from, '%Y-%m-%d').date()
            qs = qs.filter(timestamp__date__gte=d)
        except ValueError:
            pass
    if date_to:
        try:
            d = datetime.strptime(date_to, '%Y-%m-%d').date()
            qs = qs.filter(timestamp__date__lte=d)
        except ValueError:
            pass

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get('page'))

    # تجميعات سريعة للسايد بانل
    total = PlatformEvent.objects.count()
    by_type = (
        PlatformEvent.objects.values('event_type')
        .annotate(n=Count('id')).order_by('-n')
    )
    top_tenants = (
        PlatformEvent.objects.exclude(tenant_schema='')
        .values('tenant_schema', 'tenant_name')
        .annotate(n=Count('id')).order_by('-n')[:10]
    )
    top_users = (
        PlatformEvent.objects.exclude(user_name='')
        .values('user_name').annotate(n=Count('id')).order_by('-n')[:10]
    )

    return render(request, 'clients/saas_admin/audit_log.html', {
        'page_obj': page_obj,
        'filters': {
            'event_type': event_type, 'tenant': tenant, 'user': user_q,
            'q': q, 'from': date_from, 'to': date_to,
        },
        'event_types': PlatformEvent.EVENT_TYPES,
        'total': total,
        'filtered_count': qs.count(),
        'by_type': list(by_type),
        'top_tenants': list(top_tenants),
        'top_users': list(top_users),
    })


# ─────────────────────────────────────────────────────────────────────
# ⚙️ System Status — DB / Redis / Celery / app health
# ─────────────────────────────────────────────────────────────────────
def _check_database():
    """Ping the DB with `SELECT 1` and time it."""
    from django.db import connection as _conn
    import time
    t0 = time.perf_counter()
    try:
        with _conn.cursor() as cur:
            cur.execute('SELECT 1')
            cur.fetchone()
        ms = (time.perf_counter() - t0) * 1000
        if ms > 500:
            return {'name': 'PostgreSQL', 'status': 'degraded', 'detail': f'{ms:.0f}ms (بطيء)', 'icon': 'database'}
        return {'name': 'PostgreSQL', 'status': 'ok', 'detail': f'{ms:.1f}ms', 'icon': 'database'}
    except Exception as e:
        return {'name': 'PostgreSQL', 'status': 'down', 'detail': str(e)[:120], 'icon': 'database'}


def _check_cache():
    """Round-trip set/get from the default cache backend."""
    from django.core.cache import cache
    import time, secrets
    key = f'__health_probe_{secrets.token_hex(4)}__'
    val = secrets.token_hex(8)
    t0 = time.perf_counter()
    try:
        cache.set(key, val, 10)
        got = cache.get(key)
        ms = (time.perf_counter() - t0) * 1000
        if got != val:
            return {'name': 'Redis Cache', 'status': 'degraded', 'detail': 'set/get mismatch', 'icon': 'bolt'}
        cache.delete(key)
        if ms > 200:
            return {'name': 'Redis Cache', 'status': 'degraded', 'detail': f'{ms:.0f}ms (بطيء)', 'icon': 'bolt'}
        return {'name': 'Redis Cache', 'status': 'ok', 'detail': f'{ms:.1f}ms', 'icon': 'bolt'}
    except Exception as e:
        return {'name': 'Redis Cache', 'status': 'down', 'detail': str(e)[:120], 'icon': 'bolt'}


def _check_celery():
    """Ping live Celery workers."""
    try:
        from erp_core.celery import app as celery_app
    except Exception:
        try:
            from celery import current_app as celery_app
        except Exception as e:
            return {'name': 'Celery Workers', 'status': 'unknown', 'detail': f'لا يمكن استيراد celery: {e}', 'icon': 'gears'}
    try:
        replies = celery_app.control.ping(timeout=1.0) or []
        n = len(replies)
        if n == 0:
            return {'name': 'Celery Workers', 'status': 'down', 'detail': 'لا يوجد عمال نشطون', 'icon': 'gears'}
        return {'name': 'Celery Workers', 'status': 'ok', 'detail': f'{n} عامل نشط', 'icon': 'gears'}
    except Exception as e:
        return {'name': 'Celery Workers', 'status': 'unknown', 'detail': str(e)[:120], 'icon': 'gears'}


def _check_resources():
    """Disk + memory via psutil if available."""
    out = []
    try:
        import psutil
    except ImportError:
        return [{'name': 'Disk / Memory', 'status': 'unknown', 'detail': 'psutil غير مثبت — pip install psutil', 'icon': 'microchip'}]
    try:
        du = psutil.disk_usage('/')
        pct = du.percent
        free_gb = du.free / (1024 ** 3)
        status = 'ok' if pct < 80 else ('degraded' if pct < 92 else 'down')
        out.append({
            'name': 'Disk', 'status': status,
            'detail': f'{pct:.1f}% مستخدم — {free_gb:.1f} GB متاح', 'icon': 'hard-drive',
        })
    except Exception as e:
        out.append({'name': 'Disk', 'status': 'unknown', 'detail': str(e)[:80], 'icon': 'hard-drive'})
    try:
        vm = psutil.virtual_memory()
        status = 'ok' if vm.percent < 85 else ('degraded' if vm.percent < 95 else 'down')
        out.append({
            'name': 'Memory', 'status': status,
            'detail': f'{vm.percent:.1f}% مستخدم — {vm.available / (1024**3):.1f} GB متاح', 'icon': 'memory',
        })
    except Exception as e:
        out.append({'name': 'Memory', 'status': 'unknown', 'detail': str(e)[:80], 'icon': 'memory'})
    return out


# ─────────────────────────────────────────────────────────────────────
# 🩺 Tenant Health Score — churn-risk dashboard
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
def churn_risk(request):
    """
    /superadmin/churn-risk/ — تصنيف كل الشركات بدرجة صحة + أسباب الخطر.
    GET params:
      - risk  → low|medium|high|critical
      - grade → A|B|C|D|F
      - q     → بحث بالاسم/الـ schema
      - sort  → score (default ascending = worst first) | -score | name
    """
    from clients.services.tenant_health import bulk_tenant_health

    risk_filter = (request.GET.get('risk') or '').strip()
    grade_filter = (request.GET.get('grade') or '').strip().upper()
    q = (request.GET.get('q') or '').strip()
    sort = (request.GET.get('sort') or 'score').strip()

    tenants = list(
        Client.objects.exclude(schema_name='public').filter(is_deleted=False)
    )
    if q:
        ql = q.lower()
        tenants = [
            t for t in tenants
            if ql in (t.name or '').lower() or ql in (t.schema_name or '').lower()
        ]

    healths = bulk_tenant_health(tenants)

    rows = []
    for t in tenants:
        h = healths.get(t.id, {})
        rows.append({
            'tenant': t,
            'score': h.get('score', 0),
            'grade': h.get('grade', 'F'),
            'risk': h.get('risk', 'critical'),
            'signals': h.get('signals', {}),
            'reasons': h.get('reasons', []),
        })

    if risk_filter:
        rows = [r for r in rows if r['risk'] == risk_filter]
    if grade_filter:
        rows = [r for r in rows if r['grade'] == grade_filter]

    if sort == '-score':
        rows.sort(key=lambda r: r['score'], reverse=True)
    elif sort == 'name':
        rows.sort(key=lambda r: (r['tenant'].name or '').lower())
    else:  # default: worst first
        rows.sort(key=lambda r: r['score'])

    # تجميعات
    buckets = {'low': 0, 'medium': 0, 'high': 0, 'critical': 0}
    grades  = {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'F': 0}
    for r in rows:
        buckets[r['risk']] = buckets.get(r['risk'], 0) + 1
        grades[r['grade']] = grades.get(r['grade'], 0) + 1

    return render(request, 'clients/saas_admin/churn_risk.html', {
        'rows': rows,
        'filters': {'risk': risk_filter, 'grade': grade_filter, 'q': q, 'sort': sort},
        'buckets': buckets,
        'grades': grades,
        'total': len(rows),
    })


@saas_admin_required
def system_status(request):
    """
    /superadmin/system/ — لقطة لحظية لصحة المنصة.
    تشمل: DB ping, Redis cache, Celery workers, disk/memory, error rate, response time.
    """
    from clients.models import VisitorLog
    from django.db.models import Avg
    now = timezone.now()
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(hours=24)
    five_min_ago = now - timedelta(minutes=5)

    checks = [_check_database(), _check_cache(), _check_celery()] + _check_resources()

    # تحديد الحالة الإجمالية
    statuses = [c['status'] for c in checks]
    if 'down' in statuses:
        overall = 'down'
    elif 'degraded' in statuses:
        overall = 'degraded'
    elif all(s == 'ok' for s in statuses):
        overall = 'ok'
    else:
        overall = 'degraded'

    # App-level metrics
    try:
        errors_hour = SystemErrorLog.objects.filter(timestamp__gte=hour_ago).count()
        errors_day = SystemErrorLog.objects.filter(timestamp__gte=day_ago).count()
        unresolved = SystemErrorLog.objects.filter(is_resolved=False).count()
    except Exception:
        errors_hour = errors_day = unresolved = None

    try:
        avg_resp_hour = VisitorLog.objects.filter(
            timestamp__gte=hour_ago, response_time_ms__isnull=False,
        ).aggregate(avg=Avg('response_time_ms'))['avg'] or 0
        slowest = VisitorLog.objects.filter(
            timestamp__gte=hour_ago, response_time_ms__isnull=False,
        ).order_by('-response_time_ms')[:10].values(
            'timestamp', 'path', 'response_time_ms', 'status_code', 'tenant_schema',
        )
        active_5m = VisitorLog.objects.filter(timestamp__gte=five_min_ago).values('ip_address').distinct().count()
        reqs_hour = VisitorLog.objects.filter(timestamp__gte=hour_ago).count()
    except Exception:
        avg_resp_hour = reqs_hour = active_5m = 0
        slowest = []

    return render(request, 'clients/saas_admin/system_status.html', {
        'checks': checks,
        'overall': overall,
        'errors_hour': errors_hour,
        'errors_day': errors_day,
        'unresolved_errors': unresolved,
        'avg_resp_hour': int(avg_resp_hour or 0),
        'reqs_hour': reqs_hour,
        'active_5m': active_5m,
        'slowest': list(slowest),
        'now': now,
    })


# ─────────────────────────────────────────────────────────────────────
# 📢 Email Broadcast — composer + history
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
def broadcast_list(request):
    """صفحة قائمة الحملات + composer للحملة الجديدة."""
    from clients.services.broadcast import resolve_audience

    campaigns = BroadcastCampaign.objects.order_by('-created_at')[:100]

    # القيم الافتراضية للـ composer
    preview_audience = (request.GET.get('audience') or 'all').strip()
    preview_plan = (request.GET.get('plan') or '').strip()
    try:
        preview_qs = resolve_audience(preview_audience, plan=preview_plan)
        preview_count = preview_qs.count()
        preview_sample = list(preview_qs.values_list('name', 'email', 'schema_name')[:5])
    except Exception:
        preview_count = 0
        preview_sample = []

    plans = list(
        Plan.objects.filter(is_active=True).values('slug', 'name').order_by('name')
    )

    return render(request, 'clients/saas_admin/broadcast_list.html', {
        'campaigns': campaigns,
        'audience_choices': BroadcastCampaign.AUDIENCE_CHOICES,
        'preview_audience': preview_audience,
        'preview_plan': preview_plan,
        'preview_count': preview_count,
        'preview_sample': preview_sample,
        'plans': plans,
    })


@saas_admin_required
def broadcast_send(request):
    """POST: ينشئ حملة + يبعتها (sync — لـ MVP). للأحجام الكبيرة ننقلها لـ Celery لاحقاً."""
    if request.method != 'POST':
        return HttpResponseBadRequest("POST required")

    subject = (request.POST.get('subject') or '').strip()
    body = (request.POST.get('body') or '').strip()
    audience = (request.POST.get('audience') or 'all').strip()
    plan = (request.POST.get('plan') or '').strip()
    confirm = (request.POST.get('confirm') or '').strip()

    if not subject or not body:
        messages.error(request, "الموضوع والنص مطلوبان.")
        return redirect('saas_broadcast_list')
    if confirm != 'SEND':
        messages.error(request, "اكتب SEND في خانة التأكيد للإرسال.")
        return redirect('saas_broadcast_list')

    campaign = BroadcastCampaign.objects.create(
        subject=subject[:200],
        body=body,
        audience=audience,
        audience_plan=plan,
        created_by=request.user,
    )

    try:
        from clients.services.broadcast import send_campaign
        send_campaign(campaign)
    except Exception as e:
        logger.exception("broadcast_send failed")
        campaign.status = 'failed'
        campaign.error_log = f"{type(e).__name__}: {e}"
        campaign.save(update_fields=['status', 'error_log'])
        messages.error(request, f"فشل الإرسال: {e}")
        return redirect('saas_broadcast_list')

    _log_event(
        'other', user=request.user,
        description=(
            f"📢 بث جماعي «{subject[:80]}» — "
            f"{campaign.sent_count} مُرسَل / {campaign.failed_count} فشل / "
            f"{campaign.skipped_count} متخطّى"
        ),
    )
    messages.success(
        request,
        f"✅ تم الإرسال: {campaign.sent_count} نجاح، {campaign.failed_count} فشل، "
        f"{campaign.skipped_count} متخطٍ (بدون email).",
    )
    return redirect('saas_broadcast_list')
