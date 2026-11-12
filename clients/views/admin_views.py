"""
Super-admin / platform-owner endpoints — the dashboard at /superadmin/,
per-tenant detail and tenant-grants editing, the "enter tenant" jump
into a tenant schema, and the legacy /impersonate-login entry point.

Every endpoint in this module assumes public-schema + superuser.
"""
from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from clients.permissions import platform_owner_required
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.db import connection, models, transaction
from django.db.models import Avg, Count, F, Sum, Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _t
from django.views.decorators.csrf import csrf_exempt
from django_tenants.utils import schema_context

from clients.models import (
    AIAddonPackage,
    AIBonusGrant,
    BidOffer,
    BlindBiddingRequest,
    Client,
    CustomerNotification,
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
    TenderOffer,
    VisitorLog,
)

from ._shared import _is_platform_owner
from erp_core.localization import current_tenant_symbol as _sym

logger = logging.getLogger('mouss_tec_core')
User = get_user_model()
ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal')


def _plan_slug_candidates(value):
    """slug الباقة نفسه + المقابل بتاعه لو القيمة من Client.plan القديمة ('gold' ⇒ 'auto-gold')."""
    from tenancy.services.plan_mapping import resolve_plan_slug
    value = (value or '').strip()
    return [v for v in (value, resolve_plan_slug(value)) if v]


def _resolve_active_plan(value):
    """أول باقة نشطة بالترتيب: الـ slug بالظبط، وبعدين المقابل القديم — ترتيب ثابت مش حسب الـ DB."""
    for slug in _plan_slug_candidates(value):
        plan = Plan.objects.filter(slug=slug, is_active=True).first()
        if plan is not None:
            return plan
    return None


