from django.contrib import admin
from django.utils.html import format_html
from django.contrib import messages
from django.db import transaction
from django.db import connection  # مستشعر فحص النطاقات السحابية اللحظي
from django.core.exceptions import ValidationError
from django.utils import timezone
from datetime import timedelta
from dateutil.relativedelta import relativedelta

# استدعاء جميع نماذج الإمبراطورية بعد التحديثات الأخيرة
from .models import (
    Client, Domain, GlobalB2BMarketplace, BlindBiddingRequest,
    BidOffer, EscrowLedger, Plan, AIAddonPackage,
    TenantSubscription, AILimitTracker,
)

import logging
logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🛡️ 0. درع الحماية السيبراني لحظر تسريب البيانات للفروع (SaaS Isolation Shield)
# =====================================================================
class PublicSchemaOnlyAdminMixin:
    """
    🚀 ابتكار أمني: يمنع ظهور اللوحة أو جداولها نهائياً خارج النطاق المركزي للمنصة.
    إذا دخل أي عميل من الـ Subdomain بتاعه، تختفي هذه الجداول تماماً من شاشته.
    """
    def has_module_permission(self, request):
        return connection.schema_name == 'public'
    def has_view_permission(self, request, obj=None):
        return connection.schema_name == 'public'
    def has_add_permission(self, request):
        return connection.schema_name == 'public'
    def has_change_permission(self, request, obj=None):
        return connection.schema_name == 'public'
    def has_delete_permission(self, request, obj=None):
        return connection.schema_name == 'public'


class AutomotiveOnlyAdminMixin:
    """
    🏭 يمنع ظهور نماذج السيارات (B2B/مزادات) في المطابع.
    تظهر فقط في: public + automotive tenants.
    """
    def _is_allowed(self):
        if connection.schema_name == 'public':
            return True
        tenant = getattr(connection, 'tenant', None)
        industry = getattr(tenant, 'industry', 'automotive') if tenant else 'automotive'
        return industry != 'printing'

    def has_module_permission(self, request):
        return self._is_allowed() and super().has_module_permission(request)
    def has_view_permission(self, request, obj=None):
        return self._is_allowed() and super().has_view_permission(request, obj)
    def has_add_permission(self, request):
        return self._is_allowed() and super().has_add_permission(request)
    def has_change_permission(self, request, obj=None):
        return self._is_allowed() and super().has_change_permission(request, obj)
    def has_delete_permission(self, request, obj=None):
        return self._is_allowed() and super().has_delete_permission(request, obj)

# =====================================================================
# 🌐 1. النطاقات وعروض الأسعار (Inlines)
# =====================================================================
class DomainInline(admin.TabularInline):
    model = Domain
    max_num = 1
    extra = 1
    verbose_name = "نطاق / رابط الشركة"
    verbose_name_plural = "🌐 الروابط والنطاقات (Domains)"

class BidOfferInline(admin.TabularInline):
    """🚀 كشف عروض الأسعار المخفية داخل لوحة التحكم لفض النزاعات من الإدارة المركزية"""
    model = BidOffer
    extra = 0
    readonly_fields = ('seller', 'offer_price', 'condition', 'estimated_delivery_days', 'created_at')
    can_delete = False
    verbose_name = "عرض سعر مقدم"
    verbose_name_plural = "📥 عروض أسعار التجار السريّة"

class TenantSubscriptionInline(admin.StackedInline):
    """Inline لعرض اشتراك المستأجر داخل صفحة Client"""
    model = TenantSubscription
    extra = 0
    max_num = 1
    can_delete = False
    verbose_name = "اشتراك المستأجر"
    verbose_name_plural = "📋 اشتراك المستأجر (Subscription)"
    readonly_fields = ('created_at', 'updated_at', 'subscription_summary')
    fieldsets = (
        (None, {
            'fields': ('plan', 'ai_addon', 'billing_cycle_months', 'current_period_start', 'current_period_end', 'is_active', 'subscription_summary'),
        }),
    )

    def subscription_summary(self, obj):
        if not obj.pk:
            return "—"
        plan_name = obj.plan.name if obj.plan else 'بدون باقة'
        addon_name = obj.ai_addon.name if obj.ai_addon else 'بدون'
        status_color = '#28a745' if obj.is_active else '#dc3545'
        status_text = 'فعّال' if obj.is_active else 'غير فعّال'

        days_left = ''
        if obj.current_period_end:
            remaining = (obj.current_period_end - timezone.now().date()).days
            if remaining > 0:
                days_left = f' | متبقي {remaining} يوم'
            elif remaining == 0:
                days_left = ' | ينتهي اليوم!'
            else:
                days_left = f' | <span style="color:#dc3545;">منتهي منذ {abs(remaining)} يوم</span>'

        return format_html(
            '<div style="background:#f8f9fa; padding:10px; border-radius:6px; border-left:4px solid {};">'
            '<b>الباقة:</b> {} | <b>حزمة AI:</b> {} | '
            '<b>الحالة:</b> <span style="color:{};">{}</span>{}'
            '</div>',
            status_color, plan_name, addon_name, status_color, status_text, days_left
        )
    subscription_summary.short_description = "ملخص الاشتراك"

