"""🏢 لوحة مالك المنصة — الشركات: حذف/استرجاع، صلاحية OBD، خطر الإلغاء، التجارب، صفحة 360، التجديد، التحكم، البحث السريع."""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from datetime import timedelta

from django.contrib import messages
from django.db import transaction
from django.db import models
from django.db.models import Count
from django.http import HttpResponseForbidden, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from clients.models import (
    Client, SystemErrorLog,
    PartListing, DisputeTicket, PlatformEvent,
)
from clients.permissions import widget_required
from ._common import _log_event, saas_admin_required

logger = logging.getLogger('mouss_tec_core')


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
@widget_required('tenants')
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
@widget_required('tenants')
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
# 🔧 OBD / Diagnostics-Room paid add-on — grant UI (Phase 3 #3)
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('tenants')
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
@widget_required('tenants')
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
@widget_required('tenants')
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


# ─────────────────────────────────────────────────────────────────────
# 🩺 Tenant Health Score — churn-risk dashboard
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('tenants')
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
@widget_required('tenants')
def trials_watch(request):
    """/superadmin/trials/ — الشركات التجريبية: مين بيشتغل فعلاً ومين محتاج مكالمة."""
    from clients.services.trial_watch import trial_rows
    rows = trial_rows()
    counts = {}
    for r in rows:
        counts[r['verdict']] = counts.get(r['verdict'], 0) + 1
    return render(request, 'clients/saas_admin/trials_watch.html', {
        'rows': rows, 'counts': counts, 'total': len(rows),
    })


