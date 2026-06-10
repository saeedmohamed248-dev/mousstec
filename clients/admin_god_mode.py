"""
🛡️ Mouss Tec Super Admin — God Mode Dashboard
=====================================================================
- MarketplaceCustomer admin مع 4 actions/features:
    1) grant_gift_package — منح باقات هدية مع intermediate page
    2) hard_delete_customer — حذف كامل آمن مع cascade للملفات والـ logs
    3) Impersonate (login as) — زرار من list_display
    4) System Health dashboard — view مستقل

كل العمليات الخطرة محمية بـ transaction.atomic() عشان لو فشلت خطوة، الـ DB يرجع
لحالته السابقة بدون orphans.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.decorators import user_passes_test
from django.db import connection, transaction
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    MarketplaceCustomer, CustomerDesign, DesignPackage, DesignPurchase,
    DesignPrintRequest, AIPromptLearningLog, AIStudioSession, DesignChatMessage,
)

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────────────
# Reuse the central-only mixin defined in clients/admin.py
# ─────────────────────────────────────────────────────────────────────────────
class _PublicOnlyMixin:
    def has_module_permission(self, request):
        return connection.schema_name == 'public'

    def has_view_permission(self, request, obj=None):
        return connection.schema_name == 'public' and super().has_view_permission(request, obj)

    def has_add_permission(self, request, obj=None):
        return connection.schema_name == 'public' and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return connection.schema_name == 'public' and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return connection.schema_name == 'public' and super().has_delete_permission(request, obj)


# ─────────────────────────────────────────────────────────────────────────────
# MarketplaceCustomer Admin — God Mode
# ─────────────────────────────────────────────────────────────────────────────
@admin.register(MarketplaceCustomer)
class MarketplaceCustomerAdmin(_PublicOnlyMixin, admin.ModelAdmin):
    list_display = (
        'id', 'full_name', 'phone', 'sector', 'customer_type',
        'designs_balance_badge', 'is_blocked', 'created_at', 'admin_action_buttons',
    )
    list_filter = ('sector', 'customer_type', 'is_blocked', 'is_verified', 'created_at')
    search_fields = ('full_name', 'company_name', 'phone', 'email')
    readonly_fields = ('uid', 'session_token', 'created_at', 'last_active', 'last_login_at')
    ordering = ('-created_at',)
    list_per_page = 50
    actions = ['grant_gift_package', 'hard_delete_customer']

    # ── List display custom columns ─────────────────────────────────────────
    def designs_balance_badge(self, obj):
        free_left = max(obj.free_designs_total - obj.free_designs_used, 0)
        paid_left = sum(
            max(p.designs_total - p.designs_used, 0)
            for p in obj.design_purchases.filter(status='paid')
        )
        total = free_left + paid_left
        color = '#10b981' if total > 5 else ('#f59e0b' if total > 0 else '#ef4444')
        return format_html(
            '<span style="background:{};color:#fff;padding:3px 9px;border-radius:6px;'
            'font-weight:700;font-size:11px;">🎨 {}</span>',
            color, total,
        )
    designs_balance_badge.short_description = "رصيد التصاميم"

    def admin_action_buttons(self, obj):
        impersonate_url = reverse('admin_impersonate_customer', args=[obj.id])
        return format_html(
            '<a href="{}" style="background:#7c3aed;color:#fff;padding:4px 10px;'
            'border-radius:6px;font-weight:700;font-size:11px;text-decoration:none;" '
            'onclick="return confirm(\'الدخول كهذا العميل؟ سيظهر بانر للخروج.\');">'
            '🎭 Login As</a>',
            impersonate_url,
        )
    admin_action_buttons.short_description = "أدوات"

    # ── Custom admin URLs ───────────────────────────────────────────────────
    def get_urls(self):
        urls = super().get_urls()
        my_urls = [
            path(
                'grant-gift/',
                self.admin_site.admin_view(self.grant_gift_view),
                name='clients_marketplacecustomer_grant_gift',
            ),
            path(
                'hard-delete/',
                self.admin_site.admin_view(self.hard_delete_view),
                name='clients_marketplacecustomer_hard_delete',
            ),
        ]
        return my_urls + urls

    # ─────────────────────────────────────────────────────────────────────
    # 🎁 Action 1: Grant Gift Package (intermediate confirmation page)
    # ─────────────────────────────────────────────────────────────────────
    def grant_gift_package(self, request, queryset):
        ids = list(queryset.values_list('id', flat=True))
        request.session['_god_gift_ids'] = ids
        return HttpResponseRedirect(reverse('admin:clients_marketplacecustomer_grant_gift'))
    grant_gift_package.short_description = "🎁 منح باقة هدية للعملاء المحددين"

    def grant_gift_view(self, request):
        from erp_core.ai.credit_packages import CUSTOMER_TOPUPS

        ids = request.session.get('_god_gift_ids') or []
        customers = list(MarketplaceCustomer.objects.filter(id__in=ids))

        if not customers:
            messages.error(request, "اختر عميل واحد على الأقل من القائمة قبل تنفيذ المنحة.")
            return redirect('admin:clients_marketplacecustomer_changelist')

        if request.method == 'POST':
            slug = (request.POST.get('package_slug') or '').strip()
            reason = (request.POST.get('reason') or 'هدية من الإدارة').strip()[:200]

            pkg_cfg = next((p for p in CUSTOMER_TOPUPS if p['slug'] == slug), None)
            if not pkg_cfg:
                messages.error(request, "الباقة المختارة غير صالحة.")
                return redirect('admin:clients_marketplacecustomer_grant_gift')

            granted_count = 0
            errors = []
            try:
                with transaction.atomic():
                    pkg, _ = DesignPackage.objects.get_or_create(
                        slug=pkg_cfg['slug'],
                        defaults={
                            'target_audience': 'customer',
                            'name_ar': f"🎁 {pkg_cfg['name']} (هدية)",
                            'designs_count': pkg_cfg['designs'],
                            'price_egp': pkg_cfg['price'],
                            'is_active': True,
                        },
                    )
                    for c in customers:
                        try:
                            DesignPurchase.objects.create(
                                customer=c,
                                package=pkg,
                                designs_total=pkg_cfg['designs'],
                                designs_used=0,
                                price_paid=Decimal('0.00'),
                                payment_method='admin_grant',
                                payment_reference=f'GIFT:{request.user.username}:{reason}'[:200],
                                status='paid',
                                paid_at=timezone.now(),
                            )
                            granted_count += 1
                        except Exception as e:
                            errors.append(f"{c.full_name}: {e}")
                            raise  # يدخل في rollback عام
            except Exception as e:
                messages.error(request, f"⚠️ المنحة فشلت — تم التراجع. السبب: {e}")
                logger.exception('[GOD MODE] grant_gift failed')
                return redirect('admin:clients_marketplacecustomer_changelist')

            request.session.pop('_god_gift_ids', None)
            messages.success(
                request,
                f"✅ تم منح {granted_count} عميل باقة «{pkg_cfg['name']}» "
                f"({pkg_cfg['designs']} تصميم لكل عميل) — السبب: {reason}"
            )
            logger.info(
                f"[GOD MODE] grant_gift by {request.user.username}: "
                f"slug={slug} count={granted_count} reason={reason}"
            )
            return redirect('admin:clients_marketplacecustomer_changelist')

        return render(request, 'admin/clients/marketplacecustomer/grant_gift.html', {
            'customers': customers,
            'packages': CUSTOMER_TOPUPS,
            'title': '🎁 منح باقة هدية',
            'opts': self.model._meta,
            'has_permission': True,
            'site_header': admin.site.site_header,
        })

    # ─────────────────────────────────────────────────────────────────────
    # 💣 Action 2: Hard Delete (Deep Clean)
    # ─────────────────────────────────────────────────────────────────────
    def hard_delete_customer(self, request, queryset):
        ids = list(queryset.values_list('id', flat=True))
        request.session['_god_delete_ids'] = ids
        return HttpResponseRedirect(reverse('admin:clients_marketplacecustomer_hard_delete'))
    hard_delete_customer.short_description = "💣 حذف نهائي عميق (Deep Clean)"

    def hard_delete_view(self, request):
        ids = request.session.get('_god_delete_ids') or []
        customers = list(MarketplaceCustomer.objects.filter(id__in=ids))

        if not customers:
            messages.error(request, "اختر عميل واحد على الأقل قبل تنفيذ الحذف العميق.")
            return redirect('admin:clients_marketplacecustomer_changelist')

        # احسب الـ impact للعرض
        impact = []
        for c in customers:
            impact.append({
                'customer': c,
                'designs': c.designs.count(),
                'purchases': c.design_purchases.count(),
                'print_requests': c.print_requests.count(),
                'learning_logs': AIPromptLearningLog.objects.filter(customer=c).count(),
            })

        if request.method == 'POST':
            confirm = (request.POST.get('confirm_phrase') or '').strip()
            if confirm != 'DELETE':
                messages.error(request, "اكتب كلمة DELETE بالضبط للتأكيد.")
                return redirect('admin:clients_marketplacecustomer_hard_delete')

            deleted_count = 0
            files_removed = 0
            try:
                with transaction.atomic():
                    for c in customers:
                        # 1) امسح ملفات الـ logo_image من الـ storage يدوياً
                        for d in c.designs.exclude(logo_image='').exclude(logo_image__isnull=True):
                            try:
                                d.logo_image.delete(save=False)
                                files_removed += 1
                            except Exception as fe:
                                logger.warning(f'[GOD MODE] file delete failed (non-fatal): {fe}')

                        # 2) امسح learning logs المرتبطة بالعميل (FK is SET_NULL،
                        #    فلازم نمسحها يدوياً لمنع الـ orphans حسب طلب الإدمن)
                        AIPromptLearningLog.objects.filter(customer=c).delete()

                        # 3) امسح AIStudioSession اللي ليها نفس الـ user أو مرتبطة
                        #    (الـ AIStudioSession ربطها tenant مش customer، فمنشيلهاش)

                        # 4) الحذف الرئيسي — CASCADE handles:
                        #    designs, design_purchases, print_requests, chat messages, requests
                        c.delete()
                        deleted_count += 1
            except Exception as e:
                messages.error(request, f"⚠️ الحذف فشل — تم التراجع كاملاً. السبب: {e}")
                logger.exception('[GOD MODE] hard_delete failed')
                return redirect('admin:clients_marketplacecustomer_changelist')

            request.session.pop('_god_delete_ids', None)
            messages.success(
                request,
                f"💣 تم حذف {deleted_count} عميل نهائياً — مع كل سجلاتهم "
                f"({files_removed} ملف من التخزين)."
            )
            logger.warning(
                f"[GOD MODE] hard_delete by {request.user.username}: "
                f"ids={ids} files_removed={files_removed}"
            )
            return redirect('admin:clients_marketplacecustomer_changelist')

        return render(request, 'admin/clients/marketplacecustomer/confirm_hard_delete.html', {
            'impact': impact,
            'title': '💣 تأكيد الحذف النهائي',
            'opts': self.model._meta,
            'has_permission': True,
            'site_header': admin.site.site_header,
        })


# ─────────────────────────────────────────────────────────────────────────────
# 🎭 Impersonation views (Login As Customer)
# ─────────────────────────────────────────────────────────────────────────────
def _is_super(user):
    return user.is_authenticated and user.is_superuser


@user_passes_test(_is_super)
def admin_impersonate_customer(request, customer_id: int):
    """يبدأ جلسة impersonation للعميل المختار. يحط الـ mp_session cookie + flag
    على Django session عشان البانر يظهر."""
    customer = MarketplaceCustomer.objects.filter(id=customer_id).first()
    if not customer:
        messages.error(request, "العميل غير موجود.")
        return redirect('admin:clients_marketplacecustomer_changelist')
    if customer.is_blocked:
        messages.error(request, "هذا العميل محظور — لا يمكن انتحال شخصيته.")
        return redirect('admin:clients_marketplacecustomer_changelist')

    # نسجل في Django session إن ده impersonation عشان البانر يظهر
    request.session['impersonating_customer_id'] = customer.id
    request.session['impersonator_admin_username'] = request.user.username
    request.session['impersonating_customer_name'] = customer.full_name or customer.phone

    response = redirect('/marketplace/design-store/my-designs/')
    response.set_cookie(
        'mp_session', str(customer.session_token),
        max_age=60 * 60 * 2,  # ساعتين أقصاها
        httponly=True, samesite='Lax',
        secure=not __import__('django.conf', fromlist=['settings']).settings.DEBUG,
    )
    logger.warning(
        f"[GOD MODE] IMPERSONATION START: admin={request.user.username} -> customer={customer.id} ({customer.phone})"
    )
    return response


@user_passes_test(_is_super)
def admin_impersonate_exit(request):
    """يخرج من جلسة الـ impersonation ويرجع للأدمن."""
    cust_id = request.session.pop('impersonating_customer_id', None)
    request.session.pop('impersonator_admin_username', None)
    request.session.pop('impersonating_customer_name', None)
    response = redirect('/admin/clients/marketplacecustomer/')
    response.delete_cookie('mp_session')
    logger.warning(f"[GOD MODE] IMPERSONATION EXIT: admin={request.user.username} (was customer={cust_id})")
    return response


# ─────────────────────────────────────────────────────────────────────────────
# 🩺 System Health Dashboard (Radar)
# ─────────────────────────────────────────────────────────────────────────────
@user_passes_test(_is_super)
def admin_system_health(request):
    """يعرض دورات غير مكتملة + buttons للتنظيف الآمن."""
    now = timezone.now()
    cutoff_24h = now - timezone.timedelta(hours=24)
    cutoff_7d = now - timezone.timedelta(days=7)

    # POST = resolve/clean action
    if request.method == 'POST':
        action = request.POST.get('action', '')
        try:
            with transaction.atomic():
                if action == 'expire_stale_prints':
                    n = DesignPrintRequest.objects.filter(
                        status='pending', created_at__lt=cutoff_24h,
                    ).update(status='cancelled')
                    messages.success(request, f"✅ تم تعليم {n} طلب طباعة قديم كـ cancelled.")

                elif action == 'purge_failed_logs':
                    # logs بدون image_url = generation فشل
                    n, _ = AIPromptLearningLog.objects.filter(
                        Q(image_url='') | Q(image_url__isnull=True),
                        created_at__lt=cutoff_7d,
                    ).delete()
                    messages.success(request, f"🗑️ تم حذف {n} سجل تعلم فاشل (أقدم من 7 أيام).")

                elif action == 'expire_abandoned_purchases':
                    n = DesignPurchase.objects.filter(
                        status='pending', created_at__lt=cutoff_24h,
                    ).update(status='expired')
                    messages.success(request, f"💳 تم تعليم {n} طلب شراء مهجور كـ expired.")

                else:
                    messages.error(request, "إجراء غير معروف.")
        except Exception as e:
            messages.error(request, f"⚠️ فشل التنفيذ — تم التراجع. السبب: {e}")
            logger.exception('[GOD MODE] system_health action failed')
        return redirect('admin_system_health')

    # GET = render radar
    stale_prints = DesignPrintRequest.objects.filter(
        status='pending', created_at__lt=cutoff_24h,
    ).select_related('customer').order_by('-created_at')[:50]

    failed_logs = AIPromptLearningLog.objects.filter(
        Q(image_url='') | Q(image_url__isnull=True),
    ).order_by('-created_at')[:50]

    abandoned_purchases = DesignPurchase.objects.filter(
        status='pending', created_at__lt=cutoff_24h,
    ).select_related('customer', 'package').order_by('-created_at')[:50]

    context = {
        'title': '🩺 System Health Radar',
        'site_header': admin.site.site_header,
        'has_permission': True,
        'stale_prints': stale_prints,
        'stale_prints_count': stale_prints.count() if hasattr(stale_prints, 'count') else len(stale_prints),
        'failed_logs': failed_logs,
        'failed_logs_count': len(failed_logs),
        'abandoned_purchases': abandoned_purchases,
        'abandoned_purchases_count': len(abandoned_purchases),
        'cutoff_24h': cutoff_24h,
        'cutoff_7d': cutoff_7d,
    }
    return render(request, 'admin/system_health.html', context)
