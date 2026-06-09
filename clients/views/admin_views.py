"""
Super-admin / platform-owner endpoints — the dashboard at /superadmin/,
per-tenant detail and tenant-grants editing, the "enter tenant" jump
into a tenant schema, and the legacy /impersonate-login entry point.

Every endpoint in this module assumes public-schema + superuser.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.db import connection, models, transaction
from django.db.models import Count, F, Sum
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django_tenants.utils import schema_context

from clients.models import (
    BidOffer,
    BlindBiddingRequest,
    Client,
    CustomerNotification,
    DesignPurchase,
    Domain,
    EscrowLedger,
    MarketplaceCustomer,
    PartListing,
    PartOrder,
    PlatformEvent,
    ServiceRequest,
    TenderOffer,
    VisitorLog,
)

from ._shared import _is_platform_owner

logger = logging.getLogger('mouss_tec_core')
User = get_user_model()
ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal')


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
        target = get_object_or_404(Client, id=tenant_id)
        if action == 'suspend':
            target.status = 'suspended'
            target.is_active = False
            target.save(update_fields=['status', 'is_active'])
            PlatformEvent.objects.create(
                event_type='suspension', tenant_schema=target.schema_name,
                tenant_name=target.name, user_name=request.user.username,
                description=f"تعليق حساب «{target.name}» بواسطة {request.user.username}",
            )
        elif action == 'activate':
            target.status = 'active'
            target.is_active = True
            target.save(update_fields=['status', 'is_active'])
        elif action == 'flag_fraud':
            target.is_fraud_flagged = True
            target.save(update_fields=['is_fraud_flagged'])
            PlatformEvent.objects.create(
                event_type='fraud_flag', tenant_schema=target.schema_name,
                tenant_name=target.name, user_name=request.user.username,
                description=f"تعليم احتيال على «{target.name}»",
            )
        elif action == 'unflag_fraud':
            target.is_fraud_flagged = False
            target.save(update_fields=['is_fraud_flagged'])
        elif action == 'extend_trial':
            base_date = target.trial_ends_at or timezone.localdate()
            target.trial_ends_at = base_date + timedelta(days=3)
            target.save(update_fields=['trial_ends_at'])
        elif action == 'activate_subscription':
            plan = request.POST.get('plan', 'silver')
            billing_period = request.POST.get('billing_period', 'monthly')
            plan_prices = {'silver': 780, 'gold': 1250, 'empire': 1800}
            period_days = {'monthly': 30, 'quarterly': 90, 'semi_annual': 180, 'annual': 365}
            period_discounts = {'monthly': Decimal('0'), 'quarterly': Decimal('0.09'),
                                'semi_annual': Decimal('0.125'), 'annual': Decimal('0.25')}
            period_labels = {'monthly': 'شهري', 'quarterly': 'ربع سنوي',
                             'semi_annual': 'نصف سنوي', 'annual': 'سنوي'}
            months_map = {'monthly': 1, 'quarterly': 3, 'semi_annual': 6, 'annual': 12}

            base_price = Decimal(str(plan_prices.get(plan, 780)))
            discount = period_discounts.get(billing_period, Decimal('0'))
            months = months_map.get(billing_period, 1)
            total = (base_price * months * (1 - discount)).quantize(Decimal('1'))
            days = period_days.get(billing_period, 30)

            target.plan = plan
            target.status = 'active'
            target.is_active = True
            target.subscription_end_date = timezone.localdate() + timedelta(days=days)
            target.save(update_fields=['plan', 'status', 'is_active', 'subscription_end_date'])

            PlatformEvent.objects.create(
                event_type='subscription', tenant_schema=target.schema_name,
                tenant_name=target.name, user_name=request.user.username,
                description=f"تفعيل اشتراك «{target.name}» — {plan} {period_labels.get(billing_period)} — {total} ج.م",
                metadata={'plan': plan, 'period': billing_period, 'total': str(total)},
            )

            messages.success(request,
                f'تم تفعيل اشتراك «{target.name}» — باقة {target.get_plan_display()} '
                f'({period_labels.get(billing_period, billing_period)}) — {total} ج.م — '
                f'ينتهي {target.subscription_end_date}')

        elif action == 'renew_subscription':
            # تجديد الاشتراك الحالي بنفس الباقة لمدة 30 يوم إضافية
            if target.subscription_end_date:
                base_date = max(target.subscription_end_date, timezone.localdate())
            else:
                base_date = timezone.localdate()
            target.subscription_end_date = base_date + timedelta(days=30)
            target.status = 'active'
            target.is_active = True
            target.save(update_fields=['subscription_end_date', 'status', 'is_active'])
            PlatformEvent.objects.create(
                event_type='subscription', tenant_schema=target.schema_name,
                tenant_name=target.name, user_name=request.user.username,
                description=f"تجديد اشتراك «{target.name}» — {target.get_plan_display()} — حتى {target.subscription_end_date}",
            )
            messages.success(request, f'تم تجديد اشتراك «{target.name}» حتى {target.subscription_end_date}')

        elif action == 'activate_ai_addon':
            # تفعيل حزمة AI Studio للشركة
            from clients.models import TenantSubscription, AIAddonPackage
            addon_slug = request.POST.get('ai_addon_slug', 'ai-basic')
            addon = AIAddonPackage.objects.filter(slug=addon_slug, is_active=True).first()
            if not addon:
                messages.error(request, 'حزمة AI غير موجودة.')
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
                messages.success(request, f'تم تفعيل حزمة AI «{addon.name}» لشركة «{target.name}»')

        elif action == 'deactivate_ai_addon':
            # إلغاء حزمة AI Studio
            from clients.models import TenantSubscription
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
                messages.success(request, f'تم إلغاء حزمة AI من «{target.name}»')
            except TenantSubscription.DoesNotExist:
                messages.error(request, 'لا يوجد اشتراك لهذه الشركة.')

        elif action == 'grant_ai_bonus':
            # 🎁 هدية رصيد AI Studio من السوبر أدمن
            from clients.models import AIBonusGrant
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
                messages.error(request, '❌ يجب تحديد رصيد واحد على الأقل (تصاميم / واتساب / علامات مائية).')
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
                messages.success(
                    request,
                    f'🎁 تم منح «{target.name}» هدية: {designs} تصميم + {whatsapp_n} واتساب + {watermarks_n} علامة مائية' +
                    (f' (تنتهي خلال {expires_days} يوم)' if expires_at else '')
                )

        elif action == 'revoke_bonus':
            from clients.models import AIBonusGrant
            grant_id = request.POST.get('grant_id')
            try:
                grant = AIBonusGrant.objects.get(pk=grant_id, tenant=target)
                grant.is_active = False
                grant.save(update_fields=['is_active'])
                messages.success(request, f'تم إلغاء الهدية #{grant.pk} من «{target.name}»')
            except AIBonusGrant.DoesNotExist:
                messages.error(request, 'الهدية غير موجودة.')

        elif action == 'delete_tenant':
            # ⚠️ حذف نهائي للشركة — يحذف الـ schema بالكامل
            confirm_name = request.POST.get('confirm_name', '').strip()
            if confirm_name != target.schema_name:
                messages.error(request, 'فشل الحذف: اسم التأكيد لا يتطابق مع اسم الـ Schema.')
            else:
                tenant_name = target.name
                schema = target.schema_name
                try:
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
                    messages.success(request, f'تم حذف شركة «{tenant_name}» نهائياً.')
                except Exception as e:
                    logger.error("[SUPER ADMIN] Failed to delete tenant %s: %s", schema, e)
                    messages.error(request, f'فشل حذف الشركة: {e}')

        return redirect('super_admin_dashboard')

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
        visitors_today = VisitorLog.objects.filter(timestamp__gte=today_start).count()
        unique_ips_today = VisitorLog.objects.filter(
            timestamp__gte=today_start
        ).values('ip_address').distinct().count()
        visitors_week = VisitorLog.objects.filter(timestamp__gte=week_ago).count()
        unique_ips_week = VisitorLog.objects.filter(
            timestamp__gte=week_ago
        ).values('ip_address').distinct().count()

        # أكثر الصفحات زيارة
        top_pages = list(
            VisitorLog.objects.filter(timestamp__gte=week_ago)
            .values('path')
            .annotate(hits=Count('id'))
            .order_by('-hits')[:10]
        )

        # أكثر الشركات نشاطاً
        top_tenants = list(
            VisitorLog.objects.filter(timestamp__gte=week_ago)
            .exclude(tenant_schema='public')
            .exclude(tenant_schema='')
            .values('tenant_schema')
            .annotate(hits=Count('id'))
            .order_by('-hits')[:10]
        )

        # Device breakdown
        device_breakdown = list(
            VisitorLog.objects.filter(timestamp__gte=week_ago)
            .values('device_type')
            .annotate(count=Count('id'))
            .order_by('-count')
        )

        # Average response time
        avg_response = VisitorLog.objects.filter(
            timestamp__gte=today_start, response_time_ms__isnull=False,
        ).aggregate(avg=Avg('response_time_ms'))['avg'] or 0

        # آخر 50 زائر (Live Feed)
        recent_visitors = list(
            VisitorLog.objects.filter(timestamp__gte=today_start)
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
        pass

    # --- Platform Events (Activity Feed) ---
    recent_events = []
    try:
        recent_events = list(
            PlatformEvent.objects.order_by('-timestamp')[:30]
            .values('timestamp', 'event_type', 'tenant_name', 'user_name', 'description')
        )
    except Exception:
        pass

    # --- Tenant Deep Details (per tenant) ---
    from clients.models import TenantSubscription, AIAddonPackage
    tenants_enriched = []
    for t in tenants:
        users_count = 0
        try:
            with schema_context(t.schema_name):
                users_count = User.objects.count()
        except Exception:
            pass

        # جلب حالة AI addon
        ai_addon_name = ''
        try:
            sub = TenantSubscription.objects.get(tenant=t)
            if sub.ai_addon:
                ai_addon_name = sub.ai_addon.name
        except TenantSubscription.DoesNotExist:
            pass

        # 🎁 جلب رصيد الهدايا النشطة
        from clients.models import AIBonusGrant
        active_grants = AIBonusGrant.objects.filter(tenant=t, is_active=True)
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
        })

    # --- طلبات سوق في انتظار الموافقة ---
    pending_marketplace_requests = ServiceRequest.objects.filter(
        status='pending_approval'
    ).select_related('customer').order_by('-created_at')[:30]

    # --- طلبات شراء التصاميم المعلّقة ---
    pending_design_purchases = DesignPurchase.objects.filter(
        status__in=['pending', 'awaiting_confirm']
    ).select_related('customer', 'package').order_by('-created_at')[:20]

    # --- طلبات طباعة التصاميم ---
    from clients.models import DesignPrintRequest
    pending_print_requests = DesignPrintRequest.objects.filter(
        status__in=['pending', 'quoted']
    ).select_related('customer', 'design').order_by('-created_at')[:30]

    # --- 🛍️ عملاء السوق + إحصائيات نشاطهم ---
    from django.db.models import Count, Sum
    marketplace_customers = (
        MarketplaceCustomer.objects.all()
        .annotate(
            requests_count=Count('requests', distinct=True),
            designs_count=Count('designs', distinct=True),
            purchases_count=Count('design_purchases', distinct=True),
            spent_total=Sum('design_purchases__price_paid'),
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

    # --- 🏢 نشاط الشركات (آخر أحداث + إحصائيات) ---
    from clients.models import VisitorLog
    tenant_activity = []
    for t in tenants:
        last_visit = VisitorLog.objects.filter(tenant_schema=t.schema_name).order_by('-timestamp').first()
        visits_today = VisitorLog.objects.filter(
            tenant_schema=t.schema_name,
            timestamp__gte=today_start,
        ).count()
        visits_7d = VisitorLog.objects.filter(
            tenant_schema=t.schema_name,
            timestamp__gte=week_ago,
        ).count()
        last_events = list(
            PlatformEvent.objects.filter(tenant_schema=t.schema_name)
            .order_by('-timestamp')[:5]
        )
        tenant_activity.append({
            'tenant': t,
            'last_visit_at': last_visit.timestamp if last_visit else None,
            'last_visit_path': last_visit.path if last_visit else '',
            'visits_today': visits_today,
            'visits_7d': visits_7d,
            'last_events': last_events,
        })
    # رتب: آخر نشاط أولاً
    from datetime import datetime, timezone as _tz
    _epoch = datetime(1970, 1, 1, tzinfo=_tz.utc)
    tenant_activity.sort(key=lambda x: x['last_visit_at'] or _epoch, reverse=True)

    # --- 🚗 P2P Parts Marketplace — refund requests + recent orders ---
    from clients.models import PartOrder
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

    # --- حزم AI المتاحة ---
    ai_addons = list(AIAddonPackage.objects.filter(is_active=True).order_by('sort_order').values('slug', 'name', 'monthly_price'))

    # --- الباقات للمودال ---
    plan_prices_json = json.dumps({'silver': 780, 'gold': 1250, 'empire': 1800})
    period_discounts_json = json.dumps({'monthly': 0, 'quarterly': 0.09, 'semi_annual': 0.125, 'annual': 0.25})
    period_months_json = json.dumps({'monthly': 1, 'quarterly': 3, 'semi_annual': 6, 'annual': 12})

    return render(request, 'clients/super_admin.html', {
        'tenants': tenants_enriched,
        'summary': summary,
        'today': today,
        'new_this_month': new_this_month,
        'new_this_week': new_this_week,
        'expiring_soon': expiring_soon,
        'trial_expired': trial_expired,
        'visitor_stats': visitor_stats,
        'recent_events': recent_events,
        'plan_prices_json': plan_prices_json,
        'period_discounts_json': period_discounts_json,
        'period_months_json': period_months_json,
        'ai_addons': ai_addons,
        'pending_design_purchases': pending_design_purchases,
        'pending_print_requests': pending_print_requests,
        'pending_marketplace_requests': pending_marketplace_requests,
        'marketplace_customers': marketplace_customers,
        'customers_summary': customers_summary,
        'tenant_activity': tenant_activity,
        'pending_refund_orders': pending_refund_orders,
        'parts_orders_summary': parts_orders_summary,
    })


# =====================================================================
# 👤 تفاصيل عميل السوق (Marketplace Customer Detail - AJAX HTML)
# =====================================================================

@login_required
@user_passes_test(lambda u: u.is_superuser)
def super_admin_customer_detail(request, customer_id):
    """يُرجع HTML بتفاصيل العميل + كل نشاطه (designs, purchases, requests, chats)."""
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access Denied')

    customer = get_object_or_404(MarketplaceCustomer, pk=customer_id)
    designs = list(customer.designs.select_related('purchase__package').order_by('-created_at')[:50])
    purchases = list(customer.design_purchases.select_related('package').order_by('-created_at')[:20])
    requests_list = list(customer.requests.order_by('-created_at')[:20])

    # إجماليات
    total_spent = sum((p.price_paid for p in purchases if p.status == 'paid'), Decimal('0'))
    total_designs = len(designs)
    total_purchases = len(purchases)

    return render(request, 'clients/super_admin_customer_detail.html', {
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
@user_passes_test(lambda u: u.is_superuser)
def super_admin_tenant_grants(request, tenant_id):
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access Denied')
    from clients.models import AIBonusGrant
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
@user_passes_test(lambda u: u.is_superuser)
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
@user_passes_test(lambda u: u.is_superuser)
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
@user_passes_test(lambda u: u.is_superuser)
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
@user_passes_test(lambda u: u.is_superuser)
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
@user_passes_test(lambda u: u.is_superuser)
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

    with transaction.atomic():
        order.status = 'refunded'
        order.refunded_at = timezone.now()
        order.admin_notes = (order.admin_notes + '\n' if order.admin_notes else '') + f'[REFUND APPROVED] {note}'
        order.save(update_fields=['status', 'refunded_at', 'admin_notes'])

        # Notify buyer
        if order.buyer_customer_id:
            CustomerNotification.objects.create(
                customer=order.buyer_customer,
                title='✅ تم قبول طلب الإرجاع',
                body=f'هنرجع لك مبلغ {order.amount_paid} ج.م خلال 3-5 أيام عمل. الشحن على المنصة.',
                level='success', icon='fa-rotate-left',
            )
        # Notify seller
        if order.listing.seller_customer_id:
            CustomerNotification.objects.create(
                customer=order.listing.seller_customer,
                title='⚠️ تم قبول طلب الإرجاع',
                body=f'الإدارة وافقت على إرجاع «{order.listing.title}». التواصل مع المشتري لاسترداد القطعة. الشحن علينا.',
                level='warning', icon='fa-rotate-left',
            )

        PlatformEvent.objects.create(
            event_type='other', tenant_schema='public', tenant_name='parts_market',
            user_name=request.user.username,
            description=f"✅ موافقة إرجاع طلب {order.order_code} — {order.amount_paid} ج.م إلى المشتري",
        )

    return JsonResponse({'ok': True, 'message': 'تم قبول الإرجاع وإشعار الطرفين.'})


@login_required
@user_passes_test(lambda u: u.is_superuser)
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

    with transaction.atomic():
        order.status = 'released'
        order.released_at = timezone.now()
        order.admin_notes = (order.admin_notes + '\n' if order.admin_notes else '') + f'[REFUND REJECTED] {note}'
        order.save(update_fields=['status', 'released_at', 'admin_notes'])

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
                body=f'الإدارة رفضت طلب الإرجاع. المبلغ {order.seller_payout} ج.م في طريقه لحسابك.',
                level='success', icon='fa-money-bill-wave',
            )

        PlatformEvent.objects.create(
            event_type='other', tenant_schema='public', tenant_name='parts_market',
            user_name=request.user.username,
            description=f"❌ رفض إرجاع طلب {order.order_code} — تحرير {order.seller_payout} ج.م للبائع",
        )

    return JsonResponse({'ok': True, 'message': 'تم رفض الإرجاع وتحرير أموال البائع.'})