# ─────────────────────────────────────────────────────────────────────
# 🧭 Tenant 360 View — single page with everything about one tenant
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('tenants')
def tenant_360(request, tenant_id):
    """
    /superadmin/tenant/<id>/360/ — لقطة شاملة لشركة واحدة.
    تشمل: health score, subscription, billing, usage, support, errors,
    marketplace, audit log.
    """
    from clients.models import VisitorLog, PlatformInvoice
    from clients.services.tenant_health import bulk_tenant_health
    from django.db.models import Sum, Q

    tenant = get_object_or_404(
        Client.all_objects.select_related(),
        pk=tenant_id,
    )

    today = timezone.localdate()
    now = timezone.now()
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    quarter_ago = now - timedelta(days=90)

    # ─── Health ───
    health = bulk_tenant_health([tenant]).get(tenant.id, {})

    # ─── Subscription summary ───
    sub_end = tenant.subscription_end_date
    days_left = (sub_end - today).days if sub_end else None
    trial_days_left = None
    if tenant.status == 'trial' and tenant.trial_ends_at:
        trial_days_left = (tenant.trial_ends_at - today).days

    # ─── Billing ───
    try:
        invoices = list(
            PlatformInvoice.objects
            .filter(tenant=tenant)
            .select_related('plan_revision__plan')
            .order_by('-issued_at')[:20]
            .values(
                'invoice_number', 'period_start', 'period_end',
                'total', 'currency', 'status', 'issued_at', 'paid_at',
                'payment_provider', 'notes',
            )
        )
        billing_totals = PlatformInvoice.objects.filter(tenant=tenant).aggregate(
            paid=Sum('total', filter=Q(status='paid')),
            issued=Sum('total', filter=Q(status='issued')),
        )
        paid_total = float(billing_totals.get('paid') or 0)
        owed_total = float(billing_totals.get('issued') or 0)
    except Exception:
        logger.exception("tenant_360: billing fetch failed for tenant=%s", tenant.schema_name)
        invoices = []
        paid_total = owed_total = 0

    # ─── Usage (VisitorLog) ───
    try:
        visits_7d = VisitorLog.objects.filter(
            tenant_schema=tenant.schema_name, timestamp__gte=week_ago,
        ).count()
        visits_30d = VisitorLog.objects.filter(
            tenant_schema=tenant.schema_name, timestamp__gte=month_ago,
        ).count()
        last_visit = VisitorLog.objects.filter(
            tenant_schema=tenant.schema_name,
        ).order_by('-timestamp').values('timestamp', 'path', 'ip_address').first()
        top_pages = list(
            VisitorLog.objects
            .filter(tenant_schema=tenant.schema_name, timestamp__gte=month_ago)
            .values('path')
            .annotate(n=Count('id'))
            .order_by('-n')[:10]
        )
    except Exception:
        visits_7d = visits_30d = 0
        last_visit = None
        top_pages = []

    # ─── Users count (read inside tenant schema) ───
    try:
        from django.contrib.auth import get_user_model
        from django_tenants.utils import schema_context
        U = get_user_model()
        with schema_context(tenant.schema_name):
            users_total = U.objects.count()
            users_active = U.objects.filter(is_active=True).count()
    except Exception:
        users_total = users_active = None

    # ─── Support ───
    try:
        from clients.models import SupportTicket
        open_tickets = list(
            SupportTicket.objects
            .filter(tenant=tenant, is_deleted=False)
            .exclude(status='closed')
            .order_by('-created_at')[:10]
            .values('id', 'subject', 'status', 'priority', 'created_at')
        )
        closed_count = SupportTicket.objects.filter(
            tenant=tenant, status='closed',
        ).count()
    except Exception:
        open_tickets = []
        closed_count = 0

    # ─── Errors ───
    try:
        from clients.services.error_insight import explain
        recent_errors = list(
            SystemErrorLog.objects
            .filter(tenant_schema=tenant.schema_name)
            .order_by('-created_at')[:10]
        )
        _hosts = set(tenant.domains.values_list('domain', flat=True))
        for _e in recent_errors:
            _e.insight = explain(_e, _hosts)   # المشكلة فين بالظبط: الصفحة، كان واقف فين، السطر في الكود
        errors_30d = SystemErrorLog.objects.filter(
            tenant_schema=tenant.schema_name, created_at__gte=month_ago,
        ).count()
        # 🐛 [FIX]: أي 403/404 من زائر مش داخل (بوت فحص، معاينة لينك واتساب/فيسبوك، أيقونة آيفون)
        #    كان بيتحسب «أخطاء غير محلولة» على الشركة — عميل جديد بيبان عنده ١٠ أخطاء من أول ساعة.
        #    بنعدّهم لوحدهم كضوضاء زوار؛ الرقم الأحمر للأخطاء اللي قابلت مستخدم حقيقي أو 5xx.
        _open = SystemErrorLog.objects.filter(tenant_schema=tenant.schema_name, is_resolved=False)
        _visitor_noise = Q(user_id__isnull=True, status_code__in=(403, 404), exception_class='')
        unresolved_errors = _open.exclude(_visitor_noise).count()
        # بقت بتتقفل لوحدها وقت التسجيل — فبنعرض كام واحدة جت آخر ٣٠ يوم للعلم بس
        visitor_noise_errors = SystemErrorLog.objects.filter(
            _visitor_noise, tenant_schema=tenant.schema_name, created_at__gte=month_ago).count()
    except Exception:
        recent_errors = []
        errors_30d = unresolved_errors = visitor_noise_errors = 0

    # ─── Marketplace ───
    try:
        listings_total = PartListing.objects.filter(
            seller_tenant=tenant, is_deleted=False,
        ).count()
        listings_active = PartListing.objects.filter(
            seller_tenant=tenant, is_deleted=False, moderation_status='approved',
        ).count()
        disputes_open = DisputeTicket.objects.filter(
            opened_by_tenant=tenant, is_deleted=False,
            status__in=['open', 'under_review'],
        ).count()
    except Exception:
        listings_total = listings_active = disputes_open = 0

    # ─── Recent audit trail ───
    try:
        events = list(
            PlatformEvent.objects
            .filter(tenant_schema=tenant.schema_name)
            .order_by('-timestamp')[:20]
            .values('timestamp', 'event_type', 'user_name', 'description')
        )
    except Exception:
        events = []

    from clients.models import Plan as _Plan
    # 🧭 رحلة الشركة: الزيارات بالترتيب، آخر صفحة، وكل عملية (ومين عملها من سجل الأدمن)
    from clients.services.tenant_journey import build_tenant_journey
    try:
        journey_days = max(1, min(int(request.GET.get('journey_days', 7)), 30))
    except (TypeError, ValueError):
        journey_days = 7
    journey = build_tenant_journey(tenant, days=journey_days)
    from clients.views.screen_recording_views import retention_days, sessions_for_tenant
    screen_sessions = sessions_for_tenant(tenant.schema_name)

    return render(request, 'clients/saas_admin/tenant_360.html', {
        'tenant': tenant,
        'journey': journey,
        'screen_sessions': screen_sessions,
        'screen_retention_days': retention_days(),
        'control_plans': _Plan.objects.filter(is_active=True).order_by('monthly_price'),
        'is_owner': __import__('clients.permissions', fromlist=['is_platform_owner']).is_platform_owner(request.user),
        'today': today,
        'health': health,
        'days_left': days_left,
        'trial_days_left': trial_days_left,
        'invoices': invoices,
        'paid_total': paid_total,
        'owed_total': owed_total,
        'visits_7d': visits_7d,
        'visits_30d': visits_30d,
        'last_visit': last_visit,
        'top_pages': top_pages,
        'users_total': users_total,
        'users_active': users_active,
        'open_tickets': open_tickets,
        'closed_tickets_count': closed_count,
        'recent_errors': recent_errors,
        'errors_30d': errors_30d,
        'unresolved_errors': unresolved_errors,
        'visitor_noise_errors': visitor_noise_errors,
        'listings_total': listings_total,
        'listings_active': listings_active,
        'disputes_open': disputes_open,
        'events': events,
    })