# مشتريات باقات التصاميم اللي اتدفعت فعلاً (المستهلكة بالكامل اتدفعت برضه)
PAID_PURCHASE_STATUSES = ('paid', 'exhausted')


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
    attention_total = (
        catalog_new +
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
        'catalog_new': catalog_new,
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


# =====================================================================
# 👤 تفاصيل عميل السوق (Marketplace Customer Detail - AJAX HTML)
# =====================================================================

@login_required
@platform_owner_required
def super_admin_customer_detail(request, customer_id):
    """يُرجع HTML بتفاصيل العميل + كل نشاطه (designs, purchases, requests, chats)."""
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access Denied')

    customer = get_object_or_404(MarketplaceCustomer, pk=customer_id)
    designs = list(customer.designs.select_related('purchase__package').order_by('-created_at')[:50])
    purchases = list(customer.design_purchases.select_related('package').order_by('-created_at')[:20])
    requests_list = list(customer.requests.order_by('-created_at')[:20])

    # إجماليات
    # 🐛 [FIX]: الباقة اللي اتدفعت واتصرفت كلها (exhausted) ماكانتش بتتحسب في «إجمالي المدفوع»
    total_spent = sum((p.price_paid for p in purchases if p.status in PAID_PURCHASE_STATUSES), Decimal('0'))
    total_designs = len(designs)
    total_purchases = len(purchases)

    # 🧭 رحلة العميل على الموقع (الصفحات اللي فتحها وآخر صفحة وقف عندها)
    from clients.services.customer_journey import build_customer_journey
    journey = build_customer_journey(customer)

    return render(request, 'clients/super_admin_customer_detail.html', {
        'journey': journey,
        'customer': customer,
        'designs': designs,
        'purchases': purchases,
        'requests_list': requests_list,
        'total_spent': total_spent,
        'total_designs': total_designs,
        'total_purchases': total_purchases,
    })


# =====================================================================
# 🎁 API: قائمة الهدايا النشطة لشركة معينة (JSON) — للمودال في السوبر أدمن
# =====================================================================
@login_required
@platform_owner_required
def super_admin_tenant_grants(request, tenant_id):
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access Denied')
    tenant = get_object_or_404(Client, pk=tenant_id)
    grants = AIBonusGrant.objects.filter(tenant=tenant, is_active=True).order_by('granted_at')
    data = []
    for g in grants:
        if not g.is_valid:
            continue
        data.append({
            'id': g.pk,
            'reason': g.reason or '',
            'is_welcome': (g.granted_by_id is None and 'ترحيب' in (g.reason or '')),
            'granted_designs': g.granted_designs,
            'granted_whatsapp': g.granted_whatsapp,
            'granted_watermarks': g.granted_watermarks,
            'remaining_designs': g.remaining_designs,
            'remaining_whatsapp': g.remaining_whatsapp,
            'remaining_watermarks': g.remaining_watermarks,
            'granted_at': g.granted_at.strftime('%Y-%m-%d %H:%M'),
        })
    return JsonResponse({'grants': data})


# =====================================================================
# 🚪 الدخول كمالك المنصة على أي شركة (Tenant Impersonation)
# =====================================================================

@login_required
@platform_owner_required
def enter_tenant(request, schema_name):
    """
    Super Admin → يدخل على أي شركة مباشرة.
    يولّد توكن دخول مؤقت (صالح 60 ثانية) ويحول للـ subdomain.
    الـ impersonation view على الـ tenant يتحقق من التوكن ويعمل login تلقائي.
    """
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access Denied')

    tenant = get_object_or_404(Client, schema_name=schema_name)
    domain = Domain.objects.filter(tenant=tenant).first()
    if not domain:
        messages.error(request, f'لا يوجد نطاق مسجل لشركة «{tenant.name}».')
        return redirect('super_admin_dashboard')

    # --- إنشاء توكن دخول مؤقت (self-contained signed token) ---
    # ⚠️ لا نستخدم cache لأن الـ cache key function تضيف schema_name
    # فالتوكن المحفوظ على public لا يُقرأ من tenant schema.
    # بدلاً من ذلك: Django Signing — التوكن يحتوي على البيانات مشفرة.
    from django.core import signing
    import time

    token = signing.dumps({
        'schema_name': schema_name,
        'superuser_id': request.user.id,
        'superuser_name': request.user.username,
        'created': int(time.time()),
    }, salt='impersonate-login-token')

    # --- Log the impersonation ---
    PlatformEvent.objects.create(
        event_type='login',
        tenant_schema=schema_name,
        tenant_name=tenant.name,
        user_name=request.user.username,
        description=f"دخول Super Admin «{request.user.username}» على شركة «{tenant.name}»",
    )

    protocol = 'https' if request.is_secure() else 'http'
    target_url = f'{protocol}://{domain.domain}/impersonate-login/?token={token}'

    return redirect(target_url)


@csrf_exempt
def impersonate_login(request):
    """
    GET /impersonate-login/?token=xxx
    يُستدعى من الـ tenant subdomain — يتحقق من التوكن ويعمل login تلقائي كأدمن.
    """
    token = request.GET.get('token', '').strip()
    admin_url = os.getenv('ADMIN_URL', 'secure-portal')
    if not token:
        # No token — redirect to login page instead of blank forbidden
        return redirect(f'/{admin_url}/login/')

    from django.core import signing
    try:
        token_data = signing.loads(token, salt='impersonate-login-token', max_age=120)
    except (signing.BadSignature, signing.SignatureExpired):
        return redirect(f'/{admin_url}/login/?msg=token_expired')

    # 🐛 [FIX]: التوكن كان ينفع يتعاد استخدامه طول الـ 120 ثانية (لو اتسرّب من لوج/Referer/History
    # أي حد يدخل كأدمن). دلوقتي استخدام واحد بس — cache.add ذرّي (atomic) على Redis.
    import hashlib
    from django.core.cache import cache
    _used_key = 'impersonate_used:' + hashlib.sha256(token.encode()).hexdigest()
    if not cache.add(_used_key, 1, timeout=180):
        return redirect(f'/{admin_url}/login/?msg=token_expired')

    schema_name = token_data.get('schema_name', '')
    current_schema = getattr(connection, 'schema_name', 'public')

    if current_schema == 'public' or current_schema != schema_name:
        return redirect(f'/{admin_url}/login/')

    # --- إيجاد أو إنشاء admin user على هذا الـ tenant ---
    from django.contrib.auth import login as auth_login, logout as auth_logout

    # 🛡️ تنظيف الـ session الحالي قبل الدخول لمنع تسرب بيانات من session سابق
    # (السبب وراء «أحياناً بيدخل علي صفحة تانية» — session قديم لمستخدم آخر)
    if request.user.is_authenticated:
        auth_logout(request)
    request.session.flush()

    # 🎯 ابحث أولاً عن أدمن الشركة الحقيقي عن طريق إيميل الشركة (أدق من «آخر staff»)
    admin_user = None
    try:
        with schema_context('public'):
            tenant_obj = Client.objects.filter(schema_name=schema_name).first()
        tenant_email = (getattr(tenant_obj, 'email', '') or '').strip().lower()
        if tenant_email:
            admin_user = User.objects.filter(
                email__iexact=tenant_email, is_active=True
            ).first()
    except Exception:
        logger.warning(
            "enter_tenant: failed to look up tenant admin by email for schema=%s",
            schema_name, exc_info=True,
        )
        admin_user = None

    # 🔁 fallback: لو ملقيناش، استخدم أقدم superuser (مالك الشركة غالباً أول واحد)
    if not admin_user:
        admin_user = (
            User.objects.filter(is_superuser=True, is_active=True)
            .exclude(username__startswith='mousstec_admin_')
            .order_by('date_joined')
            .first()
        )

    # 🔁 fallback أخير: أقدم staff (مع استبعاد auto-created admins)
    if not admin_user:
        admin_user = (
            User.objects.filter(is_staff=True, is_active=True)
            .exclude(username__startswith='mousstec_admin_')
            .order_by('date_joined')
            .first()
        )

    if not admin_user:
        # ⚠️ Tenant ليس له أدمن حقيقي — ننشئ بمعرّف مميز عشوائي (مش اسم ثابت قابل للتخمين)
        # ونسجّل الحدث للمراقبة الأمنية.
        from django.db import connection as _conn
        admin_user = User.objects.create_superuser(
            username=f"mousstec_admin_{secrets.token_hex(4)}",
            email=f"admin+{_conn.schema_name}@mousstec.com",
            password=secrets.token_urlsafe(32),
            first_name="Mouss Tec",
            last_name="Platform Admin",
        )
        logger.warning(
            f"🔐 [IMPERSONATE]: Auto-created tenant admin '{admin_user.username}' "
            f"on schema '{_conn.schema_name}' (tenant had no staff)"
        )

    # Login
    auth_login(request, admin_user, backend='clients.backends.CaseInsensitiveEmailBackend')
    # 🎥 صاحب المنصة داخل على الشركة ⇒ مايتسجّلش في «تسجيل الشاشة» كأنه موظف عندهم
    request.session['platform_owner_entered'] = True

    admin_url = os.getenv('ADMIN_URL', 'secure-portal')
    return redirect(f'/{admin_url}/')


# =====================================================================
# 🛍️ Marketplace Customer admin actions — Delete / Gift / Notify
# =====================================================================

def _require_superadmin(request):
    """Common guard: must be public schema + authenticated superuser. Returns
    an HttpResponseForbidden when the check fails, otherwise None."""
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access Denied (tenant schema)')
    if not request.user.is_authenticated or not request.user.is_superuser:
        return HttpResponseForbidden('Superuser only')
    return None


@login_required
@platform_owner_required
def super_admin_customer_delete(request, customer_id):
    """
    🗑️ Hard-delete a MarketplaceCustomer + all related data.
    Requires POST with confirm_phone matching the customer's phone number.
    """
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    customer = get_object_or_404(MarketplaceCustomer, pk=customer_id)
    confirm_phone = (request.POST.get('confirm_phone') or '').strip()
    if confirm_phone != customer.phone:
        return JsonResponse({
            'error': 'رقم التأكيد لا يطابق رقم العميل.',
        }, status=400)

    label = customer.company_name or customer.full_name
    phone = customer.phone
    try:
        with transaction.atomic():
            customer.delete()  # cascades through notifications, designs, purchases, requests
            PlatformEvent.objects.create(
                event_type='other',
                tenant_schema='public',
                tenant_name='marketplace',
                user_name=request.user.username,
                description=f"🗑️ حذف عميل سوق «{label}» ({phone}) نهائياً.",
            )
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("[SUPER ADMIN] Failed to delete customer %s: %s", customer_id, exc)
        return JsonResponse({'error': f'فشل الحذف: {exc}'}, status=500)

    return JsonResponse({
        'ok': True,
        'message': f'تم حذف العميل «{label}» نهائياً.',
    })


@login_required
@platform_owner_required
def super_admin_customer_gift(request, customer_id):
    """
    🎁 Grant free design credits to a marketplace customer.
    POST: designs=<int>, reason=<str>
    """
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    customer = get_object_or_404(MarketplaceCustomer, pk=customer_id)
    try:
        designs = max(int(request.POST.get('designs', 0) or 0), 0)
    except ValueError:
        designs = 0
    reason = (request.POST.get('reason') or '').strip()

    if designs <= 0:
        return JsonResponse({'error': 'لازم تحدد عدد تصاميم أكبر من صفر.'}, status=400)

    with transaction.atomic():
        # Grant: increase total free designs (does not affect already-used count).
        customer.free_designs_total = (customer.free_designs_total or 0) + designs
        customer.save(update_fields=['free_designs_total'])

        # Notify the customer in-app
        CustomerNotification.objects.create(
            customer=customer,
            title=f'🎁 هدية: {designs} تصميم مجاني',
            body=reason or f'تم منحك {designs} تصميم مجاني من إدارة منصة Mouss Tec. اضغط للتصميم الآن.',
            level='success',
            icon='fa-gift',
            action_url='/marketplace/design-store/',
            action_label='ابدأ التصميم',
            sent_by=request.user,
        )

        PlatformEvent.objects.create(
            event_type='other', tenant_schema='public', tenant_name='marketplace',
            user_name=request.user.username,
            description=f"🎁 هدية {designs} تصميم لعميل سوق «{customer.full_name}» ({customer.phone})",
        )

    return JsonResponse({
        'ok': True,
        'message': f'تم منح «{customer.full_name}» هدية {designs} تصميم مجاني.',
        'new_total': customer.free_designs_total,
        'remaining': customer.free_designs_remaining,
    })


@login_required
@platform_owner_required
def super_admin_customer_notify(request, customer_id):
    """
    🔔 Send an in-app notification to a marketplace customer.
    POST: title, body, level (info|success|warning|danger), icon, action_url, action_label
    """
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    customer = get_object_or_404(MarketplaceCustomer, pk=customer_id)
    title = (request.POST.get('title') or '').strip()
    body = (request.POST.get('body') or '').strip()
    level = (request.POST.get('level') or 'info').strip()
    icon = (request.POST.get('icon') or 'fa-bell').strip()
    action_url = (request.POST.get('action_url') or '').strip()
    action_label = (request.POST.get('action_label') or '').strip()

    if not title or not body:
        return JsonResponse({'error': 'العنوان والنص مطلوبين.'}, status=400)
    if level not in {'info', 'success', 'warning', 'danger'}:
        level = 'info'

    notif = CustomerNotification.objects.create(
        customer=customer,
        title=title[:200],
        body=body,
        level=level,
        icon=icon[:50] or 'fa-bell',
        action_url=action_url[:300],
        action_label=action_label[:80],
        sent_by=request.user,
    )

    PlatformEvent.objects.create(
        event_type='other', tenant_schema='public', tenant_name='marketplace',
        user_name=request.user.username,
        description=f"🔔 إشعار «{title[:60]}» إلى عميل «{customer.full_name}» ({customer.phone})",
    )

    return JsonResponse({
        'ok': True,
        'message': 'تم إرسال الإشعار.',
        'notification_id': notif.pk,
    })


# =====================================================================
# 🔔 Customer-facing notifications API — used by /marketplace/ dashboard
# =====================================================================

def customer_notifications_list(request):
    """Return the current marketplace customer's notifications as JSON."""
    from clients.views._shared import _marketplace_auth
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'not_authenticated'}, status=401)

    qs = customer.notifications.order_by('-created_at')[:30]
    items = [{
        'id': n.pk,
        'title': n.title,
        'body': n.body,
        'level': n.level,
        'icon': n.icon or 'fa-bell',
        'action_url': n.action_url,
        'action_label': n.action_label,
        'created_at': n.created_at.isoformat(),
        'is_read': n.is_read,
    } for n in qs]
    unread = customer.notifications.filter(read_at__isnull=True).count()
    return JsonResponse({'items': items, 'unread': unread})


