from django.conf import settings
from django.db import models, transaction
from django_tenants.models import TenantMixin, DomainMixin
from clients.soft_delete import SoftDeleteMixin
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.db.models import F
from datetime import timedelta
from decimal import Decimal
import uuid
import logging

logger = logging.getLogger('mouss_tec_core')


# Cross-domain: tenancy (Client, Plan, …) and marketplace_c2c
# (MarketplaceCustomer) referenced by FKs below.
from .tenancy import *  # noqa: F401, F403
from .marketplace_c2c import *  # noqa: F401, F403

# Design Store: AI design packages, generation conversations, brand profiles.
# =====================================================================
# 🎨 AI Designs Store — مكتبة التصاميم الفورية للعملاء النهائيين
# =====================================================================

class DesignPackage(models.Model):
    """
    📦 باقة شراء تصاميم — العميل يدفع مرة واحدة ويستهلك التصاميم تدريجياً.
    """
    PACKAGE_TIERS = (
        ('starter', _('🥉 Starter — 25 تصميم')),
        ('pro', _('🥈 Pro — 50 تصميم')),
        ('business', _('🥇 Business — 100 تصميم')),
        ('studio', _('💎 Studio — 250 تصميم')),
        
        # الباقات التجارية الجديدة للعملاء (Marketplace Customers)
        ('cust_50', _('🎯 باقة 50 تصميم — 25 ج.م')),
        ('cust_100', _('🎯 باقة 100 تصميم — 35 ج.م')),
        ('cust_500', _('🎯 باقة 500 تصميم — 150 ج.م')),
        
        # باقات الشركات والمطابع الإضافية (Merchant Top-ups)
        ('comp_1000', _('🏢 باقة الشركات 1000 تصميم — 250 ج.م')),
        ('comp_2500', _('🏢 باقة الشركات 2500 تصميم — 500 ج.م')),
        ('comp_5000', _('🏢 باقة الشركات 5000 تصميم — 900 ج.م')),
        
        # Designer packages
        ('des_15', _('🎨 باقة 15 تصميم')),
        ('des_25', _('🎨 باقة 25 تصميم')),
        ('des_50', _('🎨 باقة 50 تصميم')),
        ('des_100', _('🎨 باقة 100 تصميم')),

        # ✨ 2026 launch packages — short clean slugs
        ('c10',  _('🎯 باقة 10 تصاميم — عميل')),
        ('c20',  _('🎯 باقة 20 تصميم — عميل')),
        ('c50',  _('🎯 باقة 50 تصميم — عميل')),
        ('d20',  _('🏢 باقة 20 تصميم — مصمم/شركة')),
        ('d50',  _('🏢 باقة 50 تصميم — مصمم/شركة')),
        ('d100', _('🏢 باقة 100 تصميم — مصمم/شركة')),
    )

    AUDIENCE_CHOICES = (
        ('customer', _('عملاء أفراد')),
        ('designer', _('مصممين / شركات')),
    )
    slug = models.CharField(max_length=20, choices=PACKAGE_TIERS, unique=True)
    target_audience = models.CharField(max_length=10, choices=AUDIENCE_CHOICES, default='customer',
        verbose_name=_("الفئة المستهدفة"))
    name_ar = models.CharField(max_length=100, verbose_name=_("الاسم بالعربي"))
    designs_count = models.IntegerField(verbose_name=_("عدد التصاميم (عميل)"))
    designer_designs_count = models.IntegerField(default=0, verbose_name=_("عدد التصاميم (مصمم)"),
        help_text=_("نفس السعر لكن عدد أكبر للمصممين. 0 = نفس عدد العملاء"))
    price_egp = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("السعر بالجنيه"))
    price_per_design = models.DecimalField(max_digits=10, decimal_places=2, editable=False)

    # Feature flags
    allows_logo_upload = models.BooleanField(default=True)
    allows_watermark = models.BooleanField(default=False)
    allows_source_files = models.BooleanField(default=False, verbose_name=_("ملفات مصدر (PSD/SVG)"))
    allows_commercial_use = models.BooleanField(default=True, verbose_name=_("استخدام تجاري"))
    allows_whatsapp_delivery = models.BooleanField(default=True)
    free_regenerations_per_design = models.IntegerField(default=2, verbose_name=_("إعادة توليد مجاني (2 محاولة)"))

    # Quality
    resolution_max = models.CharField(max_length=20, default='2048x2048', verbose_name=_("أعلى دقة"))
    quality_level = models.CharField(max_length=20, default='hd',
        choices=[('standard', 'عادية'), ('hd', 'عالية'), ('ultra', 'فائقة')])

    # Display
    icon_emoji = models.CharField(max_length=10, default='🎨')
    accent_color = models.CharField(max_length=7, default='#8b5cf6')
    is_featured = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    description_html = models.TextField(blank=True, verbose_name=_("الوصف"))
    badge_text = models.CharField(max_length=50, blank=True, verbose_name=_("شارة (مثل: الأكثر مبيعاً)"))

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("باقة تصاميم")
        verbose_name_plural = _("🎨 باقات تصاميم AI")
        ordering = ['sort_order', 'designs_count']

    def __str__(self):
        return f"{self.icon_emoji} {self.name_ar} — {self.price_egp} ج.م"

    def save(self, *args, **kwargs):
        if self.designs_count > 0:
            self.price_per_design = (self.price_egp / Decimal(str(self.designs_count))).quantize(Decimal('0.01'))
        super().save(*args, **kwargs)

    @property
    def savings_vs_starter(self):
        """نسبة التوفير مقارنة بالـ Starter"""
        starter = DesignPackage.objects.filter(slug='starter').first()
        if not starter or starter.pk == self.pk:
            return 0
        diff = starter.price_per_design - self.price_per_design
        return int((diff / starter.price_per_design) * 100) if starter.price_per_design > 0 else 0