# ─────────────────────────────────────────────────────────────────────
# 🔄 Tenant Subscription Renewal — AJAX from tenant_360
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
@widget_required('tenants')
def tenant_renew_subscription(request, tenant_id):
    """
    POST /superadmin/tenants/<id>/renew-subscription/
    Body: months=1|3|6|12
    تجديد أو تمديد اشتراك شركة مباشرة من لوحة السوبر أدمن.
    """
    from django.http import JsonResponse

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    tenant = get_object_or_404(Client.all_objects, pk=tenant_id)

    try:
        months = int(request.POST.get('months') or 1)
        if months not in (1, 3, 6, 12):
            return JsonResponse({'ok': False, 'error': 'مدة غير صحيحة — اختر 1 أو 3 أو 6 أو 12 شهراً'}, status=400)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'مدة غير صحيحة'}, status=400)

    from clients.models import Plan, TenantSubscription
    from clients.services.platform_billing import issue_subscription_invoice
    sub = TenantSubscription.objects.filter(tenant=tenant).select_related('plan').first()
    plan_obj = (sub.plan if sub and sub.plan_id else
                Plan.objects.filter(slug=tenant.plan, is_active=True).first())
    gift = request.POST.get('payment_mode') == 'gift'
    with transaction.atomic():
        if plan_obj is not None:
            # 💸 بفاتورة منصة زي Paymob (الإيرادات + فترة الاشتراك + المميزات)
            inv = issue_subscription_invoice(tenant, plan_obj, months, gift=gift, user=request.user,
                                             notes='تجديد من صفحة الشركات')
            tenant.refresh_from_db()
            new_end = tenant.subscription_end_date
        else:
            today = timezone.localdate()
            base = max(tenant.subscription_end_date or today, today)
            new_end = base + timedelta(days=30 * months)
            tenant.subscription_end_date = new_end
            tenant.status = 'active'
            tenant.is_active = True
            tenant.save(update_fields=['subscription_end_date', 'status', 'is_active'])

        # مسح الكاش عشان الـ middleware يشوف التغيير فوراً
        try:
            from django.core.cache import caches
            cache = caches['local_tier'] if 'local_tier' in caches else caches['default']
            cache.delete(f"tenant_guard_plan_{tenant.schema_name}")
        except Exception:
            pass

        PlatformEvent.objects.create(
            event_type='other',
            tenant_schema=tenant.schema_name,
            tenant_name=tenant.name,
            user_name=request.user.username,
            description=(
                f"🔄 تجديد اشتراك «{tenant.name}» لمدة {months} شهر "
                f"(ينتهي {new_end}) — بواسطة {request.user.username}"
            ),
        )

    logger.warning(
        "subscription_renewal tenant=%s months=%d new_end=%s by=%s",
        tenant.schema_name, months, new_end, request.user.username,
    )

    return JsonResponse({
        'ok': True,
        'message': f'✅ تم تجديد اشتراك «{tenant.name}» حتى {new_end}',
        'new_end': str(new_end),
        'months': months,
    })