def customer_notification_read(request, notification_id):
    """Mark a single notification as read."""
    from clients.views._shared import _marketplace_auth
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'not_authenticated'}, status=401)
    try:
        n = customer.notifications.get(pk=notification_id)
    except CustomerNotification.DoesNotExist:
        return JsonResponse({'error': 'not_found'}, status=404)
    n.mark_read()
    return JsonResponse({'ok': True})


# =====================================================================
# 🚗 Admin: Parts marketplace dispute / refund resolution
# =====================================================================

@login_required
@platform_owner_required
def super_admin_parts_refund_approve(request, order_code):
    """
    ✅ Approve a buyer's refund request — refunds the buyer (escrow → buyer).
    POST: admin_note (optional).
    """
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    order = get_object_or_404(PartOrder, order_code=order_code)
    if order.status != 'refund_requested':
        return JsonResponse({'error': f'الحالة الحالية لا تسمح ({order.get_status_display()}).'}, status=400)

    note = (request.POST.get('admin_note') or '').strip()
    # Who pays return shipping is decided by the reason category — never the
    # platform (see escrow.RETURN_REASON_TO_PAYER). Admin may override the
    # category the buyer picked.
    return_reason = (request.POST.get('return_reason') or order.return_reason or 'defective').strip()
    from clients.services import escrow as escrow_svc
    from django.core.exceptions import ValidationError as _DjVE

    try:
        with transaction.atomic():
            # 🐛 [FIX]: الموافقة كانت بتقلب حالة الطلب بس، والـ EscrowHold كان
            #    بيفضل "محجوز" للأبد — السجل المالي مش مطابق للواقع.
            escrow_svc.refund_to_buyer(order, return_reason=return_reason, by_user=request.user)
            order.status = 'refunded'
            order.refunded_at = timezone.now()
            order.admin_notes = (order.admin_notes + '\n' if order.admin_notes else '') + f'[REFUND APPROVED] {note}'
            order.save(update_fields=['status', 'refunded_at', 'admin_notes'])
    except _DjVE as exc:
        return JsonResponse({'error': '; '.join(exc.messages)}, status=400)

    payer_label = 'عليك' if order.return_shipping_payer == 'buyer' else 'على البائع'
    seller_payer_label = 'عليك' if order.return_shipping_payer == 'seller' else 'على المشتري'
    # Notify buyer
    if order.buyer_customer_id:
        CustomerNotification.objects.create(
            customer=order.buyer_customer,
            title='✅ تم قبول طلب الإرجاع',
            body=(f'هنرجع لك مبلغ {order.amount_paid} {_sym()} خلال 3-5 أيام عمل. '
                  f'شحن الإرجاع {payer_label}.'),
            level='success', icon='fa-rotate-left',
            action_url='/marketplace/parts/orders/', action_label='طلباتي',
        )
    # Notify seller
    if order.listing.seller_customer_id:
        CustomerNotification.objects.create(
            customer=order.listing.seller_customer,
            title='⚠️ تم قبول طلب الإرجاع',
            body=(f'الإدارة وافقت على إرجاع «{order.listing.title}». تواصل مع المشتري لاسترداد القطعة. '
                  f'شحن الإرجاع {seller_payer_label}.'),
            level='warning', icon='fa-rotate-left',
            action_url='/marketplace/parts/sales/', action_label='مبيعاتي',
        )
    PlatformEvent.objects.create(
        event_type='other', tenant_schema='public', tenant_name='parts_market',
        user_name=request.user.username,
        description=f"✅ موافقة إرجاع طلب {order.order_code} — {order.amount_paid} {_sym()} إلى المشتري",
    )

    return JsonResponse({'ok': True, 'message': 'تم قبول الإرجاع وإشعار الطرفين.'})