class DesignPurchase(models.Model):
    """
    🛒 عملية شراء باقة من عميل.
    """
    STATUS_CHOICES = (
        ('pending', _('في انتظار الدفع')),
        ('awaiting_confirm', _('في انتظار تأكيد الدفع')),
        ('paid', _('مدفوعة — جاهزة للاستخدام')),
        ('rejected', _('مرفوضة')),
        ('exhausted', _('تم استهلاكها بالكامل')),
        ('refunded', _('مردودة')),
        ('expired', _('منتهية الصلاحية')),
    )
    PAYMENT_METHODS = (
        ('paymob', _('بطاقة ائتمان (Paymob)')),
        ('vodafone_cash', _('فودافون كاش')),
        ('instapay', _('إنستاباي')),
        ('cash_collect', _('دفع عند الاستلام')),
        ('admin_grant', _('منحة من الإدارة')),
    )

    purchase_code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    customer = models.ForeignKey(MarketplaceCustomer, on_delete=models.CASCADE, related_name='design_purchases')
    package = models.ForeignKey(DesignPackage, on_delete=models.PROTECT, related_name='purchases')

    designs_total = models.IntegerField()  # snapshot of package.designs_count at purchase time
    designs_used = models.IntegerField(default=0)
    price_paid = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS, default='paymob')
    payment_reference = models.CharField(max_length=200, blank=True)
    sender_phone = models.CharField(max_length=20, blank=True, verbose_name=_("رقم المرسل"))

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name=_("ينتهي في"))

    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("شراء باقة تصاميم")
        verbose_name_plural = _("🛒 مشتريات باقات التصاميم")
        ordering = ['-created_at']

    def __str__(self):
        return f"#{self.id} | {self.customer.full_name} | {self.package.name_ar}"

    @property
    def designs_remaining(self):
        return max(self.designs_total - self.designs_used, 0)

    @property
    def is_usable(self):
        if self.status != 'paid':
            return False
        if self.designs_remaining <= 0:
            return False
        if self.expires_at and timezone.now() > self.expires_at:
            return False
        return True

    def consume_design(self):
        """خصم تصميم من الباقة (atomic)"""
        from django.db import transaction as _tx
        with _tx.atomic():
            type(self).objects.filter(pk=self.pk).update(designs_used=F('designs_used') + 1)
            self.refresh_from_db()
            if self.designs_used >= self.designs_total:
                self.status = 'exhausted'
                self.save(update_fields=['status'])


