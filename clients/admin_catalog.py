"""🛍️ أدمن كتالوج السوق + بائعين السوق (لصاحب المنصة بس).

اتفصل من clients/admin.py عشان الملف مايعدّيش حد الحجم (erp_core/tests/test_codebase_structure.py).
بيتحمّل من آخر clients/admin.py.
"""
from django.contrib import admin
from django.db import connection

from .admin import _PlatformOnlyAdminMixin

# 🛍️ كتالوج السوق (متاجر Mouss Tec) — تعديل المتاجر والمنتجات ورفع الصور (لصاحب المنصة بس)
from .models import CatalogItem, CatalogQuoteRequest, CatalogStore


class _CatalogItemInline(admin.TabularInline):
    model = CatalogItem
    extra = 0
    fields = ('title', 'category', 'icon', 'image', 'min_qty', 'price_from', 'is_active', 'sort')
    show_change_link = True


@admin.register(CatalogStore)
class CatalogStoreAdmin(_PlatformOnlyAdminMixin, admin.ModelAdmin):
    # ⭐ اختيار Mouss Tec + تقييم Mouss Tec (رأي المنصة، بيتعرض باسمه منفصل عن تقييم العملاء)
    # تقييم Mouss Tec: «ظاهر» ⇒ بيتعرض للعميل باسمه؛ «مخفي» ⇒ بيرتّب المتاجر بس من غير رقم
    list_display = ('name', 'sector', 'city', 'is_featured', 'platform_score', 'show_platform_score', 'is_active', 'sort')
    list_filter = ('sector', 'is_featured', 'show_platform_score', 'is_active')
    list_editable = ('is_featured', 'platform_score', 'show_platform_score', 'is_active', 'sort')
    search_fields = ('name', 'tagline', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [_CatalogItemInline]

    def has_module_permission(self, request):
        return connection.schema_name == 'public' and request.user.is_superuser


@admin.register(CatalogItem)
class CatalogItemAdmin(_PlatformOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('title', 'store', 'category', 'car_make', 'has_image', 'price_from', 'admin_hidden', 'is_active')
    list_filter = ('admin_hidden', 'store__sector', 'category', 'is_active', 'store')
    list_editable = ('price_from', 'is_active')
    search_fields = ('title', 'description', 'car_make', 'car_models', 'store__name')
    autocomplete_fields = ('store',)
    actions = ('approve_items', 'reject_items')

    # 🏪 منتجات بائعين السوق بتظهر على طول — صاحب المنصة يقدر يخفيها (والبائع مايقدرش يرجّعها)
    @admin.action(description='✅ إظهار المنتجات للعملاء')
    def approve_items(self, request, queryset):
        n = queryset.update(is_active=True, pending_review=False, admin_hidden=False)
        self.message_user(request, f'ظهر {n} منتج.')

    @admin.action(description='🙈 إخفاء المنتجات (البائع مايقدرش يرجّعها)')
    def reject_items(self, request, queryset):
        n = queryset.update(is_active=False, pending_review=False, admin_hidden=True)
        self.message_user(request, f'اتخفى {n} منتج.')

    def save_model(self, request, obj, form, change):
        # إخفاء منتج بائع من الأدمن (حتى من عمود «is_active») ⇒ يفضل مخفي والبائع مايرجّعوش
        if obj.store.is_seller_store:
            obj.admin_hidden = not obj.is_active
            obj.pending_review = False
        super().save_model(request, obj, form, change)

    @admin.display(boolean=True, description='صورة')
    def has_image(self, obj):
        return bool(obj.image)

    def has_module_permission(self, request):
        return connection.schema_name == 'public' and request.user.is_superuser


from .models import MarketSeller


@admin.register(MarketSeller)
class MarketSellerAdmin(_PlatformOnlyAdminMixin, admin.ModelAdmin):
    """🏪 بائعين السوق (من غير برنامج الإدارة) — الموافقة على المتجر = تفعيله."""
    list_display = ('store', 'owner_name', 'phone', 'sector_display', 'store_live', 'pending_items',
                    'is_blocked', 'upgraded_to', 'created_at')
    list_filter = ('store__sector', 'store__is_active', 'is_blocked')
    search_fields = ('owner_name', 'phone', 'email', 'store__name')
    readonly_fields = ('uid', 'store', 'phone', 'upgraded_to', 'market_rating', 'successful_deals',
                       'last_login_at', 'created_at')
    exclude = ('password_hash', 'session_token')
    actions = ('approve_stores', 'hide_stores')

    @admin.display(description='النشاط')
    def sector_display(self, obj):
        return obj.store.get_sector_display()

    @admin.display(boolean=True, description='المتجر ظاهر')
    def store_live(self, obj):
        return obj.store.is_active

    @admin.display(description='المنتجات الظاهرة')
    def pending_items(self, obj):
        return obj.store.items.filter(is_active=True).count()

    @admin.action(description='✅ إظهار المتجر للعملاء')
    def approve_stores(self, request, queryset):
        n = CatalogStore.objects.filter(seller__in=queryset).update(is_active=True)
        self.message_user(request, f'اتفعّل {n} متجر.')

    @admin.action(description='🙈 إخفاء المتجر')
    def hide_stores(self, request, queryset):
        n = CatalogStore.objects.filter(seller__in=queryset).update(is_active=False)
        self.message_user(request, f'اتخفى {n} متجر.')

    def has_add_permission(self, request):
        return False

    def has_module_permission(self, request):
        return connection.schema_name == 'public' and request.user.is_superuser


@admin.register(CatalogQuoteRequest)
class CatalogQuoteRequestAdmin(_PlatformOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('item_title', 'name', 'phone', 'sector', 'status', 'created_at')
    list_filter = ('status', 'sector')
    search_fields = ('item_title', 'name', 'phone', 'details')
    readonly_fields = ('item', 'customer', 'ip_address', 'created_at', 'updated_at')

    def has_module_permission(self, request):
        return connection.schema_name == 'public' and request.user.is_superuser



from .models import CatalogReview


@admin.register(CatalogReview)
class CatalogReviewAdmin(_PlatformOnlyAdminMixin, admin.ModelAdmin):
    """مراجعة تقييمات العملاء: تخفي سبام/إساءة بسبب — لكن النجوم والتعليق مابيتعدّلوش."""
    list_display = ('store', 'rating', 'reviewer_name', 'comment', 'is_visible', 'hidden_reason', 'created_at')
    list_filter = ('is_visible', 'rating', 'store__sector', 'store')
    list_editable = ('is_visible', 'hidden_reason')
    search_fields = ('comment', 'reviewer_name', 'store__name')
    readonly_fields = ('quote_request', 'store', 'item', 'rating', 'comment', 'reviewer_name', 'created_at')

    def has_add_permission(self, request):
        return False   # التقييم بييجي من العميل بس

    def has_module_permission(self, request):
        return connection.schema_name == 'public' and request.user.is_superuser


# 📈 بورصة أسعار المطابع — البنود (تتعدّل/تتزوّد) والأسعار (للمراجعة: الشاذ بيتعلّم لوحده)
from .models import PriceQuote, PriceSpec  # noqa: E402


@admin.register(PriceSpec)
class PriceSpecAdmin(_PlatformOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'spec', 'unit', 'kind', 'is_active', 'sort')
    list_filter = ('kind', 'is_active', 'sector')
    list_editable = ('is_active', 'sort')
    search_fields = ('name', 'name_en', 'code')

    def has_module_permission(self, request):
        return connection.schema_name == 'public' and request.user.is_superuser


@admin.register(PriceQuote)
class PriceQuoteAdmin(_PlatformOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('spec', 'price', 'governorate', 'quoted_on', 'seller', 'merchant', 'is_outlier')
    list_filter = ('is_outlier', 'quoted_on', 'governorate', 'spec')
    list_editable = ('is_outlier',)
    search_fields = ('seller__store__name', 'merchant__name', 'spec__name')
    raw_id_fields = ('seller', 'merchant')
    date_hierarchy = 'quoted_on'

    def has_module_permission(self, request):
        return connection.schema_name == 'public' and request.user.is_superuser