@login_required
@platform_owner_required
def super_admin_parts_refund_reject(request, order_code):
    """
    ❌ Reject the refund request → releases funds to seller (admin decided
    the part is as described). POST: admin_note (required, ≥ 10 chars).
    """
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    order = get_object_or_404(PartOrder, order_code=order_code)
    if order.status != 'refund_requested':
        return JsonResponse({'error': f'الحالة الحالية لا تسمح ({order.get_status_display()}).'}, status=400)

    note = (request.POST.get('admin_note') or '').strip()
    if len(note) < 10:
        return JsonResponse({'error': 'لازم تكتب سبب الرفض بالتفصيل (10 حروف على الأقل).'}, status=400)

    from clients.services import escrow as escrow_svc
    from django.core.exceptions import ValidationError as _DjVE
    try:
        with transaction.atomic():
            # 🐛 [FIX]: الرفض كان بيقلب الطلب "released" من غير ما يحرّك
            #    الـ EscrowHold — الفلوس كانت بتفضل محجوزة في السجلات.
            escrow_svc.release_to_seller(order, by_user=request.user, reason='refund request rejected by admin')
            order.status = 'released'
            order.released_at = timezone.now()
            order.admin_notes = (order.admin_notes + '\n' if order.admin_notes else '') + f'[REFUND REJECTED] {note}'
            order.save(update_fields=['status', 'released_at', 'admin_notes'])
    except _DjVE as exc:
        return JsonResponse({'error': '; '.join(exc.messages)}, status=400)

    with transaction.atomic():
        # Notify both parties
        if order.buyer_customer_id:
            CustomerNotification.objects.create(
                customer=order.buyer_customer,
                title='❌ تم رفض طلب الإرجاع',
                body=f'الإدارة قررت أن «{order.listing.title}» مطابق للوصف. السبب: {note[:150]}',
                level='danger', icon='fa-circle-xmark',
            )
        if order.listing.seller_customer_id:
            CustomerNotification.objects.create(
                customer=order.listing.seller_customer,
                title='💰 تم تحرير أموالك',
                body=f'الإدارة رفضت طلب الإرجاع. المبلغ {order.seller_payout} {_sym()} في طريقه لحسابك.',
                level='success', icon='fa-money-bill-wave',
            )

        PlatformEvent.objects.create(
            event_type='other', tenant_schema='public', tenant_name='parts_market',
            user_name=request.user.username,
            description=f"❌ رفض إرجاع طلب {order.order_code} — تحرير {order.seller_payout} {_sym()} للبائع",
        )

    return JsonResponse({'ok': True, 'message': 'تم رفض الإرجاع وتحرير أموال البائع.'})