# =====================================================================
# 🏢 2. لوحة القيادة المركزية لشركات Mouss Tec (محمية بالكامل 🔒)
# =====================================================================
@admin.register(Client)
class ClientAdmin(PublicSchemaOnlyAdminMixin, admin.ModelAdmin):
    inlines = [DomainInline, TenantSubscriptionInline]
    list_display = (
        'name', 'business_type_badge', 'plan_badge', 
        'financial_assets_styled', 'is_verified_icon', 
        'market_rating_styled', 'status', 'enter_tenant_dashboard'
    )
    list_filter = ('business_type', 'plan', 'status', 'is_verified_merchant', 'is_marketplace_active')
    search_fields = ('name', 'schema_name', 'email', 'phone', 'commercial_register')
    
    fieldsets = (
        ('البيانات الأساسية والنشاط', {
            'fields': ('name', 'owner_name', 'phone', 'email', 'business_type')
        }),
        ('إعدادات سوق Mouss Tec (Marketplace & SLA)', {
            'fields': ('is_marketplace_active', 'is_verified_merchant', 'commercial_register', 'market_rating', 'successful_deals', 'dispute_rate'),
            'description': "إدارة الثقة، التوثيق، وتقييمات الذكاء الاصطناعي للتاجر."
        }),
        ('الخزينة المركزية (Mouss Tec FinTech)', {
            'fields': ('wallet_balance', 'escrow_held', 'platform_fee_rate'),
            'description': "أموال التاجر في النظام وعمولة المنصة المحصلة منه."
        }),
        ('الباقة وقيود السحابة (SaaS Limits)', {
            'fields': ('plan', 'status', 'trial_ends_at', 'subscription_end_date', 'max_branches', 'max_users', 'max_treasuries', 'extra_treasuries_purchased')
        }),
        ('مؤشرات الذكاء الاصطناعي (AI Trust)', {
            'fields': ('ai_trust_score',),
            'classes': ('collapse',),
            'description': "مؤشر الثقة الديناميكي المحسوب آلياً بواسطة نظام الـ AI."
        }),
        ('التخصيص الفني (White-labeling)', {
            # ✅ تم إزالة الحقل الوهمي auto_create_schema للقضاء على خطأ FieldError نهائياً
            'fields': ('logo', 'theme_color')
        }),
    )

    actions = ['verify_merchants', 'suspend_clients', 'create_subscriptions_for_selected', 'quick_activate_1_month']

    def business_type_badge(self, obj):
        colors = {'service_center': '#17a2b8', 'parts_dealer': '#ffc107', 'scrap_importer': '#dc3545', 'both': '#6f42c1'}
        return format_html('<span style="background:{}; color:white; padding:4px 8px; border-radius:4px; font-size:11px;">{}</span>', colors.get(obj.business_type, 'gray'), obj.get_business_type_display())
    business_type_badge.short_description = "النشاط"

    def plan_badge(self, obj): return format_html('<b>{}</b>', obj.get_plan_display())
    plan_badge.short_description = "الباقة"

    def financial_assets_styled(self, obj):
        wallet_text = f"{obj.wallet_balance:,.2f}"
        escrow_text = f"{obj.escrow_held:,.2f}"
        return format_html('<div style="line-height:1.2;"><span style="color:#28a745; font-weight:bold;">🟢 {} ج.م</span><br><span style="color:#ffc107; font-size:10px;">🔒 {} ج.م</span></div>', wallet_text, escrow_text)
    financial_assets_styled.short_description = "الأصول المالية (Escrow)"

    def is_verified_icon(self, obj):
        if obj.is_verified_merchant: return format_html('<i class="fas fa-check-circle" style="color:#007bff; font-size:18px;"></i>')
        return format_html('<i class="fas fa-times-circle" style="color:#dc3545; font-size:18px;"></i>')
    is_verified_icon.short_description = "موثق KYC"

    def market_rating_styled(self, obj): return format_html('<span style="color:#f59e0b; font-weight:bold;"><i class="fas fa-star"></i> {}</span>', obj.market_rating)
    market_rating_styled.short_description = "التقييم"

    def enter_tenant_dashboard(self, obj):
        try:
            from django.conf import settings as _s
            raw_domain = obj.domains.first().domain
            # Subdomains must use hyphens — fix any legacy underscore records
            safe_domain = raw_domain.replace('_', '-')
            protocol = 'http' if _s.DEBUG else 'https'
            admin_slug = _s.ADMIN_URL if hasattr(_s, 'ADMIN_URL') else 'secure-portal'
            url = f"{protocol}://{safe_domain}/{admin_slug}/"
            return format_html(
                '<a class="button" href="{}" target="_blank" '
                'style="background-color:#28a745;color:white;padding:5px 12px;'
                'border-radius:4px;text-decoration:none;font-weight:bold;font-size:12px;">🚀 الإدارة الميدانية</a>',
                url
            )
        except Exception:
            return format_html('<span style="color:#dc3545;font-weight:bold;font-size:11px;">⚠️ بدون نطاق</span>')
    enter_tenant_dashboard.short_description = "الدخول السريع"

    @admin.action(description='✅ منح علامة التوثيق (العلامة الزرقاء) للتجار المحددين')
    def verify_merchants(self, request, queryset):
        updated = queryset.update(is_verified_merchant=True)
        self.message_user(request, f"تم توثيق عدد {updated} تاجر/مركز بنجاح بنظام KYC.", messages.SUCCESS)

    @admin.action(description='🛑 تعليق حسابات الشركات المحددة (إيقاف النظام)')
    def suspend_clients(self, request, queryset):
        updated = queryset.update(status='suspended', is_active=False)
        self.message_user(request, f"تم إيقاف عدد {updated} شركة وعزل فروعها عن السحابة.", messages.WARNING)

    @admin.action(description='📋 إنشاء سجل اشتراك للشركات المحددة (إذا لم يوجد)')
    def create_subscriptions_for_selected(self, request, queryset):
        created = 0
        skipped = 0
        for client in queryset:
            _, was_created = TenantSubscription.objects.get_or_create(
                tenant=client,
                defaults={'is_active': False},
            )
            if was_created:
                created += 1
            else:
                skipped += 1
        msg = f"📋 تم إنشاء {created} سجل اشتراك جديد."
        if skipped:
            msg += f" ({skipped} شركة لديها اشتراك بالفعل)"
        self.message_user(request, msg, messages.SUCCESS)

    @admin.action(description='✅ تفعيل اشتراك فوري — شهر واحد (إنشاء + تفعيل)')
    def quick_activate_1_month(self, request, queryset):
        today = timezone.now().date()
        end_date = today + relativedelta(months=1)
        success = 0
        for client in queryset:
            try:
                with transaction.atomic():
                    sub, _ = TenantSubscription.objects.get_or_create(
                        tenant=client,
                        defaults={'is_active': False},
                    )
                    sub.current_period_start = today
                    sub.current_period_end = end_date
                    sub.billing_cycle_months = 1
                    sub.is_active = True
                    sub.save()
                    client.status = 'active'
                    client.subscription_end_date = end_date
                    client.is_active = True
                    if sub.plan:
                        client.max_branches = sub.plan.max_branches
                        client.max_users = sub.plan.max_users
                        client.max_treasuries = sub.plan.max_treasuries
                    client.save(update_fields=[
                        'status', 'subscription_end_date', 'is_active',
                        'max_branches', 'max_users', 'max_treasuries',
                    ])
                    success += 1
            except Exception as e:
                logger.error(f"🔴 [QUICK ACTIVATE ERROR]: {client.name} — {e}")
        self.message_user(
            request,
            f"✅ تم تفعيل {success} اشتراك فوري لمدة شهر (حتى {end_date}).",
            messages.SUCCESS,
        )

    exclude = ('auto_create_schema', 'auto_drop_schema')

