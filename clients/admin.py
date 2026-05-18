from django.contrib import admin
from django.utils.html import format_html
from django.contrib import messages
from django.db import transaction
from django.db import connection  # مستشعر فحص النطاقات السحابية اللحظي
from django.core.exceptions import ValidationError

# استدعاء جميع نماذج الإمبراطورية بعد التحديثات الأخيرة
from .models import Client, Domain, GlobalB2BMarketplace, BlindBiddingRequest, BidOffer, EscrowLedger

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

# =====================================================================
# 🏢 2. لوحة القيادة المركزية لشركات Mouss Tec (محمية بالكامل 🔒)
# =====================================================================
@admin.register(Client)
class ClientAdmin(PublicSchemaOnlyAdminMixin, admin.ModelAdmin):
    inlines = [DomainInline]
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
            'fields': ('plan', 'status', 'trial_ends_at', 'subscription_end_date', 'max_branches', 'max_users')
        }),
        ('التخصيص الفني (White-labeling)', {
            # ✅ تم إزالة الحقل الوهمي auto_create_schema للقضاء على خطأ FieldError نهائياً
            'fields': ('logo', 'theme_color')
        }),
    )

    actions = ['verify_merchants', 'suspend_clients']

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
            domain = obj.domains.first().domain
            url = f"http://{domain}/system/dashboard/" if connection.schema_name == 'public' else f"http://{domain}:8000/system/dashboard/"
            return format_html('<a class="button" href="{}" target="_blank" style="background-color:#28a745; color:white; padding:5px 12px; border-radius:4px; text-decoration:none; font-weight:bold; font-size:12px;">🚀 الإدارة الميدانية</a>', url)
        except:
            return format_html('<span style="color:#dc3545; font-weight:bold; font-size:11px;">⚠️ بدون نطاق</span>')
    enter_tenant_dashboard.short_description = "الدخول السريع"

    @admin.action(description='✅ منح علامة التوثيق (العلامة الزرقاء) للتجار المحددين')
    def verify_merchants(self, request, queryset):
        updated = queryset.update(is_verified_merchant=True)
        self.message_user(request, f"تم توثيق عدد {updated} تاجر/مركز بنجاح بنظام KYC.", messages.SUCCESS)

    @admin.action(description='🛑 تعليق حسابات الشركات المحددة (إيقاف النظام)')
    def suspend_clients(self, request, queryset):
        updated = queryset.update(status='suspended', is_active=False)
        self.message_user(request, f"تم إيقاف عدد {updated} شركة وعزل فروعها عن السحابة.", messages.WARNING)
# بداخل كلاس ClientAdmin في ملف clients/admin.py
exclude = ('auto_create_schema', 'auto_drop_schema')

# =====================================================================
# 🛒 3. إدارة السوق المركزي (Global Marketplace - مصفى ومؤمن 🔐)
# =====================================================================
@admin.register(GlobalB2BMarketplace)
class GlobalB2BMarketplaceAdmin(admin.ModelAdmin):
    list_display = ('part_number', 'product_name', 'tenant', 'condition', 'wholesale_price_styled', 'available_qty', 'ai_confidence_score_badge')
    list_filter = ('condition', 'brand', 'tenant')
    search_fields = ('part_number', 'product_name', 'tenant__name')
    readonly_fields = ('updated_at',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if connection.schema_name != 'public':
            return qs.filter(tenant__schema_name=connection.schema_name)
        return qs

    def wholesale_price_styled(self, obj): return format_html('<b style="color:#007bff;">{} ج.م</b>', f"{obj.wholesale_price:,.2f}")
    wholesale_price_styled.short_description = "سعر الجملة"

    def ai_confidence_score_badge(self, obj):
        color = "#28a745" if obj.ai_quality_confidence >= 80 else "#ffc107" if obj.ai_quality_confidence >= 50 else "#dc3545"
        return format_html('<div style="width: 100px; background-color: #e9ecef; border-radius: 4px; overflow: hidden;"><div style="width: {}%; background-color: {}; height: 8px;"></div></div> <span style="font-size:10px; color:{};">{}%</span>', obj.ai_quality_confidence, color, color, color, obj.ai_quality_confidence)
    ai_confidence_score_badge.short_description = "مؤشر جودة AI"


# =====================================================================
# ⚖️ 4. غرفة عمليات المزاد العكسي وفض النزاعات (Atomic & Fully Integrated 🔐)
# =====================================================================
@admin.register(BlindBiddingRequest)
class BlindBiddingRequestAdmin(admin.ModelAdmin):
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