# ============================================================================
# 🎁 Superadmin — Gift / Renew Diagnostics Subscription
# ============================================================================

@login_required
@platform_owner_required
def super_admin_gift_diagnostics(request):
    """
    GET  → render the gift-diagnostics wizard (phone lookup + tier/duration picker).
    POST → activate or renew a CustomerDiagnosticsSubscription.
    AJAX GET ?phone=… → return customer + subscription status JSON.
    """
    from clients.models import CustomerDiagnosticsSubscription
    from dateutil.relativedelta import relativedelta

    TIERS = [
        {'key': 'basic',  'label': 'Basic',  'price': 99,  'scans': 30,  'color': '#3b82f6', 'icon': 'fa-circle'},
        {'key': 'pro',    'label': 'Pro',    'price': 199, 'scans': 100, 'color': '#8b5cf6', 'icon': 'fa-star'},
        {'key': 'empire', 'label': 'Empire', 'price': 399, 'scans': None,'color': '#f59e0b', 'icon': 'fa-crown'},
    ]
    DURATIONS = [
        {'months': 1,  'label': 'شهر',       'discount': 0},
        {'months': 3,  'label': '3 أشهر',    'discount': 10},
        {'months': 6,  'label': '6 أشهر',    'discount': 15},
        {'months': 12, 'label': 'سنة كاملة', 'discount': 20},
    ]

    # ── AJAX lookup ──────────────────────────────────────────────────────────
    if request.method == 'GET' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        phone = (request.GET.get('phone') or '').strip()
        if not phone:
            return JsonResponse({'found': False, 'error': 'أدخل رقم الهاتف'})
        with schema_context('public'):
            customer = MarketplaceCustomer.objects.filter(phone=phone).first()
            if not customer:
                return JsonResponse({'found': False})
            sub = CustomerDiagnosticsSubscription.objects.filter(customer=customer).order_by('-paid_until').first()
            now = timezone.now()
            return JsonResponse({
                'found': True,
                'name': customer.full_name,
                'phone': customer.phone,
                'customer_id': customer.pk,
                'tier': sub.tier if sub else None,
                'tier_display': sub.get_tier_display() if sub else 'لا يوجد',
                'is_active': bool(sub and sub.paid_until and sub.paid_until > now),
                'days_remaining': max((sub.paid_until - now).days, 0) if (sub and sub.paid_until and sub.paid_until > now) else 0,
                'paid_until': sub.paid_until.strftime('%Y-%m-%d') if (sub and sub.paid_until) else None,
            })

    # ── POST: activate/renew ─────────────────────────────────────────────────
    if request.method == 'POST':
        phone     = (request.POST.get('phone') or '').strip()
        tier      = (request.POST.get('tier') or '').strip()
        try:
            months = int(request.POST.get('months') or 1)
        except (TypeError, ValueError):
            months = 0  # rejected below as invalid
        note      = (request.POST.get('note') or '').strip()

        valid_tiers    = {t['key'] for t in TIERS}
        valid_months   = {d['months'] for d in DURATIONS}
        discount_map   = {d['months']: d['discount'] for d in DURATIONS}
        price_map      = {t['key']: t['price'] for t in TIERS}

        if tier not in valid_tiers or months not in valid_months:
            return JsonResponse({'ok': False, 'error': 'بيانات غير صحيحة'}, status=400)

        with schema_context('public'):
            customer = MarketplaceCustomer.objects.filter(phone=phone).first()
            if not customer:
                return JsonResponse({'ok': False, 'error': 'العميل غير موجود'}, status=404)

            now        = timezone.now()
            discount   = discount_map[months]
            unit_price = Decimal(str(price_map[tier]))
            total      = (unit_price * months * (Decimal('100') - Decimal(str(discount))) / Decimal('100')).quantize(Decimal('0.01'))

            sub = CustomerDiagnosticsSubscription.objects.filter(customer=customer).order_by('-paid_until').first()
            base = max(sub.paid_until, now) if (sub and sub.paid_until and sub.paid_until > now) else now

            new_expiry = base + relativedelta(months=months)

            if sub and sub.tier == tier:
                sub.paid_until = new_expiry
                sub.save(update_fields=['paid_until'])
            else:
                sub, _ = CustomerDiagnosticsSubscription.objects.update_or_create(
                    customer=customer,
                    defaults={'tier': tier, 'paid_until': new_expiry},
                )

            # In-app notification to the customer
            CustomerNotification.objects.create(
                customer=customer,
                title=f'🎁 هدية اشتراك تشخيص {sub.get_tier_display()}',
                body=(
                    note or
                    f'تم تفعيل باقة {sub.get_tier_display()} لمدة '
                    f'{"شهر" if months == 1 else f"{months} أشهر"} '
                    f'(تنتهي {new_expiry.strftime("%Y-%m-%d")}) — بعناية إدارة Mouss Tec.'
                ),
                level='success',
                icon='fa-gift',
                action_url='/marketplace/diagnostics/',
                action_label='ابدأ التشخيص',
                sent_by=request.user,
            )

            PlatformEvent.objects.create(
                event_type='other', tenant_schema='public', tenant_name='marketplace',
                user_name=request.user.username,
                description=(
                    f"🎁 هدية اشتراك تشخيص «{sub.get_tier_display()}» × {months} شهر "
                    f"لعميل «{customer.full_name}» ({customer.phone}) — قيمة {total} {_sym()}"
                ),
            )

        return JsonResponse({
            'ok': True,
            'message': f'تم تفعيل باقة {sub.get_tier_display()} للعميل «{customer.full_name}» حتى {new_expiry.strftime("%Y-%m-%d")}.',
            'paid_until': new_expiry.strftime('%Y-%m-%d'),
        })

    # ── GET: render page ─────────────────────────────────────────────────────
    now = timezone.now()
    companies = Client.objects.filter(is_deleted=False).order_by('name').values(
        'id', 'name', 'schema_name', 'industry', 'plan',
        'has_obd_access', 'obd_access_expiry', 'subscription_end_date',
    )
    companies_list = []
    for c in companies:
        obd_valid = c['has_obd_access'] and (
            c['obd_access_expiry'] is None or c['obd_access_expiry'] > now
        )
        companies_list.append({**c, 'obd_valid': obd_valid})

    return render(request, 'clients/super_admin_gift_diagnostics.html', {
        'tiers': TIERS,
        'durations': DURATIONS,
        'companies': companies_list,
    })


