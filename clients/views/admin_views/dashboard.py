"""👑 أدوات السوبر أدمن — لوحة إدارة كل الشركات."""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import user_passes_test
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Avg, Count, Sum, Q
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _t
from django_tenants.utils import schema_context

from clients.models import (
    AIAddonPackage,
    AIBonusGrant,
    BidOffer,
    BlindBiddingRequest,
    Client,
    DesignPrintRequest,
    DesignPurchase,
    Domain,
    EscrowLedger,
    GlobalB2BMarketplace,
    ManualPaymentReceipt,
    MarketplaceCustomer,
    PartListing,
    PartOrder,
    Plan,
    PlatformEvent,
    ServiceRequest,
    TenantSubscription,
    VisitorLog,
)

from .._shared import _is_platform_owner
from erp_core.localization import current_tenant_symbol as _sym
from ._common import ADMIN_URL, PAID_PURCHASE_STATUSES, _plan_slug_candidates, _resolve_active_plan

logger = logging.getLogger('mouss_tec_core')
User = get_user_model()


# =====================================================================
# 👑 Super Admin — لوحة إدارة كل الشركات
# =====================================================================
@user_passes_test(_is_platform_owner, login_url='/secure-portal/login/')
def super_admin_dashboard(request):

    # حماية مزدوجة: حتى لو عدى الـ decorator، نتأكد إنه على public schema
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('<h1>403 — Access Denied</h1><p>هذه الصفحة مخصصة لمالك المنصة فقط.</p>')

    action = request.POST.get('action')
    tenant_id = request.POST.get('tenant_id')

    if request.method == 'POST' and tenant_id:
        # 🐛 [FIX]: get_object_or_404 كان بيرمي Http404 — مركز الأخطاء كان بيسجّله «500 عطل في الكود»
        #    والسوبر أدمن بيشوف صفحة 404 فاضية. السبب الشائع: الشركة اتحذفت (ضغطة حذف مرتين/تاب قديم)
        #    أو tenant_id بايظ. دلوقتي رسالة واضحة ورجوع للوحة.
        target = (Client.objects.filter(pk=int(tenant_id)).first()
                  if str(tenant_id).strip().isdigit() else None)
        if target is None:
            logger.warning("super_admin_dashboard: action=%r on missing tenant_id=%r by %s",
                           action, tenant_id, request.user.username)
            messages.error(request, _t('الشركة رقم «%(id)s» مش موجودة — غالباً اتحذفت أو الصفحة قديمة. '
                                       'حدّث الصفحة وجرّب تاني.') % {'id': str(tenant_id)[:20]})
            return redirect(reverse('super_admin_dashboard') + '#tenants')
        if action == 'suspend':
            target.status = 'suspended'
            target.is_active = False
            target.save(update_fields=['status', 'is_active'])
            PlatformEvent.objects.create(
                event_type='suspension', tenant_schema=target.schema_name,
                tenant_name=target.name, user_name=request.user.username,
                description=f"تعليق حساب «{target.name}» بواسطة {request.user.username}",
            )
            # 🐛 [FIX]: التعليق/التفعيل/المراقبة/تمديد التجربة ماكانوش بيظهروا أي رسالة — الصفحة بترجع ومش باين اتعمل ولا لأ
            messages.success(request, _t('اتعلّق حساب «%(name)s».') % {'name': target.name})
        elif action == 'activate':
            # 🐛 [FIX]: «تفعيل» كان بيحط active من غير تاريخ انتهاء — والتحقق
            #    والـ cron بيعتبروا «مفيش تاريخ» = مش بينتهي، يعني مجاني للأبد.
            #    دلوقتي بيرفع التعليق بس: لو مفيش اشتراك ساري، مهلة 7 أيام.
            from clients.models.tenancy import TRIAL_DAYS
            today = timezone.localdate()
            fields = ['status', 'is_active']
            if not target.subscription_end_date or target.subscription_end_date < today:
                target.subscription_end_date = today + timedelta(days=TRIAL_DAYS)
                fields.append('subscription_end_date')
            target.status = 'active'
            target.is_active = True
            target.save(update_fields=fields)
            PlatformEvent.objects.create(
                event_type='other', tenant_schema=target.schema_name,
                tenant_name=target.name, user_name=request.user.username,
                description=f"رفع تعليق «{target.name}» — ساري حتى {target.subscription_end_date}",
            )
            messages.success(request, _t('اتفعّل حساب «%(name)s» — ساري حتى %(date)s.')
                             % {'name': target.name, 'date': target.subscription_end_date})
        elif action == 'flag_fraud':
            target.is_fraud_flagged = True
            target.save(update_fields=['is_fraud_flagged'])
            PlatformEvent.objects.create(
                event_type='fraud_flag', tenant_schema=target.schema_name,
                tenant_name=target.name, user_name=request.user.username,
                description=f"تعليم احتيال على «{target.name}»",
            )
            messages.success(request, _t('«%(name)s» بقت تحت المراقبة.') % {'name': target.name})
        elif action == 'unflag_fraud':
            target.is_fraud_flagged = False
            target.save(update_fields=['is_fraud_flagged'])
            PlatformEvent.objects.create(
                event_type='fraud_flag', tenant_schema=target.schema_name,
                tenant_name=target.name, user_name=request.user.username,
                description=f"إلغاء تعليم الاحتيال عن «{target.name}»",
            )
            messages.success(request, _t('اتشالت المراقبة عن «%(name)s».') % {'name': target.name})
        elif action == 'extend_trial':
            # 🐛 [FIX]: كان بيزوّد 7 أيام على تاريخ قديم فات (فالتمديد مايبانش)
            #    ومابيرفعش التعليق اللي حصل بسبب انتهاء التجربة.
            from clients.models.tenancy import TRIAL_DAYS
            today = timezone.localdate()
            base_date = max(target.trial_ends_at or today, today)
            target.trial_ends_at = base_date + timedelta(days=TRIAL_DAYS)
            fields = ['trial_ends_at']
            if target.status in ('suspended', 'trial') or not target.is_active:
                target.status = 'trial'
                target.is_active = True
                fields += ['status', 'is_active']
            target.save(update_fields=fields)
            PlatformEvent.objects.create(
                event_type='other', tenant_schema=target.schema_name,
                tenant_name=target.name, user_name=request.user.username,
                description=f"تمديد تجربة «{target.name}» حتى {target.trial_ends_at}",
            )
            messages.success(request, _t('اتمدّدت تجربة «%(name)s» حتى %(date)s.')
                             % {'name': target.name, 'date': target.trial_ends_at})
        elif action == 'activate_subscription':
            # 💸 [FIX]: التفعيل اليدوي ماكانش بيعمل فاتورة منصة ولا بيمد فترة
            #    TenantSubscription — فالإيرادات (من الفواتير) ماكانتش بتطابق MRR.
            #    دلوقتي نفس مسار دفع Paymob: فاتورة → mark_paid().
            from clients.services.platform_billing import issue_subscription_invoice
            plan = request.POST.get('plan', 'silver')
            billing_period = request.POST.get('billing_period', 'monthly')
            period_labels = {'monthly': 'شهري', 'quarterly': 'ربع سنوي',
                             'semi_annual': 'نصف سنوي', 'annual': 'سنوي'}
            period_ui = {'monthly': _t('شهري'), 'quarterly': _t('ربع سنوي'),
                         'semi_annual': _t('نصف سنوي'), 'annual': _t('سنوي')}
            months = {'monthly': 1, 'quarterly': 3, 'semi_annual': 6, 'annual': 12}.get(billing_period, 1)
            gift = request.POST.get('payment_mode') == 'gift'
            # 🐛 [FIX]: المودال كان بيبعت silver/gold/empire (قيم Client.plan القديمة) والباقات في
            #    الـ DB اسمها auto-silver/auto-gold… ⇒ «الباقة غير موجودة» على طول وزرار «اشتراك»
            #    عمره ما اشتغل. دلوقتي المودال بيبعت الـ slug الحقيقي، والقديم بيتترجم برضه.
            plan_full = _resolve_active_plan(plan)
            if plan_full is None:
                messages.error(request, _t('الباقة غير موجودة أو موقوفة.'))
            else:
                inv = issue_subscription_invoice(
                    target, plan_full, months, gift=gift, user=request.user,
                    notes=(request.POST.get('notes') or '').strip()[:300] or 'تفعيل يدوي من لوحة المنصة')
                target.refresh_from_db()
                PlatformEvent.objects.create(
                    event_type='subscription', tenant_schema=target.schema_name,
                    tenant_name=target.name, user_name=request.user.username,
                    description=(f"تفعيل اشتراك «{target.name}» — {plan} {period_labels.get(billing_period)} — "
                                 f"{'هدية' if gift else f'{inv.total} {_sym()}'} — فاتورة {inv.invoice_number}"),
                    metadata={'plan': plan, 'period': billing_period, 'total': str(inv.total),
                              'invoice': inv.invoice_number, 'gift': gift},
                )
                messages.success(request, _t(
                    'تم تفعيل اشتراك «%(name)s» — باقة %(plan)s (%(period)s) — %(amount)s — فاتورة %(invoice)s — '
                    'ينتهي %(end)s') % {
                    'name': target.name, 'plan': target.get_plan_display(),
                    'period': period_ui.get(billing_period, billing_period),
                    'amount': _t('🎁 هدية') if gift else f'{inv.total} {_sym()}',
                    'invoice': inv.invoice_number, 'end': target.subscription_end_date})

        elif action == 'renew_subscription':
            # تجديد الاشتراك الحالي بنفس الباقة لمدة شهر — بفاتورة منصة (زي Paymob)
            from clients.services.platform_billing import issue_subscription_invoice
            sub = TenantSubscription.objects.filter(tenant=target).select_related('plan').first()
            # 🐛 [FIX]: الـ fallback كان بيدوّر بـ Client.plan ('gold') على slug الباقة ('auto-gold') فمابيلاقيش
            plan_obj = sub.plan if sub and sub.plan_id else _resolve_active_plan(target.plan)
            if plan_obj is None:
                messages.error(request, _t('مفيش باقة مربوطة بالشركة — فعّل اشتراك الأول.'))
            else:
                gift = request.POST.get('payment_mode') == 'gift'
                inv = issue_subscription_invoice(target, plan_obj, 1, gift=gift, user=request.user,
                                                 notes='تجديد يدوي من لوحة المنصة')
                target.refresh_from_db()
                PlatformEvent.objects.create(
                    event_type='subscription', tenant_schema=target.schema_name,
                    tenant_name=target.name, user_name=request.user.username,
                    description=(f"تجديد اشتراك «{target.name}» — {plan_obj.slug} — حتى {target.subscription_end_date}"
                                 f" — فاتورة {inv.invoice_number} ({inv.total})"),
                )
                messages.success(request, _t('تم تجديد اشتراك «%(name)s» حتى %(end)s — فاتورة %(invoice)s') % {
                    'name': target.name, 'end': target.subscription_end_date, 'invoice': inv.invoice_number})

        elif action == 'activate_ai_addon':
            # تفعيل حزمة AI Studio للشركة
            addon_slug = request.POST.get('ai_addon_slug', 'ai-basic')
            addon = AIAddonPackage.objects.filter(slug=addon_slug, is_active=True).first()
            if not addon:
                messages.error(request, _t('حزمة AI غير موجودة.'))
            else:
                sub, _ = TenantSubscription.objects.get_or_create(tenant=target)
                sub.ai_addon = addon
                sub.is_active = True
                sub.save(update_fields=['ai_addon', 'is_active', 'updated_at'])
                PlatformEvent.objects.create(
                    event_type='subscription', tenant_schema=target.schema_name,
                    tenant_name=target.name, user_name=request.user.username,
                    description=f"تفعيل حزمة AI «{addon.name}» لشركة «{target.name}»",
                    metadata={'ai_addon': addon_slug},
                )
                messages.success(request, _t('تم تفعيل حزمة AI «%(addon)s» لشركة «%(name)s»') % {'addon': addon.name, 'name': target.name})

        elif action == 'deactivate_ai_addon':
            # إلغاء حزمة AI Studio
            try:
                sub = TenantSubscription.objects.get(tenant=target)
                old_addon = sub.ai_addon.name if sub.ai_addon else ''
                sub.ai_addon = None
                sub.save(update_fields=['ai_addon', 'updated_at'])
                PlatformEvent.objects.create(
                    event_type='subscription', tenant_schema=target.schema_name,
                    tenant_name=target.name, user_name=request.user.username,
                    description=f"إلغاء حزمة AI «{old_addon}» من شركة «{target.name}»",
                )
                messages.success(request, _t('تم إلغاء حزمة AI من «%(name)s»') % {'name': target.name})
            except TenantSubscription.DoesNotExist:
                messages.error(request, _t('لا يوجد اشتراك لهذه الشركة.'))

        elif action == 'grant_ai_bonus':
            # 🎁 هدية رصيد AI Studio من السوبر أدمن
            try:
                designs = int(request.POST.get('grant_designs', 0) or 0)
                whatsapp_n = int(request.POST.get('grant_whatsapp', 0) or 0)
                watermarks_n = int(request.POST.get('grant_watermarks', 0) or 0)
            except ValueError:
                designs = whatsapp_n = watermarks_n = 0

            reason = request.POST.get('grant_reason', '').strip()
            expires_days = request.POST.get('grant_expires_days', '').strip()
            expires_at = None
            if expires_days:
                try:
                    expires_at = timezone.now() + timedelta(days=int(expires_days))
                except ValueError:
                    pass

            if designs + whatsapp_n + watermarks_n <= 0:
                messages.error(request, _t('❌ يجب تحديد رصيد واحد على الأقل (تصاميم / واتساب / علامات مائية).'))
            else:
                grant = AIBonusGrant.objects.create(
                    tenant=target,
                    granted_designs=designs,
                    granted_whatsapp=whatsapp_n,
                    granted_watermarks=watermarks_n,
                    reason=reason or 'هدية من إدارة المنصة',
                    granted_by=request.user,
                    expires_at=expires_at,
                )
                PlatformEvent.objects.create(
                    event_type='other', tenant_schema=target.schema_name,
                    tenant_name=target.name, user_name=request.user.username,
                    description=f"🎁 منح هدية لشركة «{target.name}» — {designs} تصميم، {whatsapp_n} واتساب، {watermarks_n} علامة مائية",
                )
                msg = _t('🎁 تم منح «%(name)s» هدية: %(d)s تصميم + %(w)s واتساب + %(m)s علامة مائية') % {
                    'name': target.name, 'd': designs, 'w': whatsapp_n, 'm': watermarks_n}
                if expires_at:
                    msg += ' ' + _t('(تنتهي خلال %(n)s يوم)') % {'n': expires_days}
                messages.success(request, msg)

        elif action == 'revoke_bonus':
            grant_id = request.POST.get('grant_id')
            try:
                grant = AIBonusGrant.objects.get(pk=grant_id, tenant=target)
                grant.is_active = False
                grant.save(update_fields=['is_active'])
                messages.success(request, _t('تم إلغاء الهدية #%(id)s من «%(name)s»') % {'id': grant.pk, 'name': target.name})
            except AIBonusGrant.DoesNotExist:
                messages.error(request, _t('الهدية غير موجودة.'))

        elif action == 'delete_tenant':
            # ⚠️ حذف نهائي للشركة — يحذف الـ schema بالكامل
            confirm_name = request.POST.get('confirm_name', '').strip()
            if confirm_name != target.schema_name:
                messages.error(request, _t('فشل الحذف: اسم التأكيد لا يتطابق مع اسم الـ Schema.'))
            else:
                tenant_name = target.name
                schema = target.schema_name
                from django.db.models import ProtectedError, RestrictedError
                try:
                    # 🐛 [FIX]: الخطوات ماكانتش في transaction — الروابط بتتمسح والـ schema بيتعمله
                    #    DROP، وبعدين مسح صف الشركة يقع (فواتير اشتراك PROTECT) فالشركة تفضل
                    #    متسجّلة من غير بيانات ولا رابط، ومهاجرات كل نشر تقع عليها
                    #    (MigrationSchemaMissing). دلوقتي كله أو ولا حاجة (DROP SCHEMA في Postgres
                    #    بيرجع مع الـ rollback).
                    with transaction.atomic():
                        # حذف السجلات المرتبطة بـ PROTECT FK قبل حذف الـ Tenant
                        EscrowLedger.objects.filter(client=target).delete()
                        GlobalB2BMarketplace.objects.filter(tenant=target).delete()
                        BidOffer.objects.filter(seller=target).delete()
                        BlindBiddingRequest.objects.filter(buyer=target).update(winner=None)
                        Domain.objects.filter(tenant=target).delete()
                        target.delete(force_drop=True)
                        PlatformEvent.objects.create(
                            event_type='other', tenant_schema=schema,
                            tenant_name=tenant_name, user_name=request.user.username,
                            description=f"🗑️ حذف نهائي لشركة «{tenant_name}» (schema: {schema})",
                        )
                    cache.delete(f"tenant_guard_plan_{schema}")
                    messages.success(request, _t('تم حذف شركة «%(name)s» نهائياً.') % {'name': tenant_name})
                except (ProtectedError, RestrictedError) as e:
                    objs = getattr(e, 'protected_objects', None) or getattr(e, 'restricted_objects', None) or []
                    what = '، '.join(sorted({str(o._meta.verbose_name_plural) for o in objs})) or '—'
                    messages.error(
                        request,
                        _t('مش هينفع تتمسح «%(name)s»: عليها سجلات مالية لازم تفضل (%(what)s). '
                           'مااتمسحش أي حاجة — استخدم «تعليق» بدل الحذف.') % {'name': tenant_name, 'what': what})
                except Exception as e:
                    logger.error("[SUPER ADMIN] Failed to delete tenant %s: %s", schema, e)
                    messages.error(request, _t('فشل حذف الشركة: %(err)s') % {'err': e})

        else:
            # 🐛 [FIX]: إجراء مش معروف كان بيرجع بصمت كأنه اتعمل
            messages.error(request, _t('الإجراء ده مش معروف — حدّث الصفحة وجرّب تاني.'))

        # 🐛 [FIX]: بعد أي إجراء على شركة (تعليق/تجديد/هدية…) كانت الصفحة بترجع على
        #    «نظرة عامة» والرسالة تبان والكارت مش قدامك — نرجع على تاب الشركات نفسه.
        return redirect(reverse('super_admin_dashboard') + '#tenants')

    # ══════════════════════════════════════════════════════════════
    # DATA AGGREGATION — الداتا الضخمة للوحة التحكم
    # ══════════════════════════════════════════════════════════════
    tenants = Client.objects.exclude(schema_name='public').order_by('-created_on')
    today = timezone.localdate()
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # --- Tenant Summary ---
    summary = {
        'total': tenants.count(),
        'trial': tenants.filter(status='trial').count(),
        'active': tenants.filter(status='active').count(),
        'suspended': tenants.filter(status='suspended').count(),
        'fraud': tenants.filter(is_fraud_flagged=True).count(),
        'automotive': tenants.filter(industry='automotive').count(),
        'printing': tenants.filter(industry='printing').count(),
    }

    # --- New signups this month ---
    new_this_month = tenants.filter(created_on__gte=today.replace(day=1)).count()
    new_this_week = tenants.filter(created_on__gte=(today - timedelta(days=7))).count()

    # --- Expiring soon (next 7 days) ---
    expiring_soon = tenants.filter(
        status='active',
        subscription_end_date__isnull=False,
        subscription_end_date__lte=today + timedelta(days=7),
        subscription_end_date__gte=today,
    ).count()

    # --- Trial expired but still in trial status ---
    trial_expired = tenants.filter(status='trial', trial_ends_at__lt=today).count()

    # --- Visitor Analytics ---
    visitor_stats = {}
    try:
        # 🐛 [FIX]: الأرقام كانت بتحسب صاحب المنصة نفسه (السوبر أدمن + تنبيه الدعم اللي بيسأل كل ٢٠ ثانية)
        #    فـ«زوار فريدون اليوم» و«أكثر الصفحات زيارة» كانوا مليانين /superadmin/… — مش زوار حقيقيين.
        from clients.services.live_market import _BACKGROUND, _OWNER_PREFIXES
        owner_q = Q()
        for _p in _OWNER_PREFIXES:
            owner_q |= Q(path__startswith=_p)
        logs = VisitorLog.objects.exclude(Q(tenant_schema__in=('public', '')) & owner_q)
        visitors_today = logs.filter(timestamp__gte=today_start).count()
        unique_ips_today = logs.filter(
            timestamp__gte=today_start
        ).values('ip_address').distinct().count()
        visitors_week = logs.filter(timestamp__gte=week_ago).count()
        unique_ips_week = logs.filter(
            timestamp__gte=week_ago
        ).values('ip_address').distinct().count()

        # أكثر الصفحات زيارة — صفحات بس (من غير طلبات الخلفية زي الإشعارات والعدّادات)
        bg_q = Q()
        for _p in _BACKGROUND:
            bg_q |= Q(path__contains=_p)
        top_pages = list(
            logs.filter(timestamp__gte=week_ago, method='GET').exclude(bg_q)
            .values('path')
            .annotate(hits=Count('id'))
            .order_by('-hits')[:10]
        )

        # أكثر الشركات نشاطاً
        top_tenants = list(
            logs.filter(timestamp__gte=week_ago)
            .exclude(tenant_schema='public')
            .exclude(tenant_schema='')
            .values('tenant_schema')
            .annotate(hits=Count('id'))
            .order_by('-hits')[:10]
        )
        # 🐛 [FIX]: «أنشط الشركات» كانت بتعرض اسم الـ schema بدل اسم الشركة
        _names = dict(Client.objects.filter(schema_name__in=[r['tenant_schema'] for r in top_tenants])
                      .values_list('schema_name', 'name'))
        for r in top_tenants:
            r['name'] = _names.get(r['tenant_schema'], r['tenant_schema'])

        # Device breakdown
        device_breakdown = list(
            logs.filter(timestamp__gte=week_ago)
            .values('device_type')
            .annotate(count=Count('id'))
            .order_by('-count')
        )

        # Average response time
        avg_response = logs.filter(
            timestamp__gte=today_start, response_time_ms__isnull=False,
        ).aggregate(avg=Avg('response_time_ms'))['avg'] or 0

        # آخر 50 زائر (Live Feed)
        recent_visitors = list(
            logs.filter(timestamp__gte=today_start)
            .order_by('-timestamp')[:50]
            .values(
                'timestamp', 'ip_address', 'path', 'method',
                'status_code', 'tenant_schema', 'device_type',
                'response_time_ms', 'user__username',
            )
        )

        visitor_stats = {
            'today': visitors_today,
            'unique_today': unique_ips_today,
            'week': visitors_week,
            'unique_week': unique_ips_week,
            'top_pages': top_pages,
            'top_tenants': top_tenants,
            'device_breakdown': device_breakdown,
            'avg_response_ms': round(avg_response),
            'recent': recent_visitors,
        }
    except Exception:
        logger.exception("super_admin_dashboard: visitor_stats computation failed")

    # --- Platform Events (Activity Feed) ---
    recent_events = []
    try:
        recent_events = list(
            PlatformEvent.objects.order_by('-timestamp')[:30]
            .values('timestamp', 'event_type', 'tenant_name', 'user_name', 'description')
        )
    except Exception:
        logger.exception("super_admin_dashboard: recent_events fetch failed")

    # --- Tenant Deep Details — bulk prefetches بدل N+1 ---
    # AI addon name per tenant (1 query)
    ai_addon_by_tenant = {
        s['tenant_id']: s['ai_addon__name'] or ''
        for s in TenantSubscription.objects.filter(
            tenant__in=tenants, ai_addon__isnull=False,
        ).values('tenant_id', 'ai_addon__name')
    }
    # All active grants per tenant (1 query)
    grants_by_tenant = {t.id: [] for t in tenants}
    for g in AIBonusGrant.objects.filter(tenant__in=tenants, is_active=True):
        grants_by_tenant.setdefault(g.tenant_id, []).append(g)

    # 👤 users_count per schema: cache 5min عشان مش هيتغير كل ثانية
    users_count_cache_key = 'super_admin:users_count_by_schema'
    users_count_by_schema = cache.get(users_count_cache_key) or {}
    missing_schemas = [t.schema_name for t in tenants if t.schema_name not in users_count_by_schema]
    if missing_schemas:
        for schema in missing_schemas:
            try:
                with schema_context(schema):
                    users_count_by_schema[schema] = User.objects.count()
            except Exception:
                logger.warning("super_admin_dashboard: failed to count users in schema %s", schema, exc_info=True)
                users_count_by_schema[schema] = 0
        cache.set(users_count_cache_key, users_count_by_schema, 300)

    from clients.services.platform_billing import addons_amount

    tenants_enriched = []
    for t in tenants:
        users_count = users_count_by_schema.get(t.schema_name, 0)
        ai_addon_name = ai_addon_by_tenant.get(t.id, '')

        # 🎁 جلب رصيد الهدايا النشطة (من الـ prefetched dict)
        active_grants = grants_by_tenant.get(t.id, [])
        bonus_designs_remaining = 0
        bonus_whatsapp_remaining = 0
        bonus_watermarks_remaining = 0
        for g in active_grants:
            if not g.is_valid:
                continue
            bonus_designs_remaining += g.remaining_designs
            bonus_whatsapp_remaining += g.remaining_whatsapp
            bonus_watermarks_remaining += g.remaining_watermarks

        tenants_enriched.append({
            'obj': t,
            'users_count': users_count,
            'ai_addon_name': ai_addon_name,
            'bonus_designs': bonus_designs_remaining,
            'bonus_whatsapp': bonus_whatsapp_remaining,
            'bonus_watermarks': bonus_watermarks_remaining,
            'active_grants': active_grants,
            'days_left': (t.subscription_end_date - today).days + 1 if t.subscription_end_date and t.subscription_end_date >= today else (
                (t.trial_ends_at - today).days + 1 if t.status == 'trial' and t.trial_ends_at and t.trial_ends_at >= today else 0
            ),
            # نفس شروط كروت «تنتهي قريباً» و«تجربة منتهية» فوق — عشان الدوس عليها يفلتر الشركات دي بالظبط
            'expiring_soon': bool(t.status == 'active' and t.subscription_end_date
                                  and today <= t.subscription_end_date <= today + timedelta(days=7)),
            'trial_expired': bool(t.status == 'trial' and t.trial_ends_at and t.trial_ends_at < today),
            # 💸 الإضافات المشتراة (موظف/فرع/خزينة) بتتجدد مع الاشتراك — مودال التفعيل لازم يحسبها
            'addons_monthly': addons_amount(t, 1),
            'plan_slug': (_plan_slug_candidates(t.plan) or [''])[-1],
        })

    # --- طلبات سوق في انتظار الموافقة ---
    pending_marketplace_requests = ServiceRequest.objects.filter(
        status='pending_approval'
    ).select_related('customer').order_by('-created_at')[:30]

    # --- طلبات شراء التصاميم المعلّقة ---
    pending_design_purchases = DesignPurchase.objects.filter(
        status__in=['pending', 'awaiting_confirm']
    ).select_related('customer', 'package').order_by('-created_at')[:20]

    # --- 💵 إيصالات الدفع اليدوي (فودافون/إنستاباي) ---
    # 🐛 [FIX]: الإيصال بيتعمل «pending» أول ما العميل يختار فودافون كاش، قبل ما يرفع حاجة —
    #    فاللستة والعداد الأحمر كانوا مليانين إيصالات فاضية زرار «تأكيد» فيها مقفول من غير
    #    سبب واضح (كأنه بايظ). دلوقتي: اللي رفع الصورة + رقم العملية = محتاج قرارك،
    #    واللي لسه مارفعش في قسم لوحده تحت.
    _receipts = ManualPaymentReceipt.objects.filter(status='pending').select_related('customer', 'tenant')
    _uploaded_q = Q(receipt_image__isnull=False) & ~Q(receipt_image='') & ~Q(txn_reference='')
    pending_manual_receipts = list(_receipts.filter(_uploaded_q).order_by('-created_at')[:50])
    unsubmitted_receipts = list(_receipts.exclude(_uploaded_q).order_by('-created_at')[:30])
    uploaded_receipts_count = _receipts.filter(_uploaded_q).count()

    # --- طلبات طباعة التصاميم ---
    # 🐛 [FIX]: كانت بتعرض pending/quoted بس — الطلب اللي العميل قبل عرضه كان بيختفي من اللوحة
    #    ومحدش يقدر يحرّكه للطباعة/الشحن. دلوقتي كل الطلبات الشغالة، والعداد = اللي مستني الإدارة.
    pending_print_requests = list(DesignPrintRequest.objects.filter(
        status__in=['pending', 'quoted', 'accepted', 'in_production', 'shipped']
    ).select_related('customer', 'design').order_by('-created_at')[:50])
    print_needs_action = sum(1 for p in pending_print_requests if p.status in ('pending', 'accepted'))

    # --- 🛍️ عملاء السوق + إحصائيات نشاطهم ---
    # 🐛 [FIX]: «المصروف» كان Sum جوه نفس الـ join مع الطلبات والتصاميم ⇒ المبلغ بيتضرب في عددهم
    #    (عميل دفع 100 وعنده 3 طلبات × 4 تصاميم كان بيظهر 1200)، وكان بيحسب كمان المشتريات
    #    اللي ماتدفعتش أو اترفضت. دلوقتي subquery للمدفوع فعلاً بس.
    from django.db.models import DecimalField, OuterRef, Subquery
    from django.db.models.functions import Coalesce
    paid_sum = (DesignPurchase.objects.filter(customer=OuterRef('pk'), status__in=PAID_PURCHASE_STATUSES)
                .order_by().values('customer').annotate(t=Sum('price_paid')).values('t'))
    marketplace_customers = (
        MarketplaceCustomer.objects.all()
        .annotate(
            requests_count=Count('requests', distinct=True),
            designs_count=Count('designs', distinct=True),
            purchases_count=Count('design_purchases', distinct=True),
            spent_total=Coalesce(Subquery(paid_sum, output_field=DecimalField(max_digits=12, decimal_places=2)),
                                 Decimal('0'), output_field=DecimalField(max_digits=12, decimal_places=2)),
        )
        .order_by('-last_active')[:100]
    )
    customers_summary = {
        'total': MarketplaceCustomer.objects.count(),
        'automotive': MarketplaceCustomer.objects.filter(sector='automotive').count(),
        'printing': MarketplaceCustomer.objects.filter(sector='printing').count(),
        'individuals': MarketplaceCustomer.objects.filter(customer_type='individual').count(),
        'companies': MarketplaceCustomer.objects.filter(customer_type='company').count(),
        'active_today': MarketplaceCustomer.objects.filter(
            last_active__gte=timezone.now() - timedelta(days=1)
        ).count(),
        'blocked': MarketplaceCustomer.objects.filter(is_blocked=True).count(),
        'with_designs': MarketplaceCustomer.objects.filter(designs__isnull=False).distinct().count(),
    }

    # --- 🏢 نشاط الشركات (آخر أحداث + إحصائيات) — bulk aggregates بدل N+1 ---
    schema_names = [t.schema_name for t in tenants]
    # آخر زيارة لكل schema (Postgres DISTINCT ON — كويري واحدة)
    last_visits_by_schema = {}
    for row in (
        VisitorLog.objects.filter(tenant_schema__in=schema_names)
        .order_by('tenant_schema', '-timestamp')
        .distinct('tenant_schema')
        .values('tenant_schema', 'timestamp', 'path')
    ):
        last_visits_by_schema[row['tenant_schema']] = {
            'last_ts': row['timestamp'], 'path': row['path'] or '',
        }

    visits_today_by_schema = {
        r['tenant_schema']: r['n'] for r in
        VisitorLog.objects.filter(tenant_schema__in=schema_names, timestamp__gte=today_start)
        .values('tenant_schema').annotate(n=Count('id'))
    }
    visits_7d_by_schema = {
        r['tenant_schema']: r['n'] for r in
        VisitorLog.objects.filter(tenant_schema__in=schema_names, timestamp__gte=week_ago)
        .values('tenant_schema').annotate(n=Count('id'))
    }
    # آخر 5 أحداث لكل tenant — بنحضّر كل الأحداث الحديثة (آخر 30 يوم) ونوزعها بـ Python
    events_by_schema = {s: [] for s in schema_names}
    for ev in (
        PlatformEvent.objects.filter(
            tenant_schema__in=schema_names, timestamp__gte=month_ago,
        ).order_by('-timestamp')[:500]
    ):
        bucket = events_by_schema.get(ev.tenant_schema)
        if bucket is not None and len(bucket) < 5:
            bucket.append(ev)

    tenant_activity = []
    for t in tenants:
        lv = last_visits_by_schema.get(t.schema_name)
        tenant_activity.append({
            'tenant': t,
            'last_visit_at': lv['last_ts'] if lv else None,
            'last_visit_path': lv['path'] if lv else '',
            'visits_today': visits_today_by_schema.get(t.schema_name, 0),
            'visits_7d': visits_7d_by_schema.get(t.schema_name, 0),
            'last_events': events_by_schema.get(t.schema_name, []),
        })
    # رتب: آخر نشاط أولاً
    from datetime import datetime, timezone as _tz
    _epoch = datetime(1970, 1, 1, tzinfo=_tz.utc)
    tenant_activity.sort(key=lambda x: x['last_visit_at'] or _epoch, reverse=True)

    # --- 🚗 P2P Parts Marketplace — refund requests + recent orders ---
    pending_refund_orders = list(
        PartOrder.objects.filter(status='refund_requested')
        .select_related('listing', 'listing__car_make', 'buyer_customer',
                        'listing__seller_customer', 'listing__seller_tenant')
        .order_by('-created_at')[:30]
    )
    parts_orders_summary = {
        'total':           PartOrder.objects.count(),
        'in_escrow':       PartOrder.objects.filter(status__in=['paid_held', 'shipped', 'delivered']).count(),
        'refund_pending':  PartOrder.objects.filter(status='refund_requested').count(),
        'released_total':  PartOrder.objects.filter(status='released').count(),
    }

    # --- 🛡️ PartListings — pending moderation queue ---
    pending_parts_listings = list(
        PartListing.objects.filter(
            is_deleted=False, moderation_status='pending_approval'
        ).select_related('car_make', 'seller_customer', 'seller_tenant')
        .order_by('-created_at')[:30]
    )
    parts_listings_summary = {
        'pending':   PartListing.objects.filter(is_deleted=False, moderation_status='pending_approval').count(),
        'approved':  PartListing.objects.filter(is_deleted=False, moderation_status='approved').count(),
        'rejected':  PartListing.objects.filter(is_deleted=False, moderation_status='rejected').count(),
        'suspended': PartListing.objects.filter(is_deleted=False, moderation_status='suspended').count(),
    }

    # --- حزم AI المتاحة ---
    ai_addons = list(AIAddonPackage.objects.filter(is_active=True).order_by('sort_order').values('slug', 'name', 'monthly_price'))

    # --- الباقات لمودال «تفعيل اشتراك» ---
    # 💸 [FIX]: المودال كان فيه 3 باقات سيارات بس بأسعار مكتوبة في القالب (475/700/1400) وخصومات
    #    ثابتة (9%/12.5%/25%) — والفاتورة الحقيقية بتتحسب بـ Plan.price_for_period (خصومات الباقة
    #    + العرض الشغّال) + الإضافات. يعني «الإجمالي المطلوب» اللي قدامك ≠ اللي اتسجّل، وشركات
    #    الطباعة مالهاش باقة أصلاً. دلوقتي كل الباقات النشطة من الـ DB بنفس دالة الفاتورة.
    sub_plans = []
    from django.utils.translation import get_language
    _en = (get_language() or 'ar').startswith('en')
    for p in Plan.objects.filter(is_active=True).order_by('industry', 'monthly_price'):
        sub_plans.append({
            # 🌐 الصفحة بالإنجليزي ⇒ الاسم الإنجليزي للباقة لو متسجّل
            'slug': p.slug, 'name': ((p.name_en or p.slug.split('-', 1)[-1].replace('-', ' ').title()) if _en else p.name),
            'industry': p.industry,
            'prices': {str(m): float(p.price_for_period(m)) for m in (1, 3, 6, 12)},
        })

    from clients.models.tenancy import TRIAL_DAYS

    # --- 🔔 «محتاج قرارك» — بلاغات الدعم اللي لسه ماتشافتش (نفس عداد التنبيه اللحظي) ---
    from clients.models import SupportTicket
    support_unseen = SupportTicket.objects.filter(
        is_deleted=False, seen_at__isnull=True).exclude(status='closed').count()
    # 🛍️ طلبات أسعار جديدة من كتالوج السوق (متاجر Mouss Tec)
    from clients.models import CatalogQuoteRequest
    catalog_new = CatalogQuoteRequest.objects.filter(status='new').count()
    # 🏪 بائعين السوق: متاجر جديدة مستنية موافقة + منتجات مستنية مراجعة
    # 🐛 [FIX]: البائع اللي بيسجّل ماكانش بيظهر هنا خالص (كان في أدمن Django بس)
    from clients.models import CatalogItem, MarketSeller
    sellers_pending = MarketSeller.objects.filter(store__is_active=False, is_blocked=False,
                                                  upgraded_to__isnull=True).count()
    seller_items_pending = CatalogItem.objects.filter(pending_review=True).count()
    # 🖨️ سوق المطابع: إيصالات/مشاكل/تحويلات للمطابع — كانت بتوصل إيميل بس (notify_owner)
    #    ومش ظاهرة هنا، فلو الإيميل مش متظبط العميل يدفع والمطبعة ماتبدأش أبداً.
    from print_market.models import MarketOrder, MarketPayment
    pm_payments_pending = MarketPayment.objects.filter(status='pending').count()
    pm_disputes = MarketOrder.objects.filter(status='disputed').count()
    pm_payouts_due = MarketOrder.objects.filter(status='completed', payout_done_at__isnull=True).count()
    # ↩️ طلبات اتلغت لصالح العميل بعد ما دفع — فلوسه لسه على المنصة لحد ما تتسجّل راجعة
    pm_refunds_due = MarketOrder.refunds_due().count()
    attention_total = (
        catalog_new + sellers_pending + seller_items_pending + pm_payments_pending + pm_disputes + pm_payouts_due + pm_refunds_due +
        uploaded_receipts_count + support_unseen + len(pending_refund_orders)
        + len(pending_design_purchases) + len(pending_marketplace_requests)
        + len(pending_parts_listings) + print_needs_action
        + expiring_soon + trial_expired + summary['fraud']
    )

    return render(request, 'clients/super_admin.html', {
        'tenants': tenants_enriched,
        'summary': summary,
        'today': today,
        'admin_url': ADMIN_URL,
        'trial_days': TRIAL_DAYS,
        'new_this_month': new_this_month,
        'new_this_week': new_this_week,
        'expiring_soon': expiring_soon,
        'trial_expired': trial_expired,
        'visitor_stats': visitor_stats,
        'recent_events': recent_events,
        'sub_plans': sub_plans,
        'ai_addons': ai_addons,
        'support_unseen': support_unseen,
        'attention_total': attention_total,
        'pm_payments_pending': pm_payments_pending,
        'pm_disputes': pm_disputes,
        'pm_payouts_due': pm_payouts_due,
        'pm_refunds_due': pm_refunds_due,
        'catalog_new': catalog_new,
        'sellers_pending': sellers_pending,
        'seller_items_pending': seller_items_pending,
        'pending_design_purchases': pending_design_purchases,
        'pending_manual_receipts': pending_manual_receipts,
        'unsubmitted_receipts': unsubmitted_receipts,
        'uploaded_receipts_count': uploaded_receipts_count,
        'pending_print_requests': pending_print_requests,
        'print_needs_action': print_needs_action,
        'pending_marketplace_requests': pending_marketplace_requests,
        'marketplace_customers': marketplace_customers,
        'customers_summary': customers_summary,
        'tenant_activity': tenant_activity,
        'pending_refund_orders': pending_refund_orders,
        'parts_orders_summary': parts_orders_summary,
        'pending_parts_listings': pending_parts_listings,
        'parts_listings_summary': parts_listings_summary,
    })