# ─────────────────────────────────────────────────────────────────────
# 🎛️ Tenant Control — كل أدوات مالك المنصة على شركة واحدة (من صفحة 360)
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
def tenant_control(request, tenant_id):
    """POST /superadmin/tenant/<id>/control/  (action=...) → JSON.

    أدوات زي لوحات Stripe/Chargebee: إصدار فاتورة يدوية، تحصيلها، إلغاؤها أو
    ردّها، تغيير الباقة (مدفوع/هدية)، حدود إضافية مجانية، شحن/خصم المحفظة
    بقيد في الدفتر، رابط إعادة كلمة سر المالك، تأكيد الإيميل، وملاحظة داخلية.
    كل حاجة بتتسجّل في سجل التدقيق.
    """
    from django.core.exceptions import ValidationError
    from django.http import JsonResponse
    from clients.models import EscrowLedger, Plan, PlatformInvoice, TenantSubscription
    from clients.permissions import is_platform_owner
    from clients.services import platform_billing as pb

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)
    if not is_platform_owner(request.user):
        return JsonResponse({'ok': False, 'error': 'مالك المنصة بس'}, status=403)
    tenant = get_object_or_404(Client.all_objects, pk=tenant_id)
    action = (request.POST.get('action') or '').strip()
    P = request.POST

    def done(msg, **extra):
        _log_event('other', tenant=tenant, user=request.user, description=f"🎛️ {msg}")
        return JsonResponse({'ok': True, 'message': msg, **extra})

    def fail(msg, status=400):
        return JsonResponse({'ok': False, 'error': msg}, status=status)

    def months_from_post():
        try:
            m = int(P.get('months') or 1)
        except (TypeError, ValueError):
            m = 1
        return m if m in pb.PERIOD_DAYS else 1

    def invoice_from_post():
        return PlatformInvoice.objects.filter(tenant=tenant, invoice_number=(P.get('invoice') or '').strip()).first()

    try:
        if action in ('change_plan', 'issue_invoice'):
            plan = Plan.objects.filter(slug=(P.get('plan') or '').strip(), is_active=True).first()
            if plan is None:
                sub = TenantSubscription.objects.filter(tenant=tenant).select_related('plan').first()
                plan = sub.plan if sub and sub.plan_id else None
            if plan is None:
                return fail('اختار باقة.')
            months = months_from_post()
            if action == 'change_plan':
                gift = P.get('payment_mode') == 'gift'
                inv = pb.issue_subscription_invoice(tenant, plan, months, gift=gift, user=request.user,
                                                    reference=(P.get('reference') or '').strip()[:100],
                                                    notes=(P.get('notes') or '').strip()[:300] or 'من لوحة التحكم')
                tenant.refresh_from_db()
                return done(f"باقة «{plan.name}» {months} شهر — {'هدية' if gift else inv.total} — "
                            f"فاتورة {inv.invoice_number} — ساري حتى {tenant.subscription_end_date}")
            inv = pb.issue_subscription_invoice(tenant, plan, months, paid=False, user=request.user,
                                                notes=(P.get('notes') or '').strip()[:300] or 'فاتورة يدوية')
            return done(f"اتصدرت فاتورة {inv.invoice_number} بـ {inv.total} (مستحقة على الشركة)")

        if action == 'mark_paid':
            inv = invoice_from_post()
            if inv is None or inv.status != 'issued':
                return fail('الفاتورة مش موجودة أو مش مستحقة.')
            ref = (P.get('reference') or '').strip()[:100] or f"ADMIN-{inv.invoice_number}"
            # 🐛 [FIX]: الفترة كانت بتفضل زي ما اتحسبت وقت الإصدار — فاتورتين
            #    مستحقتين كانوا بيغطّوا نفس الشهر. نفس تحصيل بوابة الدفع والإيصالات.
            try:
                inv = pb.settle_issued_invoice(inv, provider='manual_admin', reference=ref)
            except ValidationError as exc:
                return fail('؛ '.join(exc.messages))
            return done(f"تحصيل فاتورة {inv.invoice_number} ({inv.total}) — مرجع {ref}")

        if action == 'void_invoice':
            inv = invoice_from_post()
            if inv is None or not pb.void_invoice(inv, reason=(P.get('reason') or '').strip()[:200], user=request.user):
                return fail('الإلغاء للفواتير المستحقة (غير المدفوعة) بس.')
            return done(f"إلغاء فاتورة {inv.invoice_number}")

        if action == 'refund_invoice':
            inv = invoice_from_post()
            cut = P.get('cut_service') == '1'
            if inv is None or not pb.refund_invoice(inv, reason=(P.get('reason') or '').strip()[:200],
                                                    user=request.user, cut_service=cut):
                return fail('الرد للفواتير المدفوعة بس.')
            return done(f"رد فاتورة {inv.invoice_number} ({inv.total})" + (" — والخدمة اتوقفت" if cut else ""))

        if action == 'set_limits':
            vals = {}
            for key in ('extra_users_purchased', 'extra_branches_purchased', 'extra_treasuries_purchased'):
                raw = (P.get(key) or '').strip()
                if raw == '':
                    continue
                try:
                    v = int(raw)
                except ValueError:
                    return fail('الأرقام لازم تكون صحيحة.')
                if not 0 <= v <= 500:
                    return fail('القيمة من 0 لـ 500.')
                vals[key] = v
            if not vals:
                return fail('مفيش تغيير.')
            Client.all_objects.filter(pk=tenant.pk).update(**vals)
            return done("حدود إضافية: " + ", ".join(f"{k.split('_')[1]}={v}" for k, v in vals.items()))

        if action == 'wallet_adjust':
            try:
                amount = Decimal(str(P.get('amount') or '0'))
            except InvalidOperation:
                return fail('مبلغ غير صحيح.')
            if not amount.is_finite() or amount <= 0:
                return fail('مبلغ غير صحيح.')
            direction = P.get('direction')
            note = (P.get('reason') or '').strip()[:150] or 'تسوية يدوية من لوحة المنصة'
            if direction not in ('credit', 'debit'):
                return fail('اختار إضافة أو خصم.')
            try:
                EscrowLedger.objects.create(
                    client=tenant, amount=amount,
                    transaction_type='deposit' if direction == 'credit' else 'withdrawal',
                    description=f"{'➕' if direction == 'credit' else '➖'} {note} — {request.user.username}"[:255],
                )
            except Exception as exc:  # pre_save بيرفض الخصم لو الرصيد مش كفاية
                return fail(getattr(exc, 'messages', [str(exc)])[0])
            tenant.refresh_from_db()
            return done(f"{'شحن' if direction == 'credit' else 'خصم'} محفظة {amount} — الرصيد {tenant.wallet_balance}")

        if action in ('owner_reset_link', 'verify_email'):
            from django.contrib.auth import get_user_model
            from django.core import signing
            from django_tenants.utils import schema_context
            import time
            with schema_context(tenant.schema_name):
                owner = (get_user_model().objects.filter(is_superuser=True, is_active=True)
                         .order_by('date_joined').first())
            if action == 'verify_email':
                Client.all_objects.filter(pk=tenant.pk).update(is_active=True)
                return done("تأكيد إيميل الشركة يدوياً (اتفعّلت)")
            if owner is None:
                return fail('مفيش حساب مالك للشركة.')
            token = signing.dumps({'schema_name': tenant.schema_name, 'user_id': owner.id,
                                   'email': owner.email, 'created': int(time.time())},
                                  salt='employee-set-password')
            link = f"{request.scheme}://{request.get_host()}/account/set-password/?token={token}"
            _log_event('other', tenant=tenant, user=request.user,
                       description=f"🎛️ رابط إعادة كلمة سر المالك ({owner.email})")
            return JsonResponse({'ok': True, 'message': f"رابط لـ {owner.email} (صالح 7 أيام)", 'link': link})

        if action == 'note':
            note = (P.get('note') or '').strip()[:500]
            if not note:
                return fail('اكتب الملاحظة.')
            return done(f"ملاحظة: {note}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("tenant_control %s failed for %s", action, tenant.schema_name)
        return fail(f'حصل خطأ: {exc}', status=500)

    return fail('إجراء غير معروف.')


# ─────────────────────────────────────────────────────────────────────
# 🔍 Quick Search API — backs the ⌘K command palette
# ─────────────────────────────────────────────────────────────────────
@saas_admin_required
def quick_search(request):
    """
    /superadmin/api/quick-search/?q=… → JSON {results: [{label, sub, url, kind}, …]}
    يبحث في: tenants (name + schema), users (username + email).
    """
    from django.http import JsonResponse
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    q = (request.GET.get('q') or '').strip()
    out = []

    if len(q) >= 1:
        # tenants
        tenants = (
            Client.objects.exclude(schema_name='public').filter(is_deleted=False)
            .filter(Q(name__icontains=q) | Q(schema_name__icontains=q) | Q(email__icontains=q))
            .values('id', 'name', 'schema_name', 'plan', 'status')[:8]
        )
        for t in tenants:
            out.append({
                'kind':  'tenant',
                'label': t['name'] or t['schema_name'],
                'sub':   f"{t['schema_name']} · {t['plan']} · {t['status']}",
                'url':   f"/superadmin/tenant/{t['id']}/360/",
                'icon':  'building',
            })

        # users (platform-wide; we look up superusers + tenant admins on public schema)
        U = get_user_model()
        users = (
            U.objects.filter(
                Q(username__icontains=q) | Q(email__icontains=q),
            )[:5]
            .values('id', 'username', 'email', 'is_superuser', 'is_active')
        )
        for u in users:
            badge = '🛡️ super' if u['is_superuser'] else 'user'
            out.append({
                'kind':  'user',
                'label': u['username'],
                'sub':   f"{u['email'] or '—'} · {badge}",
                'url':   f"/superadmin/audit-log/?user={u['username']}",
                'icon':  'user',
            })

    return JsonResponse({'results': out, 'q': q})