# ============================================================================
# 🎁 Superadmin — Quick OBD Grant (AJAX) for gift-diagnostics page
# ============================================================================

@login_required
@platform_owner_required
def super_admin_obd_quick_grant(request, tenant_id):
    """
    AJAX POST — grant OBD access to a tenant directly from the gift page.
    POST body: duration = 1d | 1w | 1m | 3m | 6m | 1y | lifetime
    Returns JSON {ok, message, new_expiry}
    """
    from datetime import timedelta

    _DURATIONS = {
        '1d':       (timedelta(days=1),   'يوم واحد'),
        '1w':       (timedelta(days=7),   'أسبوع'),
        '1m':       (timedelta(days=30),  'شهر'),
        '3m':       (timedelta(days=90),  '3 أشهر'),
        '6m':       (timedelta(days=180), '6 أشهر'),
        '1y':       (timedelta(days=365), 'سنة'),
        'lifetime': (None,                'مدى الحياة'),
    }

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    tenant = get_object_or_404(Client, pk=tenant_id)
    key = (request.POST.get('duration') or '').strip()
    if key not in _DURATIONS:
        return JsonResponse({'ok': False, 'error': 'مدة غير صحيحة'}, status=400)

    delta, label = _DURATIONS[key]
    tenant.grant_obd_access(delta, by_user=request.user)

    new_expiry = None
    if tenant.obd_access_expiry:
        new_expiry = tenant.obd_access_expiry.strftime('%Y-%m-%d')

    PlatformEvent.objects.create(
        event_type='other', tenant_schema='public', tenant_name=tenant.schema_name,
        user_name=request.user.username,
        description=f"🎁 منح OBD لـ «{tenant.name}» لمدة {label} بواسطة {request.user.username}",
    )

    logger.warning("OBD quick-grant tenant=%s duration=%s by=%s", tenant.schema_name, key, request.user.username)

    return JsonResponse({
        'ok': True,
        'message': f'✅ تم منح وصول OBD لـ «{tenant.name}» ({label})',
        'new_expiry': new_expiry or 'مدى الحياة',
        'tenant_id': tenant_id,
    })