class CustomerDesign(models.Model):
    """
    🖼️ تصميم تم توليده للعميل — مع كل المواصفات والـ specs.
    """
    DESIGN_CATEGORIES = (
        ('logo', _('لوجو / Brand Mark')),
        ('business_card', _('كارت بزنس')),
        ('letterhead', _('ورق رسمي')),
        ('stamp', _('ختم')),
        ('social_post', _('بوست سوشيال ميديا')),
        ('story', _('ستوري / Reels')),
        ('cover', _('غلاف فيسبوك / يوتيوب')),
        ('flyer', _('فلاير')),
        ('poster', _('بوستر / إعلان')),
        ('banner', _('بنر / Roll-up')),
        ('sign', _('يافطة / كلادينج')),
        ('menu', _('منيو')),
        ('invitation', _('دعوة / كارت')),
        ('certificate', _('شهادة')),
        ('brochure', _('بروشور')),
        ('receipt_form', _('فاتورة / نموذج')),
        ('tshirt', _('تيشرت / Merch')),
        ('pants', _('بنطلون / ملابس')),
        ('abaya', _('عباية / جلابية')),
        ('uniform', _('يونيفورم')),
        ('cap', _('كاب / طاقية')),
        ('bag', _('شنطة / حقيبة')),
        ('shoe', _('حذاء / جزمة')),
        ('packaging', _('تغليف / منتج')),
        ('label', _('ليبل / ملصق')),
        ('sticker', _('ستيكر')),
        ('mug_design', _('تصميم ماج')),
        ('mockup', _('Mockup منتج')),
        ('film_poster', _('بوستر فيلم')),
        ('book_cover', _('غلاف كتاب')),
        ('album_cover', _('غلاف ألبوم')),
        ('thumbnail', _('Thumbnail يوتيوب')),
        ('pattern', _('باترن / نقشة')),
        ('illustration', _('رسم توضيحي')),
        ('infographic', _('إنفوجرافيك')),
        ('car_wrap', _('رسم سيارة / Wrap')),
        ('other', _('أخرى')),
    )
    OUTPUT_FORMATS = (
        ('png', 'PNG'),
        ('jpg', 'JPEG'),
        ('webp', 'WebP'),
        ('pdf', 'PDF Print-Ready'),
    )
    SIZE_PRESETS = (
        ('auto', '🤖 تلقائي'),
        ('1024x1024', '🟦 مربع 1:1'),
        ('1024x1536', '📱 طولي 2:3'),
        ('1536x1024', '🖥️ أفقي 3:2'),
        ('1024x1792', '📱 قصة 1024×1792'),
        ('1792x1024', '🖥️ عريض 1792×1024'),
        ('2048x2048', '⚡ مربع HD'),
        ('a4', '📄 A4'),
        ('a3', '📑 A3'),
        ('a5', '📃 A5'),
        ('business_card', '💳 كارت بزنس'),
        ('banner_wide', '🪧 بنر عريض'),
        ('rollup', '📐 Roll-up'),
        ('sign_square', '🏗️ يافطة مربعة'),
        ('sign_landscape', '🏗️ يافطة أفقية'),
        ('tshirt_chest', '👕 تيشرت صدر'),
        ('tshirt_full', '👕 تيشرت كامل'),
        ('pants_pattern', '👖 بنطلون'),
        ('abaya_pattern', '🧕 عباية / جلابية'),
        ('full_body', '🧍 Full Body'),
        ('mug', '☕ ماج'),
        ('bag', '👜 شنطة'),
        ('book_cover', '📚 غلاف كتاب'),
        ('youtube_thumb', '🖱️ Thumbnail'),
        ('film_poster', '🎬 بوستر فيلم'),
        ('custom', '⚙️ مقاس مخصص'),
    )

    design_code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    customer = models.ForeignKey(MarketplaceCustomer, on_delete=models.CASCADE, related_name='designs')
    purchase = models.ForeignKey(DesignPurchase, on_delete=models.PROTECT, null=True, blank=True,
        related_name='designs', verbose_name=_("الباقة (فارغ = تصميم مجاني)"))
    is_free_trial = models.BooleanField(default=False, verbose_name=_("تصميم مجاني (تجربة)"))

    # User input
    title = models.CharField(max_length=200, verbose_name=_("عنوان التصميم"))
    description = models.TextField(verbose_name=_("الوصف"))
    category = models.CharField(max_length=20, choices=DESIGN_CATEGORIES, default='other')

    # 🆕 User-controllable specs (مهم جداً للعميل)
    size_preset = models.CharField(max_length=30, choices=SIZE_PRESETS, default='1024x1024',
                                   verbose_name=_("المقاس"))
    custom_width_px = models.IntegerField(null=True, blank=True, verbose_name=_("العرض (px)"))
    custom_height_px = models.IntegerField(null=True, blank=True, verbose_name=_("الارتفاع (px)"))
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True,
                                    verbose_name=_("وزن المنتج (اختياري — للـ packaging)"))
    output_format = models.CharField(max_length=10, choices=OUTPUT_FORMATS, default='png')

    # Optional logo input
    logo_image = models.ImageField(upload_to='ai_store/logos/', blank=True, null=True)

    # AI engineering
    raw_input = models.TextField(blank=True)
    engineered_prompt = models.TextField(blank=True)
    negative_prompt = models.TextField(blank=True)

    # Result — image_url is the canonical original (PNG/JPG). The two
    # *_url variants are WebP thumbnails generated at persist-time for
    # faster gallery rendering (industry-standard: serve a 200×200 in the
    # grid instead of a 2MB full-res). All three live in default_storage.
    image_url = models.URLField(max_length=600, blank=True)
    image_thumb_url = models.URLField(max_length=600, blank=True,
        verbose_name=_("صورة مصغّرة (200px WebP)"))
    image_preview_url = models.URLField(max_length=600, blank=True,
        verbose_name=_("معاينة (512px WebP)"))
    image_persisted_at = models.DateTimeField(null=True, blank=True,
        verbose_name=_("تاريخ الحفظ المحلي"),
        help_text=_("None = صورة قديمة قبل الـ persistence pipeline"))
    image_size_bytes = models.IntegerField(null=True, blank=True,
        verbose_name=_("حجم الصورة الأصلية (bytes)"))
    model_used = models.CharField(max_length=50, blank=True)

    # Regenerations
    regenerations_used = models.IntegerField(default=0)
    regenerations_allowed = models.IntegerField(default=3)

    # Delivery
    sent_to_whatsapp = models.CharField(max_length=30, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    download_count = models.IntegerField(default=0)

    # Customer rating
    customer_rating = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("تصميم للعميل")
        verbose_name_plural = _("🖼️ تصاميم العملاء (AI Store)")
        ordering = ['-created_at']

    def __str__(self):
        return f"DESIGN-{str(self.design_code)[:8]} | {self.title[:40]}"

    @property
    def can_regenerate(self):
        return self.regenerations_used < self.regenerations_allowed

    @property
    def actual_size_label(self):
        """عرض المقاس بشكل جميل للعميل"""
        if self.size_preset == 'custom' and self.custom_width_px and self.custom_height_px:
            return f"{self.custom_width_px}×{self.custom_height_px} px"
        size_map = {
            'auto': 'تلقائي',
            '1024x1024': '1024×1024',
            '1024x1536': '1024×1536',
            '1536x1024': '1536×1024',
            '1024x1792': '1024×1792',
            '1792x1024': '1792×1024',
            '2048x2048': '2048×2048 HD',
            'a4': 'A4', 'a3': 'A3', 'a5': 'A5',
            'business_card': 'كارت بزنس',
            'banner_wide': 'بنر عريض', 'rollup': 'Roll-up',
            'sign_square': 'يافطة مربعة', 'sign_landscape': 'يافطة أفقية',
            'tshirt_chest': 'تيشرت صدر', 'tshirt_full': 'تيشرت كامل',
            'pants_pattern': 'بنطلون', 'abaya_pattern': 'عباية',
            'full_body': 'Full Body',
            'mug': 'ماج', 'bag': 'شنطة',
            'book_cover': 'غلاف كتاب', 'youtube_thumb': 'Thumbnail',
            'film_poster': 'بوستر فيلم',
        }
        return size_map.get(self.size_preset, self.size_preset)


class DesignChatMessage(models.Model):
    """
    💬 رسالة في محادثة التصميم — يحفظ كل التفاعل بين العميل و AI.
    """
    ROLE_CHOICES = (
        ('user', 'العميل'),
        ('assistant', 'المصمم AI'),
        ('system', 'النظام'),
    )
    design = models.ForeignKey(CustomerDesign, on_delete=models.CASCADE, related_name='chat_messages')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='user')
    content = models.TextField(verbose_name=_('محتوى الرسالة'))
    image_url = models.URLField(max_length=600, blank=True, verbose_name=_('صورة مرفقة'))
    is_refinement = models.BooleanField(default=False, verbose_name=_('تعديل تحسيني'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('رسالة تصميم')
        verbose_name_plural = _('💬 رسائل التصاميم (Chat)')
        ordering = ['created_at']

    def __str__(self):
        return f'{self.get_role_display()}: {self.content[:50]}'


# ═══════════════════════════════════════════════════════════════════════════
# 💬 N — Conversational Design Builder (Phase N.1)
# ───────────────────────────────────────────────────────────────────────────
# Session-level multi-turn chat where the customer builds up a design in
# natural language. Distinct from DesignChatMessage above, which is a
# post-design refine thread attached to a finalized CustomerDesign.
#
# Architecture: Hybrid (Model C). Free chat early ("planning"), cheap
# generate when the user commits, then FLUX-Kontext refinement on the live
# image. The intent classifier (Llama-3-8B JSON mode) routes each user
# message to chat | generate | refine; undo/finalize are explicit buttons.
#
# State of truth: `accumulated_context` JSON (the merged prompt state).
# The chat transcript lives in DesignConversationTurn rows.
# ═══════════════════════════════════════════════════════════════════════════

class DesignConversation(models.Model):
    """💬 جلسة محادثة لبناء تصميم — multi-turn chat → image.

    Each row = one customer's design-building session. References the
    "live" CustomerDesign that the chat is iterating on (NULL until the
    first generation). Carries the accumulated prompt state across turns
    in JSON, and tracks cost / turn limits per session.
    """
    STAGE_CHOICES = (
        ('planning',  _('تخطيط — لسه بنتكلم')),
        ('generated', _('اتولّد أول تصميم')),
        ('refining',  _('قيد التعديل')),
        ('finalized', _('تم الاعتماد')),
        ('abandoned', _('متروك')),
    )

    conversation_code = models.UUIDField(
        default=uuid.uuid4, editable=False, unique=True, db_index=True,
        verbose_name=_("رمز المحادثة"),
    )
    customer = models.ForeignKey(
        MarketplaceCustomer, on_delete=models.CASCADE,
        related_name='design_conversations',
        verbose_name=_("العميل"),
    )

    # The "live" design being iterated. NULL during planning stage.
    # PROTECT (not CASCADE) so accidentally deleting a finalized design
    # doesn't nuke its conversation history — analytics value.
    current_design = models.ForeignKey(
        CustomerDesign, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='source_conversations',
        verbose_name=_("التصميم الحالي"),
    )

    # 🎨 Snapshot the brand profile at conversation start. Lets analytics
    # see what brand state was applied even if the customer edits/deletes
    # their CustomerBrandProfile later. Empty dict for guests / no brand.
    brand_profile_snapshot = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("لقطة من ملف البراند وقت بدء المحادثة"),
    )

    # 🧠 The merged prompt state — the source of truth for the next
    # generation/refinement. Schema (informally):
    #   {
    #     "raw_idea": "تيشرت رياضي للجري",
    #     "selections": {"color_primary": "navy", "style": "minimal"},
    #     "reference_descriptions": [...],
    #     "domain": "apparel",
    #     "presentation_category": "apparel",
    #     "subtype": "tshirt",
    #     "brand_disabled": false,
    #     "history": [{"turn": 1, "patch": {...}}, ...]
    #   }
    accumulated_context = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("السياق المتراكم عبر التيرنات"),
    )

    stage = models.CharField(
        max_length=12, choices=STAGE_CHOICES, default='planning',
        db_index=True,
        verbose_name=_("المرحلة"),
    )

    # 🛡️ Advisory lock to prevent concurrent-message races (user double-taps
    # send → two parallel turns → state corruption). The orchestrator sets
    # this to `now + 30s` while processing a turn; expired locks are ignored.
    locked_until = models.DateTimeField(
        null=True, blank=True,
        verbose_name=_("مقفول حتى (advisory lock)"),
    )

    # 📊 Per-session counters & limits (DESIGN_CHAT_* defaults from settings).
    turn_count = models.PositiveSmallIntegerField(default=0,
        verbose_name=_("عدد التيرنات"))
    image_count = models.PositiveSmallIntegerField(default=0,
        verbose_name=_("عدد الصور المولّدة"))
    total_cost_credits = models.DecimalField(
        max_digits=10, decimal_places=4, default=0,
        verbose_name=_("التكلفة الإجمالية (credits)"),
    )

    # Lifecycle timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    abandoned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("💬 محادثة تصميم")
        verbose_name_plural = _("💬 محادثات التصاميم")
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['customer', 'stage']),
            models.Index(fields=['stage', '-updated_at']),
        ]

    def __str__(self) -> str:
        return f'Conversation {self.conversation_code} ({self.stage})'

    # ── Convenience methods ───────────────────────────────────
    @property
    def is_active(self) -> bool:
        return self.stage in ('planning', 'generated', 'refining')

    @property
    def is_locked(self) -> bool:
        """True if a turn is currently being processed."""
        if not self.locked_until:
            return False
        from django.utils import timezone
        return self.locked_until > timezone.now()

    def can_send_another_turn(self) -> tuple[bool, str]:
        """Cost & turn limits per session — returns (allowed, reason)."""
        from django.conf import settings as _s
        max_turns = int(getattr(_s, 'DESIGN_CHAT_MAX_TURNS', 30))
        max_images = int(getattr(_s, 'DESIGN_CHAT_MAX_IMAGES', 8))
        if self.turn_count >= max_turns:
            return False, 'max_turns_reached'
        if self.image_count >= max_images:
            return False, 'max_images_reached'
        if self.is_locked:
            return False, 'in_flight'
        if self.stage in ('finalized', 'abandoned'):
            return False, 'closed'
        return True, ''


