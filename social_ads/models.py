"""
Social Studio — AI Marketing Autopilot (subscription add-on) · data model.

A self-driving social-media marketer for each tenant. It writes posts, designs
ad copy, schedules & publishes to Facebook + Instagram, then reads back the real
performance from Meta and **learns** which angles / times / tones work for this
specific business — feeding that memory into the next batch (the "يتعلم ويصحح"
loop the customer asked for).

Why the PUBLIC schema (SHARED_APPS), like `omnichannel`:
  • The publisher / insight-sync / learning Celery beat jobs must sweep *due*
    work across every tenant in a single query, before any schema is active.
  • BYOK: every Meta credential here belongs to the tenant's own Meta app / ad
    account, so all ad spend is billed to the tenant by Meta directly — never to
    Mouss Tec. Mouss Tec only bills the flat monthly add-on fee (250 EGP).

Secrets (page token, app secret, BYO LLM key) are stored as Fernet ciphertext
using the same KEK helper as the omnichannel add-on, so operators manage a single
class of key.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from clients.models import Client

# Reuse the omnichannel encryption helper — one KEK for all tenant secrets.
from omnichannel import crypto


# =====================================================================
# 1. Per-tenant configuration + subscription gate
# =====================================================================
class SocialAdsConfig(models.Model):
    """Per-tenant Social Studio settings, brand profile, and subscription state.

    One row per tenant (OneToOne to clients.Client). Mirrors the omnichannel
    add-on's subscription lifecycle so ops, billing, and the tenant wallet flow
    behave identically.
    """

    class LLMProvider(models.TextChoices):
        PLATFORM = "platform", _("Mouss Tec (Gemini) — مزوّد المنصة")
        OPENAI = "openai", _("OpenAI (مفتاح الشركة)")
        GEMINI = "gemini", _("Google Gemini (مفتاح الشركة)")

    class Autopilot(models.TextChoices):
        OFF = "off", _("إيقاف — أنا أراجع كل بوست يدوياً")
        SUGGEST = "suggest", _("اقتراح — يجهّز مسودات وأنا أعتمدها")
        FULL = "full", _("تلقائي كامل — يكتب ويجدول وينشر وحده")

    tenant = models.OneToOneField(
        Client,
        on_delete=models.CASCADE,
        related_name="social_ads_config",
        verbose_name=_("الشركة (المستأجر)"),
    )

    # ── Subscription gate (independent add-on, 250 EGP/month default) ──
    MONTHLY_PRICE = Decimal("250.00")  # EGP default (backward-compatible)

    @classmethod
    def price_for_country(cls, country: str = "EG") -> Decimal:
        """Region-aware monthly price in the region's own currency."""
        from django.conf import settings as _s
        if (country or "EG").upper() == "AE":
            return Decimal(str(getattr(_s, "SOCIAL_ADS_PRICE_AED", "25")))
        return Decimal(str(getattr(_s, "SOCIAL_ADS_PRICE_EGP", "250")))

    is_subscription_active = models.BooleanField(
        default=False,
        verbose_name=_("اشتراك استوديو التسويق فعّال؟"),
        help_text=_("يتحكم في تشغيل النشر التلقائي والإعلانات لهذا المستأجر."),
    )
    subscription_started_at = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ بدء الاشتراك"))
    subscription_expires_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("تاريخ انتهاء الاشتراك"),
        help_text=_("اتركه فارغاً مع تفعيل الاشتراك للوصول مدى الحياة."),
    )

    # ── Meta credentials (BYOK) ───────────────────────────────────────
    _page_access_token = models.TextField(
        blank=True, default="", db_column="page_access_token_enc",
        verbose_name=_("Page Access Token (مشفّر)"),
        help_text=_("توكن صفحة فيسبوك — يُستخدم للنشر وقراءة الإحصاءات."),
    )
    _app_secret = models.TextField(
        blank=True, default="", db_column="app_secret_enc",
        verbose_name=_("Meta App Secret (مشفّر)"),
    )
    facebook_page_id = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        verbose_name=_("Facebook Page ID"),
    )
    instagram_account_id = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        verbose_name=_("Instagram Business Account ID"),
        help_text=_("حساب إنستجرام الاحترافي المرتبط بالصفحة — للنشر على إنستجرام."),
    )
    ad_account_id = models.CharField(
        max_length=64, blank=True, default="",
        verbose_name=_("Meta Ad Account ID (act_...)"),
        help_text=_("حساب الإعلانات — تُخصم تكلفة الإعلانات من وسيلة الدفع المربوطة به لدى Meta."),
    )

    # ── Channel enable flags ──────────────────────────────────────────
    facebook_enabled = models.BooleanField(default=True, verbose_name=_("النشر على فيسبوك مفعّل؟"))
    instagram_enabled = models.BooleanField(default=True, verbose_name=_("النشر على إنستجرام مفعّل؟"))
    ads_enabled = models.BooleanField(
        default=False, verbose_name=_("إنشاء الحملات الإعلانية مفعّل؟"),
        help_text=_("عند التفعيل يستطيع البوت إنشاء حملات مدفوعة ضمن حدود الميزانية أدناه."),
    )

    # ── Brand / business profile (grounds the AI) ─────────────────────
    business_display_name = models.CharField(max_length=120, blank=True, default="", verbose_name=_("اسم النشاط التجاري"))
    industry = models.CharField(
        max_length=120, blank=True, default="",
        verbose_name=_("مجال النشاط"),
        help_text=_("مثال: قطع غيار سيارات، مطبعة، مطعم، عيادة أسنان..."),
    )
    products_services = models.TextField(
        blank=True, default="",
        verbose_name=_("المنتجات / الخدمات الرئيسية"),
        help_text=_("اكتب أهم ما تبيعه — يبني عليه البوت أفكار البوستات والإعلانات."),
    )
    target_audience = models.TextField(
        blank=True, default="",
        verbose_name=_("الجمهور المستهدف"),
        help_text=_("مثال: أصحاب السيارات في القاهرة، من 25 لـ 45 سنة، مهتمين بالصيانة."),
    )
    brand_tone = models.CharField(
        max_length=255, blank=True,
        default=_("عصري، ودود، وواثق — يلفت الانتباه ويشجّع على التفاعل."),
        verbose_name=_("نبرة العلامة التجارية"),
    )
    brand_keywords = models.CharField(
        max_length=255, blank=True, default="",
        verbose_name=_("كلمات مفتاحية / هاشتاجات مميّزة"),
        help_text=_("افصل بينها بفواصل — يعطيها البوت أولوية في الهاشتاجات."),
    )
    business_goals = models.CharField(
        max_length=255, blank=True,
        default=_("زيادة الوعي بالعلامة وجذب عملاء جدد."),
        verbose_name=_("أهداف التسويق"),
    )
    call_to_action = models.CharField(
        max_length=160, blank=True,
        default=_("تواصل معنا الآن عبر الرسائل أو الاتصال."),
        verbose_name=_("الدعوة لاتخاذ إجراء (CTA) الافتراضية"),
    )
    contact_phone = models.CharField(max_length=32, blank=True, default="", verbose_name=_("رقم التواصل في البوستات"))
    website_url = models.URLField(blank=True, default="", verbose_name=_("رابط الموقع / المتجر"))
    banned_words = models.CharField(
        max_length=255, blank=True, default="",
        verbose_name=_("عبارات ممنوعة"),
        help_text=_("كلمات لا يستخدمها البوت إطلاقاً — افصل بينها بفواصل."),
    )

    # ── Autopilot & cadence ───────────────────────────────────────────
    autopilot_mode = models.CharField(
        max_length=8, choices=Autopilot.choices, default=Autopilot.SUGGEST,
        verbose_name=_("وضع الطيار الآلي"),
    )
    posts_per_week = models.PositiveSmallIntegerField(
        default=5, verbose_name=_("عدد البوستات أسبوعياً"),
        help_text=_("كم بوست يجهّزه البوت ويجدوله كل أسبوع (1–21)."),
    )
    preferred_times = models.CharField(
        max_length=120, blank=True, default="11:00,19:00",
        verbose_name=_("أوقات النشر المفضّلة"),
        help_text=_("مواعيد باليوم (24h) مفصولة بفواصل — يبدأ البوت بها ثم يحسّنها بالتعلّم."),
    )
    default_language = models.CharField(
        max_length=8, default="ar", verbose_name=_("لغة المحتوى"),
        choices=[("ar", _("عربي")), ("en", _("إنجليزي")), ("mix", _("عربي + إنجليزي"))],
    )
    generate_images = models.BooleanField(
        default=True, verbose_name=_("توليد صور للبوستات؟"),
        help_text=_("يقترح البوت وصفاً بصرياً ويولّد صورة لكل بوست (حسب مزوّد الصور)."),
    )

    # ── Ad-spend guardrails ───────────────────────────────────────────
    monthly_ad_budget = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"),
        verbose_name=_("سقف الإنفاق الإعلاني الشهري"),
        help_text=_("لن يتجاوز البوت هذا الحد في الحملات المدفوعة (بعملة حساب إعلاناتك)."),
    )
    max_daily_ad_budget = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("50.00"),
        verbose_name=_("أقصى ميزانية يومية للحملة الواحدة"),
    )
    auto_optimize_ads = models.BooleanField(
        default=True, verbose_name=_("تحسين الحملات تلقائياً؟"),
        help_text=_("يعيد توزيع الميزانية نحو الإعلانات الأفضل أداءً ويوقف الضعيفة."),
    )

    # ── AI provider (BYO LLM optional) ────────────────────────────────
    llm_provider = models.CharField(
        max_length=16, choices=LLMProvider.choices, default=LLMProvider.PLATFORM,
        verbose_name=_("مزوّد الذكاء الاصطناعي"),
    )
    _llm_api_key = models.TextField(blank=True, default="", db_column="llm_api_key_enc", verbose_name=_("مفتاح LLM (مشفّر)"))
    llm_model = models.CharField(max_length=80, blank=True, default="", verbose_name=_("موديل الـ LLM"))

    # ── Notifications ─────────────────────────────────────────────────
    notify_email = models.EmailField(
        blank=True, default="", verbose_name=_("بريد التنبيهات"),
        help_text=_("يصله تقرير أسبوعي وتنبيه عند فشل نشر أو حاجة اعتماد مسودة."),
    )
    notify_on_publish = models.BooleanField(default=False, verbose_name=_("إشعار عند كل نشر؟"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("إعدادات استوديو التسويق")
        verbose_name_plural = _("📣 إعدادات استوديو التسويق (Social Studio)")

    def __str__(self) -> str:
        return f"SocialAdsConfig<{self.tenant.schema_name}>"

    # ── Encrypted-secret accessors ────────────────────────────────────
    @property
    def page_access_token(self) -> str:
        return crypto.decrypt(self._page_access_token)

    @page_access_token.setter
    def page_access_token(self, value: str) -> None:
        self._page_access_token = crypto.encrypt(value or "")

    @property
    def app_secret(self) -> str:
        return crypto.decrypt(self._app_secret)

    @app_secret.setter
    def app_secret(self, value: str) -> None:
        self._app_secret = crypto.encrypt(value or "")

    @property
    def llm_api_key(self) -> str:
        return crypto.decrypt(self._llm_api_key)

    @llm_api_key.setter
    def llm_api_key(self, value: str) -> None:
        self._llm_api_key = crypto.encrypt(value or "")

    # ── Subscription lifecycle (mirrors omnichannel) ──────────────────
    @property
    def subscription_is_valid(self) -> bool:
        if not self.is_subscription_active:
            return False
        if self.subscription_expires_at is None:
            return True
        return timezone.now() < self.subscription_expires_at

    @property
    def subscription_is_lifetime(self) -> bool:
        return self.is_subscription_active and self.subscription_expires_at is None

    @property
    def subscription_days_left(self):
        if not self.is_subscription_active:
            return 0
        if self.subscription_expires_at is None:
            return None
        return max((self.subscription_expires_at - timezone.now()).days, 0)

    @property
    def subscription_state(self) -> str:
        if self.subscription_is_lifetime:
            return "lifetime"
        if self.subscription_is_valid:
            return "active"
        if self.is_subscription_active and self.subscription_expires_at:
            return "expired"
        return "inactive"

    def grant_subscription(self, duration: timedelta | None = None, *, by_user=None):
        self.is_subscription_active = True
        if self.subscription_started_at is None:
            self.subscription_started_at = timezone.now()
        if duration is None:
            self.subscription_expires_at = None
        else:
            base = self.subscription_expires_at if (
                self.subscription_expires_at and self.subscription_expires_at > timezone.now()
            ) else timezone.now()
            self.subscription_expires_at = base + duration
        self.save(update_fields=[
            "is_subscription_active", "subscription_started_at",
            "subscription_expires_at", "updated_at",
        ])

    def revoke_subscription(self, *, by_user=None):
        self.is_subscription_active = False
        self.subscription_expires_at = timezone.now()
        self.save(update_fields=[
            "is_subscription_active", "subscription_expires_at", "updated_at",
        ])

    # ── Convenience ───────────────────────────────────────────────────
    @property
    def is_operational(self) -> bool:
        """Can we publish right now?"""
        return bool(self.subscription_is_valid and self.page_access_token and self.facebook_page_id)

    def has_facebook(self) -> bool:
        return bool(self.facebook_enabled and self.facebook_page_id and self.page_access_token)

    def has_instagram(self) -> bool:
        return bool(self.instagram_enabled and self.instagram_account_id and self.page_access_token)

    def can_run_ads(self) -> bool:
        return bool(self.ads_enabled and self.ad_account_id and self.page_access_token)

    def preferred_times_list(self) -> list[str]:
        out = []
        for chunk in (self.preferred_times or "").split(","):
            t = chunk.strip()
            if t:
                out.append(t)
        return out or ["11:00", "19:00"]

    def spend_this_month(self) -> Decimal:
        """Sum of ad spend booked this calendar month (from campaigns)."""
        start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        agg = self.campaigns.filter(created_at__gte=start).aggregate(s=models.Sum("spend"))
        return agg["s"] or Decimal("0.00")

    def remaining_ad_budget(self) -> Decimal:
        return max(self.monthly_ad_budget - self.spend_this_month(), Decimal("0.00"))


# =====================================================================
# 2. A single social post (draft → scheduled → published)
# =====================================================================
class SocialPost(models.Model):
    """One piece of content the bot writes, schedules, publishes, and measures."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("مسودة — بانتظار الاعتماد")
        SCHEDULED = "scheduled", _("مجدول")
        PUBLISHING = "publishing", _("جارٍ النشر")
        PUBLISHED = "published", _("منشور")
        FAILED = "failed", _("فشل النشر")
        CANCELLED = "cancelled", _("ملغى")

    class Platform(models.TextChoices):
        FACEBOOK = "facebook", "Facebook"
        INSTAGRAM = "instagram", "Instagram"
        BOTH = "both", _("فيسبوك + إنستجرام")

    class Source(models.TextChoices):
        AUTOPILOT = "autopilot", _("طيار آلي")
        MANUAL = "manual", _("يدوي")
        IMPORTED = "imported", _("مستورد من الصفحة")

    config = models.ForeignKey(SocialAdsConfig, on_delete=models.CASCADE, related_name="posts")
    tenant = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="social_posts", db_index=True)

    platform = models.CharField(max_length=12, choices=Platform.choices, default=Platform.BOTH)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.AUTOPILOT)

    # Content
    title = models.CharField(max_length=180, blank=True, default="", verbose_name=_("عنوان داخلي"))
    caption = models.TextField(verbose_name=_("نص البوست"))
    hashtags = models.CharField(max_length=400, blank=True, default="", verbose_name=_("الهاشتاجات"))
    image_prompt = models.TextField(blank=True, default="", verbose_name=_("وصف الصورة (Prompt)"))
    image_url = models.URLField(blank=True, default="", max_length=600, verbose_name=_("رابط الصورة"))

    # AI provenance — feeds the learning loop
    strategy_angle = models.CharField(
        max_length=80, blank=True, default="", db_index=True,
        verbose_name=_("زاوية المحتوى"),
        help_text=_("مثال: عرض_سعري، نصيحة، شهادة_عميل، خلف_الكواليس، سؤال_تفاعلي."),
    )
    ai_rationale = models.TextField(blank=True, default="", verbose_name=_("لماذا اقترح البوت هذا؟"))

    # Scheduling
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name=_("موعد النشر"))
    published_at = models.DateTimeField(null=True, blank=True, verbose_name=_("وقت النشر الفعلي"))
    approved_at = models.DateTimeField(null=True, blank=True)

    # Meta result ids
    fb_post_id = models.CharField(max_length=128, blank=True, default="")
    ig_media_id = models.CharField(max_length=128, blank=True, default="")
    permalink = models.URLField(blank=True, default="", max_length=600)
    error = models.TextField(blank=True, default="")
    publish_attempts = models.PositiveSmallIntegerField(default=0)

    # Latest cached performance (detailed history in PerformanceSnapshot)
    reach = models.PositiveIntegerField(default=0)
    impressions = models.PositiveIntegerField(default=0)
    likes = models.PositiveIntegerField(default=0)
    comments = models.PositiveIntegerField(default=0)
    shares = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)
    engagement_rate = models.FloatField(default=0.0, help_text=_("(تفاعلات / وصول) %"))
    insights_synced_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("بوست اجتماعي")
        verbose_name_plural = _("بوستات اجتماعية")
        indexes = [
            models.Index(fields=["status", "scheduled_at"], name="socialads_due_idx"),
            models.Index(fields=["tenant", "-created_at"], name="socialads_tenant_idx"),
        ]

    def __str__(self) -> str:
        return f"[{self.get_status_display()}] {self.title or self.caption[:40]}"

    @property
    def full_text(self) -> str:
        parts = [self.caption.strip()]
        if self.hashtags.strip():
            parts.append(self.hashtags.strip())
        return "\n\n".join(p for p in parts if p)

    @property
    def is_due(self) -> bool:
        return bool(
            self.status == self.Status.SCHEDULED
            and self.scheduled_at
            and self.scheduled_at <= timezone.now()
        )

    def recompute_engagement(self):
        base = self.reach or self.impressions
        interactions = self.likes + self.comments + self.shares + self.clicks
        self.engagement_rate = round((interactions / base) * 100, 2) if base else 0.0


# =====================================================================
# 3. A paid ad campaign
# =====================================================================
class AdCampaign(models.Model):
    """A Meta Marketing-API campaign the bot creates and optimizes."""

    class Objective(models.TextChoices):
        AWARENESS = "OUTCOME_AWARENESS", _("الوعي بالعلامة")
        TRAFFIC = "OUTCOME_TRAFFIC", _("زيارات / نقرات")
        ENGAGEMENT = "OUTCOME_ENGAGEMENT", _("تفاعل")
        LEADS = "OUTCOME_LEADS", _("جذب عملاء محتملين")
        SALES = "OUTCOME_SALES", _("مبيعات")

    class Status(models.TextChoices):
        DRAFT = "draft", _("مسودة")
        PENDING = "pending", _("بانتظار الاعتماد")
        ACTIVE = "active", _("نشطة")
        PAUSED = "paused", _("موقوفة")
        COMPLETED = "completed", _("مكتملة")
        FAILED = "failed", _("فشل الإنشاء")

    config = models.ForeignKey(SocialAdsConfig, on_delete=models.CASCADE, related_name="campaigns")
    tenant = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="ad_campaigns", db_index=True)
    post = models.ForeignKey(
        SocialPost, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="campaigns", help_text=_("البوست المُروَّج (إن وجد)."),
    )

    name = models.CharField(max_length=180, verbose_name=_("اسم الحملة"))
    objective = models.CharField(max_length=32, choices=Objective.choices, default=Objective.TRAFFIC)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    source = models.CharField(max_length=12, default="autopilot")

    # Targeting + copy (kept as structured text/JSON-ish for portability)
    primary_text = models.TextField(blank=True, default="", verbose_name=_("نص الإعلان"))
    headline = models.CharField(max_length=200, blank=True, default="", verbose_name=_("العنوان الرئيسي"))
    audience_spec = models.JSONField(default=dict, blank=True, verbose_name=_("مواصفات الجمهور"))
    daily_budget = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    duration_days = models.PositiveSmallIntegerField(default=7)

    # Meta object ids
    meta_campaign_id = models.CharField(max_length=64, blank=True, default="")
    meta_adset_id = models.CharField(max_length=64, blank=True, default="")
    meta_ad_id = models.CharField(max_length=64, blank=True, default="")
    error = models.TextField(blank=True, default="")

    # Performance (cached latest)
    spend = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    reach = models.PositiveIntegerField(default=0)
    impressions = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)
    ctr = models.FloatField(default=0.0)
    results = models.PositiveIntegerField(default=0, help_text=_("نتائج حسب الهدف (رسائل/زيارات/عملاء)."))
    cost_per_result = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    insights_synced_at = models.DateTimeField(null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("حملة إعلانية")
        verbose_name_plural = _("حملات إعلانية")

    def __str__(self) -> str:
        return f"[{self.get_status_display()}] {self.name}"

    def recompute_ctr(self):
        self.ctr = round((self.clicks / self.impressions) * 100, 2) if self.impressions else 0.0
        if self.results:
            self.cost_per_result = round(self.spend / self.results, 2)


# =====================================================================
# 4. Time-series performance snapshots (for the learning loop + charts)
# =====================================================================
class PerformanceSnapshot(models.Model):
    """A dated performance reading for a post or campaign.

    We keep history (not just the latest cached numbers on the object) so the
    strategist can compare angles/times over weeks and detect real trends.
    """

    class Kind(models.TextChoices):
        POST = "post", _("بوست")
        CAMPAIGN = "campaign", _("حملة")

    tenant = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="social_snapshots", db_index=True)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    post = models.ForeignKey(SocialPost, on_delete=models.CASCADE, null=True, blank=True, related_name="snapshots")
    campaign = models.ForeignKey(AdCampaign, on_delete=models.CASCADE, null=True, blank=True, related_name="snapshots")

    reach = models.PositiveIntegerField(default=0)
    impressions = models.PositiveIntegerField(default=0)
    interactions = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)
    spend = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    results = models.PositiveIntegerField(default=0)
    engagement_rate = models.FloatField(default=0.0)
    captured_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-captured_at"]
        verbose_name = _("لقطة أداء")
        verbose_name_plural = _("لقطات أداء")

    def __str__(self) -> str:
        return f"{self.kind}#{self.post_id or self.campaign_id} @ {self.captured_at:%Y-%m-%d}"


# =====================================================================
# 5. The learned strategy memory (the "يتعلم ويصحح" brain)
# =====================================================================
class StrategyMemory(models.Model):
    """Per-tenant learned marketing playbook.

    The nightly learning cycle reads recent PerformanceSnapshots, ranks which
    content angles / posting hours / hashtags actually drove engagement for THIS
    business, and writes the distilled findings here. The content generator then
    reads this memory so every new batch is better than the last.
    """

    config = models.OneToOneField(SocialAdsConfig, on_delete=models.CASCADE, related_name="memory")
    tenant = models.OneToOneField(Client, on_delete=models.CASCADE, related_name="social_memory")

    # Ranked JSON structures updated by the learning cycle.
    angle_scores = models.JSONField(default=dict, blank=True, verbose_name=_("أداء زوايا المحتوى"))
    best_hours = models.JSONField(default=list, blank=True, verbose_name=_("أفضل ساعات النشر"))
    top_hashtags = models.JSONField(default=list, blank=True, verbose_name=_("أفضل الهاشتاجات"))
    winning_examples = models.JSONField(default=list, blank=True, verbose_name=_("نماذج ناجحة"))

    # A short natural-language brief the LLM ingests as extra guidance.
    learned_brief = models.TextField(blank=True, default="", verbose_name=_("خلاصة ما تعلّمه البوت"))

    posts_analyzed = models.PositiveIntegerField(default=0)
    avg_engagement_rate = models.FloatField(default=0.0)
    last_learned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("ذاكرة استراتيجية")
        verbose_name_plural = _("ذواكر استراتيجية")

    def __str__(self) -> str:
        return f"StrategyMemory<{self.tenant.schema_name}>"

    def best_angles(self, top_n: int = 3) -> list[str]:
        """Return the highest-scoring content angles."""
        scores = self.angle_scores or {}
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return [angle for angle, _score in ranked[:top_n]]