# ============================================================================
# 🎁 Superadmin — Quick Diagnostics Top-up Grant (AJAX) for gift-diagnostics page
# ============================================================================

@login_required
@platform_owner_required
def super_admin_diag_topup_grant(request, tenant_id):
    """
    AJAX POST — gift diagnostics scan credits to a tenant.
    POST body: uses = integer (number of scans to add to diag_topup_balance)
    Returns JSON {ok, message, new_balance}

    ده بيحل مشكلة "الحصة منتهية ولا يوجد رصيد شحن" — السوبر أدمن
    يقدر يدي الشركة رصيد فحوصات مجاني من غير ما تدفع.
    """
    from clients.services.diagnostics_quota import add_topup

    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST only'}, status=405)

    tenant = get_object_or_404(Client, pk=tenant_id)

    try:
        uses = int(request.POST.get('uses') or 0)
    except (ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'عدد الفحوصات غير صحيح'}, status=400)

    if uses <= 0 or uses > 1000:
        return JsonResponse({'ok': False, 'error': 'عدد الفحوصات لازم يكون بين 1 و 1000'}, status=400)

    new_balance = add_topup(tenant, uses)

    PlatformEvent.objects.create(
        event_type='other', tenant_schema='public', tenant_name=tenant.schema_name,
        user_name=request.user.username,
        description=(
            f"🎁 شحن رصيد تشخيص لـ «{tenant.name}»: +{uses} فحص "
            f"(الرصيد الجديد {new_balance}) بواسطة {request.user.username}"
        ),
    )

    logger.warning(
        "diag-topup gift tenant=%s uses=+%d new_balance=%d by=%s",
        tenant.schema_name, uses, new_balance, request.user.username,
    )

    return JsonResponse({
        'ok': True,
        'message': f'✅ تم شحن {uses} فحص لـ «{tenant.name}» (الرصيد {new_balance})',
        'new_balance': new_balance,
        'tenant_id': tenant_id,
    })


# =====================================================================
# 🖨️ طلبات طباعة التصاميم — عرض سعر ومتابعة لحد التسليم
# =====================================================================
# 🐛 [FIX]: العميل كان بيبعت التصميم للطباعة وعنده صفحة بتتابع ٦ مراحل
#    (عرض سعر ← قبول ← طباعة ← شحن ← تسليم) — بس مفيش أي زرار في أي مكان
#    للإدارة تبعت سعر أو تحرّك الطلب، ولا العميل يقبل العرض. كل الطلبات كانت
#    بتفضل «في الانتظار» للأبد. الانتقالات المسموحة:
PRINT_TRANSITIONS = {
    'quote':         ({'pending', 'quoted'}, 'quoted'),
    'in_production': ({'accepted'}, 'in_production'),
    'shipped':       ({'in_production'}, 'shipped'),
    'delivered':     ({'shipped'}, 'delivered'),
    'cancel':        ({'pending', 'quoted', 'accepted', 'in_production'}, 'cancelled'),
}
_PRINT_NOTICE = {
    'in_production': ('🖨️ طلب الطباعة اتبدأ', 'طلبك «{p}» دخل الطباعة.', 'info', 'fa-print'),
    'shipped':       ('🚚 طلب الطباعة اتشحن', 'طلبك «{p}» اتشحن وفي الطريق ليك.', 'info', 'fa-truck'),
    'delivered':     ('📦 طلب الطباعة اتسلّم', 'طلبك «{p}» اتسلّم. شكراً لثقتك 🙏', 'success', 'fa-box-open'),
}