class DesignConversationTurn(models.Model):
    """💬 تيرن واحد في محادثة تصميم — رسالة من user أو assistant.

    Carries the classified intent (so analytics can see drop-off points
    where the classifier mis-routed) and the design snapshot at this turn
    (so 'undo' reverts to a prior known-good image).
    """
    ROLE_CHOICES = (
        ('user',      _('العميل')),
        ('assistant', _('المصمم AI')),
        ('system',    _('النظام')),
    )
    INTENT_CHOICES = (
        ('chat',      _('دردشة (بدون توليد)')),
        ('generate',  _('توليد صورة جديدة')),
        ('refine',    _('تعديل الصورة الحالية')),
        ('undo',      _('رجوع للحالة السابقة')),
        ('finalize',  _('اعتماد التصميم')),
        ('unknown',   _('غير محدد')),
    )

    conversation = models.ForeignKey(
        DesignConversation, on_delete=models.CASCADE,
        related_name='turns',
        verbose_name=_("المحادثة"),
    )
    turn_index = models.PositiveSmallIntegerField(
        verbose_name=_("ترتيب التيرن"),
        help_text=_("بداية من 1 — يـ increment على كل user message"),
    )

    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField(verbose_name=_("محتوى الرسالة"))

    intent = models.CharField(
        max_length=12, choices=INTENT_CHOICES, default='unknown',
        db_index=True,
        verbose_name=_("النية المصنّفة"),
    )
    intent_confidence = models.FloatField(
        default=0.0,
        verbose_name=_("ثقة المصنِّف (0-1)"),
    )

    # Snapshot of the design at this turn — enables 'undo' to revert
    # current_design to a prior known-good state without re-rendering.
    # NULL for chat-only turns that didn't produce an image.
    design_snapshot = models.ForeignKey(
        CustomerDesign, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='conversation_snapshots',
        verbose_name=_("صورة التيرن"),
    )

    # 💰 Per-turn cost attribution — sum to get session total
    token_cost_credits = models.DecimalField(
        max_digits=10, decimal_places=4, default=0,
        verbose_name=_("تكلفة الـ tokens"),
    )
    image_cost_credits = models.DecimalField(
        max_digits=10, decimal_places=4, default=0,
        verbose_name=_("تكلفة الصورة"),
    )
    engine_used = models.CharField(
        max_length=20, blank=True,
        verbose_name=_("المحرك المستخدم"),
        help_text=_("flux | ideogram | kontext | llm_only"),
    )

    # 🧠 Patch applied to accumulated_context this turn — lets us
    # replay the conversation state at any point (for undo / debugging).
    context_patch = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("التعديل على السياق هذا التيرن"),
    )

    # Error capture for failed turns (FLUX timeout, classifier ambiguous, ...)
    error_code = models.CharField(max_length=50, blank=True)
    error_detail = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("💬 تيرن محادثة تصميم")
        verbose_name_plural = _("💬 تيرنات محادثات التصاميم")
        ordering = ['conversation', 'turn_index', 'created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['conversation', 'turn_index', 'role'],
                name='uniq_conv_turn_role',
            ),
        ]
        indexes = [
            models.Index(fields=['conversation', 'turn_index']),
            models.Index(fields=['intent', '-created_at']),
        ]

    def __str__(self) -> str:
        snippet = (self.content or '')[:60].replace('\n', ' ')
        return f'[{self.conversation_id}#{self.turn_index}] {self.role}: {snippet}'