# =====================================================================
# 🛒 3. إدارة السوق المركزي (Global Marketplace - مصفى ومؤمن 🔐)
# =====================================================================
@admin.register(GlobalB2BMarketplace)
class GlobalB2BMarketplaceAdmin(AutomotiveOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('part_number', 'product_name', 'tenant_name_safe', 'condition', 'wholesale_price_styled', 'available_qty', 'ai_confidence_score_badge')
    list_filter = ('condition', 'brand')
    search_fields = ('part_number', 'product_name')
    readonly_fields = ('updated_at',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if connection.schema_name != 'public':
            return qs.filter(tenant__schema_name=connection.schema_name)
        return qs

    def has_add_permission(self, request):
        # 🛡️ التاجر لا يضيف يدوياً — النشر فقط عبر نظام الموافقة (B2BListingRequest)
        # السوبر أدمن فقط يقدر يضيف مباشرة للصيانة
        if connection.schema_name == 'public' and request.user.is_superuser:
            return True
        return False

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'tenant':
            # 🛡️ إخفاء tenant المنصة المركزية + عدم كشف تفاصيل الباقات
            kwargs['queryset'] = Client.objects.exclude(
                schema_name='public',
            ).exclude(
                schema_name__in=['mousstec', 'mouss_tec'],
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def tenant_name_safe(self, obj):
        """عرض اسم التاجر بدون كشف نوع الباقة"""
        return obj.tenant.name if obj.tenant else '-'
    tenant_name_safe.short_description = "التاجر"

    def wholesale_price_styled(self, obj): return format_html('<b style="color:#007bff;">{} ج.م</b>', f"{obj.wholesale_price:,.2f}")
    wholesale_price_styled.short_description = "سعر الجملة"

    def ai_confidence_score_badge(self, obj):
        color = "#28a745" if obj.ai_quality_confidence >= 80 else "#ffc107" if obj.ai_quality_confidence >= 50 else "#dc3545"
        return format_html('<div style="width: 100px; background-color: #e9ecef; border-radius: 4px; overflow: hidden;"><div style="width: {}%; background-color: {}; height: 8px;"></div></div> <span style="font-size:10px; color:{};">{}%</span>', obj.ai_quality_confidence, color, color, obj.ai_quality_confidence)
    ai_confidence_score_badge.short_description = "مؤشر جودة AI"


# =====================================================================
# ⚖️ 4. غرفة عمليات المزاد العكسي وفض النزاعات (Atomic & Fully Integrated 🔐)
# =====================================================================
@admin.register(BlindBiddingRequest)
class BlindBiddingRequestAdmin(AutomotiveOnlyAdminMixin, admin.ModelAdmin):
    inlines = [BidOfferInline] 
    list_display = ('request_id_short', 'part_number', 'buyer', 'status_badge', 'auto_award_icon', 'winner', 'winning_price_styled', 'expires_at')
    list_filter = ('status', 'auto_award')
    search_fields = ('part_number', 'buyer__name', 'winner__name', 'request_id')
    readonly_fields = ('request_id', 'created_at')
    actions = ['resolve_dispute_refund', 'resolve_dispute_release']
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if connection.schema_name != 'public':
            return qs.filter(buyer__schema_name=connection.schema_name)
        return qs

    def request_id_short(self, obj): return format_html('<span style="font-family:monospace; color:#6c757d;">{}</span>', str(obj.request_id)[:8])
    request_id_short.short_description = "كود الطلب"

    def status_badge(self, obj):
        colors = {'open': '#0dcaf0', 'awarding': '#6610f2', 'escrow_held': '#ffc107', 'shipped': '#fd7e14', 'completed': '#28a745', 'disputed': '#dc3545', 'cancelled': '#6c757d'}
        return format_html('<span style="background:{}; color:white; padding:4px 8px; border-radius:4px; font-size:11px; font-weight:bold;">{}</span>', colors.get(obj.status, 'gray'), obj.get_status_display())
    status_badge.short_description = "حالة العملية"

    def auto_award_icon(self, obj):
        if obj.auto_award: return format_html('<i class="fas fa-robot" style="color:#6f42c1;"></i>')
        return format_html('<i class="fas fa-user-tie" style="color:#6c757d;"></i>')
    auto_award_icon.short_description = "الذكاء الاصطناعي"

    def winning_price_styled(self, obj): return format_html('<b style="color:#28a745;">{} ج.م</b>', f"{obj.winning_price:,.2f}") if obj.winning_price else "-"
    winning_price_styled.short_description = "سعر الترسية"

    @admin.action(description='⚖️ فض النزاع مركزيًا: إرجاع الأموال للمشتري بالكامل (Refund)')
    def resolve_dispute_refund(self, request, queryset):
        success_count = 0
        error_count = 0
        
        disputed_bids = queryset.filter(status='disputed')
        
        for bid in disputed_bids:
            try:
                with transaction.atomic():
                    bid.trigger_refund_to_buyer() 
                    success_count += 1
            except ValidationError as ve:
                error_count += 1
                logger.error(f"🔴 [ADMIN DISPUTE REFUND ERROR]: Failed for Bid #{bid.id} - {ve.message}")
        
        if success_count > 0:
            self.message_user(request, f"⚖️ نجاح: تم فض النزاع ماليًا وإعادة الأموال لمحفظة المشتري في عدد {success_count} طلب شراء.", messages.SUCCESS)
        if error_count > 0:
            self.message_user(request, f"⚠️ تنبيه: فشلت تسوية عدد {error_count} عملية لعدم مطابقة الشروط المحاسبية.", messages.ERROR)

    @admin.action(description='⚖️ فض النزاع مركزيًا: تحرير وإرسال الأموال للتاجر (Release Funds)')
    def resolve_dispute_release(self, request, queryset):
        success_count = 0
        error_count = 0
        
        disputed_bids = queryset.filter(status='disputed')
        
        for bid in disputed_bids:
            try:
                with transaction.atomic():
                    bid.status = 'shipped'
                    bid.save(update_fields=['status'])  # 🚀 [FIX BY QA]: حفظ الحالة قبل تحرير الأموال
                    bid.trigger_release_to_seller()
                    success_count += 1
            except ValidationError as ve:
                error_count += 1
                logger.error(f"🔴 [ADMIN DISPUTE RELEASE ERROR]: Failed for Bid #{bid.id} - {ve.message}")
                
        if success_count > 0:
            self.message_user(request, f"⚖️ نجاح: تم إنهاء النزاع لصالح التاجر الفائز وتحرير أموال الضمان لعدد {success_count} طلب مبيعات.", messages.SUCCESS)
        if error_count > 0:
            self.message_user(request, f"⚠️ تنبيه: فشل تحرير الرصيد لعدد {error_count} عملية، يرجى مراجعة سجل الـ Logs المالي.", messages.ERROR)


# =====================================================================
# 👑 5. جهاز الرقابة المالية والمخابراتية الموحد (FinTech Escrow Ledger - محمية 🔒)
# =====================================================================
@admin.register(EscrowLedger)
class EscrowLedgerAdmin(PublicSchemaOnlyAdminMixin, admin.ModelAdmin): 
    list_display = ('transaction_id_short', 'client', 'transaction_type_badge', 'amount_styled', 'fraud_risk_badge', 'created_at')
    list_filter = ('transaction_type', 'created_at')
    search_fields = ('client__name', 'description', 'transaction_id')
    readonly_fields = [f.name for f in EscrowLedger._meta.fields] 

    def has_add_permission(self, request): return False
    def has_delete_permission(self, request, obj=None): return False

    def transaction_id_short(self, obj): return format_html('<span style="font-family:monospace; color:#6c757d;">{}</span>', str(obj.transaction_id)[:8])
    transaction_id_short.short_description = "رقم العملية"

    def transaction_type_badge(self, obj): return format_html('<span style="font-weight:bold; color:#495057;">{}</span>', obj.get_transaction_type_display())
    transaction_type_badge.short_description = "النوع"

    def amount_styled(self, obj):
        sign = "-" if obj.transaction_type in ['hold', 'fee_deduction', 'withdrawal'] else "+"
        color = "#dc3545" if sign == "-" else "#28a745"
        formatted_amount = f"{obj.amount:,.2f}"
        return format_html('<b style="color:{};">{} {} ج.م</b>', color, sign, formatted_amount)
    amount_styled.short_description = "المبلغ"

    def fraud_risk_badge(self, obj):
        if obj.amount > 50000 and obj.transaction_type == 'withdrawal':
            return format_html('<span style="background:#dc3545; color:white; padding:2px 6px; border-radius:4px; font-size:10px;">⚠️ فحص أمني مكثف</span>')
        return format_html('<span style="color:#28a745;"><i class="fas fa-shield-alt"></i> آمن ومطابق</span>')
    fraud_risk_badge.short_description = "الرادار الأمني"


# =====================================================================
# 💎 6. إدارة باقات الاشتراك (Subscription Plans Admin)
# =====================================================================
@admin.register(Plan)
class PlanAdmin(PublicSchemaOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        'name', 'industry_badge', 'monthly_price_styled',
        'discount_overview', 'limits_overview', 'is_active_icon', 'sort_order',
    )
    list_filter = ('industry', 'is_active')
    search_fields = ('name', 'slug')
    list_editable = ('sort_order',)
    prepopulated_fields = {'slug': ('name',)}
    ordering = ('sort_order',)

    fieldsets = (
        ('البيانات الأساسية', {
            'fields': ('name', 'slug', 'industry', 'is_active', 'sort_order'),
        }),
        ('التسعير والخصومات', {
            'fields': ('monthly_price', 'quarterly_discount', 'semi_annual_discount', 'annual_discount'),
            'description': "الأسعار بالجنيه المصري. الخصومات كنسبة مئوية من السعر الشهري."
        }),
        ('حدود الاستخدام', {
            'fields': ('max_branches', 'max_users', 'max_treasuries'),
        }),
        ('المميزات (JSON)', {
            'fields': ('features',),
            'classes': ('collapse',),
        }),
    )

    def industry_badge(self, obj):
        icon = '🚗' if obj.industry == 'automotive' else '🎨'
        return format_html('{} {}', icon, obj.get_industry_display())
    industry_badge.short_description = "القطاع"

    def monthly_price_styled(self, obj):
        return format_html('<b style="color:#007bff;">{} ج.م</b>', f"{obj.monthly_price:,.2f}")
    monthly_price_styled.short_description = "السعر الشهري"

    def discount_overview(self, obj):
        return format_html(
            '<span style="font-size:11px;">ربع: {}% | نصف: {}% | سنوي: {}%</span>',
            obj.quarterly_discount, obj.semi_annual_discount, obj.annual_discount
        )
    discount_overview.short_description = "الخصومات"

    def limits_overview(self, obj):
        return format_html(
            '<span style="font-size:11px;">🏢 {} | 👥 {} | 🏦 {}</span>',
            obj.max_branches, obj.max_users, obj.max_treasuries
        )
    limits_overview.short_description = "الحدود"

    def is_active_icon(self, obj):
        if obj.is_active:
            return format_html('<span style="color:#28a745; font-size:16px;">✅</span>')
        return format_html('<span style="color:#dc3545; font-size:16px;">❌</span>')
    is_active_icon.short_description = "مفعّلة"


# =====================================================================
# 🤖 7. إدارة حزم الذكاء الاصطناعي (AI Add-on Packages Admin)
# =====================================================================
@admin.register(AIAddonPackage)
class AIAddonPackageAdmin(PublicSchemaOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'monthly_price_styled', 'ai_generations_limit', 'whatsapp_messages_limit', 'is_active_icon')
    list_filter = ('is_active',)
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    ordering = ('sort_order',)

    def monthly_price_styled(self, obj):
        return format_html('<b style="color:#6f42c1;">{} ج.م</b>', f"{obj.monthly_price:,.2f}")
    monthly_price_styled.short_description = "السعر الشهري"

    def is_active_icon(self, obj):
        if obj.is_active:
            return format_html('<span style="color:#28a745; font-size:16px;">✅</span>')
        return format_html('<span style="color:#dc3545; font-size:16px;">❌</span>')
    is_active_icon.short_description = "مفعّلة"


# =====================================================================
# 📋 8. لوحة تحكم الاشتراكات (Tenant Subscription Admin — Super Admin HQ)
# =====================================================================
@admin.register(TenantSubscription)
class TenantSubscriptionAdmin(PublicSchemaOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        'tenant_name', 'plan_badge', 'ai_addon_badge', 'billing_cycle_display',
        'period_display', 'days_remaining', 'is_active_icon',
    )
    list_filter = ('is_active', 'plan__industry', 'plan', 'ai_addon', 'billing_cycle_months')
    search_fields = ('tenant__name', 'tenant__schema_name', 'tenant__email', 'tenant__phone')
    raw_id_fields = ('tenant',)
    readonly_fields = ('created_at', 'updated_at')
    list_per_page = 25

    fieldsets = (
        ('المستأجر', {
            'fields': ('tenant',),
        }),
        ('الباقة والدورة', {
            'fields': ('plan', 'ai_addon', 'billing_cycle_months', 'is_active'),
        }),
        ('فترة الاشتراك الحالية', {
            'fields': ('current_period_start', 'current_period_end'),
            'description': "تواريخ بدء وانتهاء الفترة الحالية. يمكن تعديلها يدوياً لتفعيل أو تمديد الاشتراك."
        }),
        ('بيانات النظام', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    actions = [
        'activate_subscription_1_month',
        'activate_subscription_3_months',
        'activate_subscription_6_months',
        'activate_subscription_12_months',
        'deactivate_subscriptions',
        'sync_subscription_to_client',
    ]

    # ── Display columns ──────────────────────────────────────────

    def tenant_name(self, obj):
        return format_html('<b>{}</b> <span style="color:#6c757d; font-size:11px;">({})</span>',
                           obj.tenant.name, obj.tenant.schema_name)
    tenant_name.short_description = "المستأجر"
    tenant_name.admin_order_field = 'tenant__name'

    def plan_badge(self, obj):
        if not obj.plan:
            return format_html('<span style="color:#dc3545;">بدون باقة</span>')
        colors = {'automotive': '#17a2b8', 'printing': '#6f42c1'}
        bg = colors.get(obj.plan.industry, '#6c757d')
        return format_html(
            '<span style="background:{}; color:white; padding:3px 8px; border-radius:4px; font-size:11px;">{}</span>',
            bg, obj.plan.name
        )
    plan_badge.short_description = "الباقة"
    plan_badge.admin_order_field = 'plan__name'

    def ai_addon_badge(self, obj):
        if not obj.ai_addon:
            return "—"
        return format_html('<span style="color:#6f42c1; font-weight:bold;">🤖 {}</span>', obj.ai_addon.name)
    ai_addon_badge.short_description = "حزمة AI"

    def billing_cycle_display(self, obj):
        labels = {1: 'شهري', 3: 'ربع سنوي', 6: 'نصف سنوي', 12: 'سنوي'}
        return labels.get(obj.billing_cycle_months, f'{obj.billing_cycle_months} شهور')
    billing_cycle_display.short_description = "دورة الفوترة"

    def period_display(self, obj):
        if not obj.current_period_start or not obj.current_period_end:
            return format_html('<span style="color:#dc3545;">غير محدد</span>')
        return format_html(
            '<span style="font-size:11px;">{} → {}</span>',
            obj.current_period_start.strftime('%Y-%m-%d'),
            obj.current_period_end.strftime('%Y-%m-%d'),
        )
    period_display.short_description = "فترة الاشتراك"

    def days_remaining(self, obj):
        if not obj.current_period_end:
            return "—"
        remaining = (obj.current_period_end - timezone.now().date()).days
        if remaining > 30:
            color = '#28a745'
        elif remaining > 7:
            color = '#ffc107'
        elif remaining > 0:
            color = '#fd7e14'
        else:
            color = '#dc3545'
        text = f'{remaining} يوم' if remaining > 0 else ('ينتهي اليوم' if remaining == 0 else f'منتهي منذ {abs(remaining)} يوم')
        return format_html('<span style="color:{}; font-weight:bold;">{}</span>', color, text)
    days_remaining.short_description = "المتبقي"

    def is_active_icon(self, obj):
        if obj.is_active:
            return format_html('<span style="color:#28a745; font-size:16px;">✅</span>')
        return format_html('<span style="color:#dc3545; font-size:16px;">❌</span>')
    is_active_icon.short_description = "فعّال"

    # ── Activation actions ────────────────────────────────────────

    def _activate_subscriptions(self, request, queryset, months):
        """Helper: activate selected subscriptions for N months from today."""
        today = timezone.now().date()
        success = 0
        errors = 0

        for sub in queryset:
            try:
                with transaction.atomic():
                    # Set subscription dates
                    sub.current_period_start = today
                    sub.current_period_end = today + relativedelta(months=months)
                    sub.billing_cycle_months = months
                    sub.is_active = True
                    sub.save()

                    # Sync to Client model
                    client = sub.tenant
                    client.status = 'active'
                    client.subscription_end_date = sub.current_period_end
                    client.is_active = True

                    # Sync plan limits if subscription has a Plan linked
                    if sub.plan:
                        client.max_branches = sub.plan.max_branches
                        client.max_users = sub.plan.max_users
                        client.max_treasuries = sub.plan.max_treasuries

                    client.save(update_fields=[
                        'status', 'subscription_end_date', 'is_active',
                        'max_branches', 'max_users', 'max_treasuries',
                    ])

                    success += 1
                    logger.info(
                        f"✅ [SUBSCRIPTION ACTIVATED]: {client.name} — "
                        f"Plan: {sub.plan}, Period: {months}m, "
                        f"End: {sub.current_period_end}"
                    )
            except Exception as e:
                errors += 1
                logger.error(f"🔴 [SUBSCRIPTION ACTIVATION ERROR]: {sub.tenant.name} — {e}")

        if success:
            self.message_user(
                request,
                f"✅ تم تفعيل {success} اشتراك لمدة {months} شهر بنجاح. "
                f"تاريخ الانتهاء: {today + relativedelta(months=months)}",
                messages.SUCCESS,
            )
        if errors:
            self.message_user(
                request,
                f"⚠️ فشل تفعيل {errors} اشتراك. راجع سجل الأخطاء.",
                messages.ERROR,
            )

    @admin.action(description='✅ تفعيل اشتراك — شهر واحد (1 month)')
    def activate_subscription_1_month(self, request, queryset):
        self._activate_subscriptions(request, queryset, 1)

    @admin.action(description='✅ تفعيل اشتراك — ربع سنوي (3 months)')
    def activate_subscription_3_months(self, request, queryset):
        self._activate_subscriptions(request, queryset, 3)

    @admin.action(description='✅ تفعيل اشتراك — نصف سنوي (6 months)')
    def activate_subscription_6_months(self, request, queryset):
        self._activate_subscriptions(request, queryset, 6)

    @admin.action(description='✅ تفعيل اشتراك — سنوي (12 months)')
    def activate_subscription_12_months(self, request, queryset):
        self._activate_subscriptions(request, queryset, 12)

    @admin.action(description='🛑 إيقاف الاشتراكات المحددة')
    def deactivate_subscriptions(self, request, queryset):
        count = 0
        for sub in queryset:
            with transaction.atomic():
                sub.is_active = False
                sub.save(update_fields=['is_active'])
                client = sub.tenant
                client.status = 'suspended'
                client.is_active = False
                client.save(update_fields=['status', 'is_active'])
                count += 1
        self.message_user(
            request,
            f"🛑 تم إيقاف {count} اشتراك وتعليق حسابات الشركات المرتبطة.",
            messages.WARNING,
        )

    @admin.action(description='🔄 مزامنة بيانات الاشتراك → جدول العملاء (Client sync)')
    def sync_subscription_to_client(self, request, queryset):
        """Sync TenantSubscription data back to Client model fields."""
        count = 0
        for sub in queryset:
            try:
                client = sub.tenant
                if sub.plan:
                    client.max_branches = sub.plan.max_branches
                    client.max_users = sub.plan.max_users
                    client.max_treasuries = sub.plan.max_treasuries
                client.subscription_end_date = sub.current_period_end
                client.status = 'active' if sub.is_active else 'suspended'
                client.is_active = sub.is_active
                client.save(update_fields=[
                    'max_branches', 'max_users', 'max_treasuries',
                    'subscription_end_date', 'status', 'is_active',
                ])
                count += 1
            except Exception as e:
                logger.error(f"🔴 [SYNC ERROR]: {sub.tenant.name} — {e}")
        self.message_user(
            request,
            f"🔄 تمت مزامنة {count} اشتراك مع جدول العملاء بنجاح.",
            messages.SUCCESS,
        )

    def save_model(self, request, obj, form, change):
        """Auto-sync Client model when saving subscription from admin."""
        super().save_model(request, obj, form, change)
        try:
            client = obj.tenant
            if obj.plan:
                client.max_branches = obj.plan.max_branches
                client.max_users = obj.plan.max_users
                client.max_treasuries = obj.plan.max_treasuries
            client.subscription_end_date = obj.current_period_end
            if obj.is_active and obj.current_period_end and obj.current_period_end >= timezone.now().date():
                client.status = 'active'
                client.is_active = True
            client.save(update_fields=[
                'max_branches', 'max_users', 'max_treasuries',
                'subscription_end_date', 'status', 'is_active',
            ])
            logger.info(f"✅ [SUBSCRIPTION SAVE SYNC]: {client.name} synced from admin save.")
        except Exception as e:
            logger.error(f"🔴 [SUBSCRIPTION SAVE SYNC ERROR]: {e}")


# =====================================================================
# 📊 9. سجل استهلاك الذكاء الاصطناعي (AI Usage Tracker Admin)
# =====================================================================
@admin.register(AILimitTracker)
class AILimitTrackerAdmin(PublicSchemaOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('tenant_name', 'action_type_badge', 'used_at', 'metadata_preview')
    list_filter = ('action_type', 'used_at', 'tenant')
    search_fields = ('tenant__name', 'tenant__schema_name')
    readonly_fields = [f.name for f in AILimitTracker._meta.fields]
    date_hierarchy = 'used_at'
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def tenant_name(self, obj):
        return obj.tenant.name
    tenant_name.short_description = "المستأجر"
    tenant_name.admin_order_field = 'tenant__name'

    def action_type_badge(self, obj):
        colors = {
            'ai_generation': '#6f42c1',
            'whatsapp_send': '#25d366',
            'smart_watermark': '#17a2b8',
        }
        color = colors.get(obj.action_type, '#6c757d')
        return format_html(
            '<span style="background:{}; color:white; padding:3px 8px; border-radius:4px; font-size:11px;">{}</span>',
            color, obj.get_action_type_display()
        )
    action_type_badge.short_description = "نوع العملية"

    def metadata_preview(self, obj):
        if not obj.metadata:
            return "—"
        text = str(obj.metadata)
        if len(text) > 80:
            text = text[:80] + '…'
        return format_html('<span style="font-size:11px; color:#6c757d;" title="{}">{}</span>', str(obj.metadata), text)
    metadata_preview.short_description = "بيانات إضافية"