@login_required
@platform_owner_required
def super_admin_print_request_update(request, request_id):
    """POST action ∈ quote(price, note) | in_production | shipped | delivered | cancel(note) → JSON."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    from clients.services.customer_notify import notify_customer
    action = (request.POST.get('action') or '').strip()
    note = (request.POST.get('note') or '').strip()[:1000]
    if action not in PRINT_TRANSITIONS:
        return JsonResponse({'error': 'إجراء غير معروف.'}, status=400)
    allowed_from, new_status = PRINT_TRANSITIONS[action]
    with transaction.atomic():
        pr = get_object_or_404(DesignPrintRequest.objects.select_for_update().select_related('customer'),
                               pk=request_id)
        if pr.status not in allowed_from:
            return JsonResponse({'error': f'مينفعش من حالة «{pr.get_status_display()}».'}, status=409)
        fields = ['status', 'updated_at']
        if action == 'quote':
            try:
                price = Decimal((request.POST.get('price') or '').strip())
            except Exception:
                price = Decimal('0')
            if price <= 0:
                return JsonResponse({'error': 'اكتب سعر أكبر من صفر.'}, status=400)
            pr.quoted_price = price.quantize(Decimal('0.01'))
            fields.append('quoted_price')
        if note:
            pr.admin_notes = note
            fields.append('admin_notes')
        pr.status = new_status
        pr.save(update_fields=fields)

    product = pr.get_product_type_display()
    url = '/marketplace/design-store/print-orders/'
    if action == 'quote':
        notify_customer(
            pr.customer, title='💰 عرض سعر لطلب الطباعة',
            body=(f'طلبك «{product}» × {pr.quantity}: السعر {pr.quoted_price:,.2f} {_sym()}.'
                  + (f' {note}' if note else '') + ' ادخل وافق على العرض أو ارفضه.'),
            level='info', icon='fa-tags', action_url=url, action_label='شوف العرض', sent_by=request.user)
    elif action == 'cancel':
        notify_customer(
            pr.customer, title='❌ طلب الطباعة اتلغى',
            body=f'طلبك «{product}» اتلغى' + (f': {note}' if note else '.'),
            level='warning', icon='fa-circle-xmark', action_url=url, action_label='طلباتي', sent_by=request.user)
    else:
        title, body, level, icon = _PRINT_NOTICE[action]
        notify_customer(pr.customer, title=title, body=body.format(p=product), level=level, icon=icon,
                        action_url=url, action_label='تابع الطلب', sent_by=request.user)
    PlatformEvent.objects.create(
        event_type='other', tenant_schema='public', tenant_name='marketplace', user_name=request.user.username,
        description=f"🖨️ طلب طباعة #{pr.pk} ({product}) → {pr.get_status_display()}"
                    + (f" — {pr.quoted_price} {_sym()}" if action == 'quote' else ''),
    )
    return JsonResponse({'ok': True, 'status': pr.status, 'status_label': pr.get_status_display(),
                         'message': f'تم — الطلب بقى «{pr.get_status_display()}» والعميل اتبلّغ.'})


# =====================================================================
# 🚫 حظر / فك حظر عميل سوق من اللوحة
# =====================================================================
@login_required
@platform_owner_required
def super_admin_customer_block(request, customer_id):
    """🐛 [FIX]: اللوحة بتعرض «محظور» وعدد المحظورين، بس الحظر نفسه كان من Django admin بس."""
    guard = _require_superadmin(request)
    if guard:
        return guard
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    customer = get_object_or_404(MarketplaceCustomer, pk=customer_id)
    customer.is_blocked = not customer.is_blocked
    customer.save(update_fields=['is_blocked'])
    label = customer.company_name or customer.full_name
    PlatformEvent.objects.create(
        event_type='fraud_flag' if customer.is_blocked else 'other', tenant_schema='public',
        tenant_name='marketplace', user_name=request.user.username,
        description=f"{'🚫 حظر' if customer.is_blocked else '✅ فك حظر'} عميل سوق «{label}» ({customer.phone})",
    )
    return JsonResponse({'ok': True, 'is_blocked': customer.is_blocked,
                         'message': (f'تم حظر «{label}» — مش هيقدر يدخل السوق.' if customer.is_blocked
                                     else f'تم فك حظر «{label}».')})