class DesignPrintRequest(models.Model):
    """
    🖨️ طلب طباعة تصميم — العميل عجبه التصميم وعاوز يطبعه.
    يظهر في Super Admin للمراجعة: إما نرد بسعر أو ننزله في الماركت بليس.
    """
    STATUS_CHOICES = (
        ('pending', _('في انتظار المراجعة')),
        ('quoted', _('تم إرسال عرض سعر')),
        ('marketplace', _('تم نشره في السوق')),
        ('accepted', _('العميل قبل العرض')),
        ('in_production', _('قيد الطباعة')),
        ('shipped', _('تم الشحن')),
        ('delivered', _('تم التسليم')),
        ('cancelled', _('ملغي')),
    )
    PRODUCT_TYPE_CHOICES = (
        ('tshirt', _('تيشرت')),
        ('business_card', _('كارت بزنس')),
        ('flyer', _('فلاير')),
        ('poster', _('بوستر')),
        ('banner', _('بنر / ستاند')),
        ('mug', _('ماج')),
        ('sticker', _('ستيكر')),
        ('packaging', _('تغليف / علبة')),
        ('pen', _('قلم / هدايا')),
        ('notebook', _('نوت بوك / أجندة')),
        ('menu', _('منيو')),
        ('invitation', _('كارت دعوة')),
        ('other', _('أخرى')),
    )

    request_code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    design = models.ForeignKey(CustomerDesign, on_delete=models.CASCADE, related_name='print_requests')
    customer = models.ForeignKey(MarketplaceCustomer, on_delete=models.CASCADE, related_name='print_requests')

    # تفاصيل الطباعة
    product_type = models.CharField(max_length=20, choices=PRODUCT_TYPE_CHOICES, verbose_name=_("نوع المنتج"))
    quantity = models.PositiveIntegerField(default=1, verbose_name=_("الكمية"))
    width_cm = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, verbose_name=_("العرض (سم)"))
    height_cm = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, verbose_name=_("الطول (سم)"))
    paper_type = models.CharField(max_length=100, blank=True, verbose_name=_("نوع الورق / الخامة"))
    color_mode = models.CharField(max_length=20, default='full_color', choices=(
        ('full_color', _('ألوان كاملة')),
        ('bw', _('أبيض وأسود')),
        ('spot', _('ألوان محددة')),
    ), verbose_name=_("الألوان"))
    finishing = models.CharField(max_length=100, blank=True, verbose_name=_("التشطيب"),
        help_text=_("سوفت تاتش، لامع، مطفي، UV، إلخ"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات إضافية"))

    # العنوان والتوصيل
    delivery_address = models.TextField(blank=True, verbose_name=_("عنوان التوصيل"))
    delivery_phone = models.CharField(max_length=20, blank=True, verbose_name=_("رقم التواصل"))

    # الحالة والسعر
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    quoted_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name=_("السعر المقترح من المنصة"))
    admin_notes = models.TextField(blank=True, verbose_name=_("ملاحظات الإدارة"))

    # 📄 Print spec PDF — يتم توليده تلقائياً عند الإرسال للمطبعة
    print_spec_pdf = models.FileField(
        upload_to='marketplace/print_specs/', null=True, blank=True,
        verbose_name=_("ملف PDF بمواصفات الطباعة"),
        help_text=_("PDF بكل تفاصيل الطباعة (نص + لون + خامة + مقاسات) — يتولّد تلقائياً"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("طلب طباعة تصميم")
        verbose_name_plural = _("🖨️ طلبات طباعة تصاميم")
        ordering = ['-created_at']

    def __str__(self):
        return f"PRINT-{str(self.request_code)[:8]} | {self.customer.full_name} | {self.get_product_type_display()}"


class DesignPromptLog(models.Model):
    """
    🧠 سجل تعلم البرومبت — يحفظ كل برومبت ناجح مع التقييم.
    يُستخدم لتحسين جودة التوليد مع الوقت: البرومبتات الحاصلة على
    تقييم عالي تُستخدم كأمثلة للبرومبتات الجديدة (few-shot learning).
    """
    category = models.CharField(max_length=30, db_index=True, verbose_name=_("التصنيف"))
    user_prompt = models.TextField(verbose_name=_("البرومبت الأصلي من العميل"))
    engineered_prompt = models.TextField(verbose_name=_("البرومبت المحسّن للـ AI"))
    model_used = models.CharField(max_length=50, blank=True)
    size_used = models.CharField(max_length=30, blank=True)
    customer_rating = models.IntegerField(null=True, blank=True,
        verbose_name=_("تقييم العميل (1-5)"))
    design = models.ForeignKey(CustomerDesign, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='prompt_logs')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("سجل برومبت")
        verbose_name_plural = _("🧠 سجلات تعلم البرومبت")
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['category', '-customer_rating']),
        ]

    def __str__(self):
        return f"{self.category} — {self.user_prompt[:50]}"

    @classmethod
    def get_best_examples(cls, category, limit=3):
        """جلب أفضل البرومبتات السابقة لنفس التصنيف (تقييم 4+) لاستخدامها كـ few-shot examples."""
        return list(
            cls.objects.filter(
                category=category,
                customer_rating__gte=4,
            ).order_by('-customer_rating', '-created_at')
            .values_list('user_prompt', 'engineered_prompt')[:limit]
        )


class CustomerBrandProfile(models.Model):
    """🎨 Brand Memory — هوية البراند المحفوظة للعميل.

    العميل يـ setup ملف البراند مرة واحدة (لوجو + ألوان + أسلوب)، ومن ساعتها
    كل تصميم يقوم بتوليده يـ inject الـ brand identity تلقائياً بدون إعادة كتابة.

    Smart Merge Logic:
      • الـ explicit selections في الـ form بتـ override الـ brand defaults.
      • الـ brand defaults بتدخل بس في الـ slots اللي العميل سايبها فاضية.
      • الـ logo image بيدخل reference_images تلقائياً (للـ vision analysis).
    """
    INDUSTRY_CHOICES = (
        ('fashion', _('موضة / ملابس')),
        ('food', _('مطاعم / طعام')),
        ('tech', _('تكنولوجيا')),
        ('beauty', _('تجميل')),
        ('jewelry', _('مجوهرات')),
        ('home', _('أثاث / ديكور')),
        ('education', _('تعليم')),
        ('healthcare', _('صحة / طب')),
        ('automotive', _('سيارات')),
        ('real_estate', _('عقارات')),
        ('retail', _('تجزئة')),
        ('services', _('خدمات')),
        ('events', _('مناسبات')),
        ('agency', _('وكالة / استشارات')),
        ('other', _('أخرى')),
    )

    AESTHETIC_CHOICES = (
        ('modern_minimal', _('عصري بسيط')),
        ('luxury_elegant', _('فاخر أنيق')),
        ('bold_playful', _('جريء مرح')),
        ('classic_traditional', _('كلاسيكي تراثي')),
        ('natural_organic', _('طبيعي عضوي')),
        ('tech_futuristic', _('تقني مستقبلي')),
        ('artisan_handcrafted', _('حرفي صناعة يدوية')),
        ('corporate_professional', _('شركاتي محترف')),
    )

    TONE_CHOICES = (
        ('formal', _('رسمي')),
        ('casual', _('غير رسمي / صديق')),
        ('playful', _('مرح / فكاهي')),
        ('authoritative', _('واثق / مرجعي')),
        ('warm', _('دافئ / إنساني')),
        ('luxurious', _('فاخر / حصري')),
    )

    FONT_STYLE_CHOICES = (
        ('modern_sans', 'Modern Sans-serif'),
        ('classic_serif', 'Classic Serif'),
        ('geometric', 'Geometric Sans'),
        ('elegant_script', 'Elegant Script'),
        ('bold_display', 'Bold Display'),
        ('arabic_naskh', 'خط النسخ التقليدي'),
        ('arabic_kufi', 'خط كوفي عصري'),
        ('arabic_diwani', 'خط ديواني فاخر'),
        ('arabic_modern', 'خط عربي عصري'),
    )

    customer = models.OneToOneField(
        MarketplaceCustomer, on_delete=models.CASCADE,
        related_name='brand_profile',
        verbose_name=_("العميل"),
    )

    # Identity
    brand_name = models.CharField(max_length=120, verbose_name=_("اسم البراند"))
    brand_name_en = models.CharField(max_length=120, blank=True,
        verbose_name=_("الاسم بالإنجليزي (اختياري)"))
    tagline = models.CharField(max_length=200, blank=True,
        verbose_name=_("الشعار / السلوجان"),
        help_text=_("جملة قصيرة بتلخص رسالة البراند"))

    # Visual identity — colors
    primary_color = models.CharField(max_length=9, default='#7c3aed',
        verbose_name=_("اللون الرئيسي"),
        help_text=_("لون البراند الأساسي — هيظهر في كل تصميم"))
    secondary_color = models.CharField(max_length=9, default='#1e293b',
        verbose_name=_("اللون الثانوي"))
    accent_color = models.CharField(max_length=9, blank=True, default='',
        verbose_name=_("لون التمييز (اختياري)"))

    # Visual identity — logo
    logo_image = models.ImageField(
        upload_to='brand_profiles/logos/%Y/%m/',
        blank=True, null=True,
        verbose_name=_("اللوجو"),
        help_text=_("هيتستخدم كـ reference في كل تصميم تلقائياً"),
    )
    logo_alt_image = models.ImageField(
        upload_to='brand_profiles/logos/%Y/%m/',
        blank=True, null=True,
        verbose_name=_("لوجو بديل (لون مختلف / monochrome)"),
    )

    # Brand voice
    industry = models.CharField(
        max_length=20, choices=INDUSTRY_CHOICES, default='other',
        verbose_name=_("المجال"),
    )
    aesthetic = models.CharField(
        max_length=30, choices=AESTHETIC_CHOICES, default='modern_minimal',
        verbose_name=_("الأسلوب البصري"),
    )
    tone = models.CharField(
        max_length=20, choices=TONE_CHOICES, default='warm',
        verbose_name=_("نبرة البراند"),
    )
    arabic_font = models.CharField(
        max_length=30, choices=FONT_STYLE_CHOICES, default='arabic_modern',
        verbose_name=_("الخط العربي المفضل"),
    )
    english_font = models.CharField(
        max_length=30, choices=FONT_STYLE_CHOICES, default='modern_sans',
        verbose_name=_("الخط الإنجليزي المفضل"),
    )

    # Free-form style notes for the LLM
    style_notes = models.TextField(
        blank=True, max_length=500,
        verbose_name=_("ملاحظات أسلوب إضافية"),
        help_text=_("أي تفاصيل عن أسلوب البراند بتحب الـ AI يتبعها — مثلاً 'استخدم زخارف إسلامية' أو 'تجنب الزهور'"),
    )

    # Control
    is_active = models.BooleanField(default=True, db_index=True,
        verbose_name=_("نشط — يطبّق تلقائياً"),
        help_text=_("لو معطّل، التصميمات الجديدة مش هتاخد ملف البراند تلقائياً"))
    auto_inject_logo = models.BooleanField(default=True,
        verbose_name=_("ضع اللوجو تلقائياً في كل تصميم"))
    auto_inject_colors = models.BooleanField(default=True,
        verbose_name=_("استخدم ألوان البراند تلقائياً"))

    # Stats
    designs_with_brand = models.PositiveIntegerField(default=0,
        verbose_name=_("عدد التصميمات اللي استخدمت ملف البراند"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("ملف براند العميل")
        verbose_name_plural = _("🎨 ملفات البراند")

    def __str__(self):
        return f"{self.brand_name} ({self.customer})"

    @property
    def has_logo(self) -> bool:
        return bool(self.logo_image and self.logo_image.name)

    def as_brand_context(self) -> dict:
        """يرجع dict شكلها يندمج في selections + يـ describe الـ aesthetic
        للـ mega_prompt. الـ design_engine بتـ merge ده مع الـ explicit
        selections (explicit يفوز دايماً)."""
        ctx = {
            'brand_name': self.brand_name,
            'industry': self.get_industry_display(),
            'aesthetic': self.get_aesthetic_display(),
            'tone': self.get_tone_display(),
        }
        if self.auto_inject_colors:
            ctx['primary_color'] = self.primary_color
            ctx['secondary_color'] = self.secondary_color
            if self.accent_color:
                ctx['accent_color'] = self.accent_color
        if self.tagline:
            ctx['tagline'] = self.tagline
        if self.brand_name_en:
            ctx['brand_name_en'] = self.brand_name_en
        if self.style_notes:
            ctx['style_notes'] = self.style_notes
        ctx['arabic_font_pref'] = self.get_arabic_font_display()
        ctx['english_font_pref'] = self.get_english_font_display()
        return ctx


class AIPromptLearningLog(models.Model):
    """🌀 Data Flywheel — كل تفاعل توليد بيتسجل لبناء fine-tuning dataset مع الوقت.

    بنحفظ الـ raw input + الـ domain اللي اللي الـ LLM حدده + الـ dynamic schema
    + اختيارات المستخدم + الـ mega prompt + الصورة + boolean is_successful (feedback).
    """
    AUDIENCE_CHOICES = (
        ('customer', _('عميل سوق')),
        ('tenant', _('شركة / تاجر')),
        ('anonymous', _('زائر')),
    )

    audience = models.CharField(max_length=12, choices=AUDIENCE_CHOICES, default='anonymous', db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    tenant = models.ForeignKey(
        Client, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ai_learning_logs',
    )
    customer = models.ForeignKey(
        MarketplaceCustomer, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ai_learning_logs',
    )

    raw_input = models.TextField(verbose_name=_("الفكرة الخام كما كتبها المستخدم"))
    detected_domain = models.CharField(max_length=80, blank=True, db_index=True,
        verbose_name=_("المجال اللي حدده الـ LLM"))
    dynamic_schema = models.JSONField(default=dict, blank=True,
        verbose_name=_("الـ JSON Schema الديناميكي"))
    selections = models.JSONField(default=dict, blank=True,
        verbose_name=_("اختيارات المستخدم"))

    mega_prompt = models.TextField(blank=True, verbose_name=_("الـ Mega Prompt النهائي"))
    negative_prompt = models.TextField(blank=True)
    image_url = models.URLField(max_length=600, blank=True)
    image_size = models.CharField(max_length=20, blank=True)

    llm_model = models.CharField(max_length=80, blank=True)
    image_model = models.CharField(max_length=80, blank=True)

    is_successful = models.BooleanField(null=True, blank=True, db_index=True,
        verbose_name=_("هل التصميم ناجح؟ (feedback من المستخدم)"))
    feedback_at = models.DateTimeField(null=True, blank=True)

    # 🔍 Quality Gate (Vision-based verification)
    quality_score = models.IntegerField(null=True, blank=True, db_index=True,
        verbose_name=_("درجة الجودة (1-10) من Vision LLM"))
    quality_verdict = models.CharField(max_length=20, blank=True, db_index=True,
        verbose_name=_("حكم الجودة"),
        help_text="excellent | acceptable | needs_regen | critical_fail")
    quality_issues = models.JSONField(default=list, blank=True,
        verbose_name=_("المشاكل المكتشفة"))
    auto_regenerated = models.BooleanField(default=False, db_index=True,
        verbose_name=_("هل اتعاد توليده تلقائياً بسبب فشل Quality Gate؟"))
    presentation_category = models.CharField(max_length=20, blank=True, db_index=True,
        verbose_name=_("فئة العرض"),
        help_text="apparel | document | footwear | furniture | ...")
    detected_subtype = models.CharField(max_length=20, blank=True, db_index=True,
        verbose_name=_("الـ subtype داخل الفئة"),
        help_text="slipper | sneaker | table | laptop | ...")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("سجل تعلم AI")
        verbose_name_plural = _("🌀 Data Flywheel — سجلات تعلم الذكاء")
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['detected_domain', '-created_at']),
            models.Index(fields=['is_successful', '-created_at']),
            models.Index(fields=['presentation_category', 'detected_subtype']),
            models.Index(fields=['quality_verdict', '-created_at']),
        ]

    def __str__(self):
        return f"AIL-{self.pk} | {self.detected_domain or '?'} | {self.raw_input[:40]}"


def seed_default_design_packages():
    """يستدعى من management command أو migration لتأسيس الباقات.

    باقات منفصلة للعملاء الأفراد والمصممين/الشركات.
    - العميل المجاني: تصميم واحد مجاني بدون إعادة محاولة
    - أول باقة عملاء: إعادة محاولة واحدة
    - ثاني وثالث باقة: إعادتين
    - باقات المصممين: إعادتين
    """
    # إلغاء تفعيل الباقات القديمة
    DesignPackage.objects.filter(slug__in=['starter', 'pro', 'business', 'studio', 'single']).update(is_active=False)

    # === باقات العملاء الأفراد ===
    customer_packages = [
        {'slug': 'cust_2', 'name_ar': 'باقة 2 تصميم', 'designs_count': 2,
         'designer_designs_count': 0, 'price_egp': Decimal('99.00'),
         'target_audience': 'customer',
         'free_regenerations_per_design': 1,
         'icon_emoji': '✨', 'accent_color': '#fbbf24', 'sort_order': 1,
         'allows_whatsapp_delivery': True,
         'description_html': 'ابدأ مشروعك. تصميمين بجودة HD + إعادة محاولة مجانية.'},
        {'slug': 'cust_4', 'name_ar': 'باقة 4 تصاميم', 'designs_count': 4,
         'designer_designs_count': 0, 'price_egp': Decimal('189.00'),
         'target_audience': 'customer',
         'free_regenerations_per_design': 2, 'is_featured': True,
         'badge_text': 'الأوفر',
         'icon_emoji': '🔥', 'accent_color': '#ec4899', 'sort_order': 2,
         'allows_whatsapp_delivery': True, 'allows_logo_upload': True,
         'description_html': 'أفضل قيمة! لوجو + كارت + سوشيال + فلاير + إعادتين مجانية.'},
        {'slug': 'cust_8', 'name_ar': 'باقة 8 تصاميم', 'designs_count': 8,
         'designer_designs_count': 0, 'price_egp': Decimal('369.00'),
         'target_audience': 'customer',
         'free_regenerations_per_design': 2,
         'icon_emoji': '💎', 'accent_color': '#8b5cf6', 'sort_order': 3,
         'allows_whatsapp_delivery': True, 'allows_logo_upload': True,
         'allows_watermark': True,
         'description_html': 'هوية بصرية كاملة. لوجو + كروت + سوشيال + علامة مائية.'},
    ]
    # === باقات المصممين والشركات ===
    designer_packages = [
        {'slug': 'des_15', 'name_ar': 'باقة 15 تصميم', 'designs_count': 15,
         'designer_designs_count': 0, 'price_egp': Decimal('599.00'),
         'target_audience': 'designer',
         'free_regenerations_per_design': 2,
         'icon_emoji': '🎨', 'accent_color': '#06b6d4', 'sort_order': 10,
         'allows_whatsapp_delivery': True, 'allows_logo_upload': True,
         'description_html': 'للمصمم المبتدئ. 15 تصميم بجودة احترافية + إعادتين مجانية.'},
        {'slug': 'des_25', 'name_ar': 'باقة 25 تصميم', 'designs_count': 25,
         'designer_designs_count': 0, 'price_egp': Decimal('949.00'),
         'target_audience': 'designer',
         'free_regenerations_per_design': 2, 'is_featured': True,
         'badge_text': 'الأكثر طلباً',
         'icon_emoji': '🚀', 'accent_color': '#ec4899', 'sort_order': 11,
         'allows_whatsapp_delivery': True, 'allows_logo_upload': True,
         'allows_watermark': True,
         'description_html': 'الأنسب للمصمم المحترف. جودة فائقة + توصيل واتساب + علامة مائية.'},
        {'slug': 'des_50', 'name_ar': 'باقة 50 تصميم', 'designs_count': 50,
         'designer_designs_count': 0, 'price_egp': Decimal('1849.00'),
         'target_audience': 'designer',
         'free_regenerations_per_design': 2,
         'icon_emoji': '⚡', 'accent_color': '#facc15', 'sort_order': 12,
         'allows_whatsapp_delivery': True, 'allows_logo_upload': True,
         'allows_watermark': True, 'allows_source_files': True,
         'description_html': 'للاستوديوهات. ملفات مصدر + علامة مائية + جودة فائقة.'},
        {'slug': 'des_100', 'name_ar': 'باقة 100 تصميم', 'designs_count': 100,
         'designer_designs_count': 0, 'price_egp': Decimal('3249.00'),
         'target_audience': 'designer',
         'free_regenerations_per_design': 2,
         'icon_emoji': '👑', 'accent_color': '#8b5cf6', 'sort_order': 13,
         'allows_whatsapp_delivery': True, 'allows_logo_upload': True,
         'allows_watermark': True, 'allows_source_files': True,
         'quality_level': 'ultra', 'resolution_max': '4096x4096',
         'badge_text': 'أقوى باقة',
         'description_html': 'للوكالات الكبرى. كل المزايا + ملفات مصدر + أعلى دقة 4K.'},
    ]
    for d in customer_packages + designer_packages:
        DesignPackage.objects.update_or_create(slug=d['slug'], defaults=d)
