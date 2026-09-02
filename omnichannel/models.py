"""
Omnichannel AI Automation — data model (Deliverable 1).

These models live in the PUBLIC schema (the app is registered in SHARED_APPS)
for one hard architectural reason: Meta delivers every webhook to a single
public URL, *before* any tenant subdomain / schema is known. The routing table
that maps an inbound WhatsApp `phone_number_id` or Messenger `page_id` back to
the owning tenant therefore has to be queryable without a schema context.

The tenant's operational data (inventory, prices) stays in the tenant schema and
is read inside the Celery task via `schema_context(client.schema_name)`.

BYOK: every Meta credential here belongs to the *tenant's own* Meta app, so all
WhatsApp conversation costs are billed to the tenant by Meta directly, never to
Mouss Tec.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from clients.models import Client

from . import crypto


class TenantChannelConfig(models.Model):
    """
    Per-tenant social + AI integration settings for the Omnichannel add-on.

    One row per tenant (OneToOne to clients.Client). Secrets are stored as
    Fernet ciphertext and exposed through plaintext properties, so they are
    never written to the DB, logs, or the admin list in the clear.
    """

    # ── Provider selection ────────────────────────────────────────────
    class LLMProvider(models.TextChoices):
        PLATFORM = "platform", _("Mouss Tec (Gemini) — مزوّد المنصة")
        OPENAI = "openai", _("OpenAI (مفتاح الشركة)")
        GEMINI = "gemini", _("Google Gemini (مفتاح الشركة)")

    tenant = models.OneToOneField(
        Client,
        on_delete=models.CASCADE,
        related_name="omnichannel_config",
        verbose_name=_("الشركة (المستأجر)"),
    )

    # ── Subscription gate ─────────────────────────────────────────────
    #   This is a SEPARATE, independently-billed add-on (250 EGP/month by
    #   default). A tenant can self-subscribe from their wallet, or the
    #   super-admin can grant/extend/revoke it manually. Activation state is:
    #     is_subscription_active=True + subscription_expires_at=None   → lifetime
    #     is_subscription_active=True + future expiry                  → timed
    #     is_subscription_active=False  (or past expiry)               → inactive
    MONTHLY_PRICE = Decimal("250.00")

    is_subscription_active = models.BooleanField(
        default=False,
        verbose_name=_("اشتراك الأتمتة فعّال؟"),
        help_text=_("يتحكم في تشغيل/إيقاف الردود الآلية بالكامل لهذا المستأجر."),
    )
    subscription_started_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("تاريخ بدء الاشتراك"),
    )
    subscription_expires_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("تاريخ انتهاء الاشتراك"),
        help_text=_("اتركه فارغاً مع تفعيل الاشتراك للوصول مدى الحياة."),
    )
    ai_enabled = models.BooleanField(
        default=True,
        verbose_name=_("الرد الآلي بالذكاء الاصطناعي مفعّل؟"),
        help_text=_("أوقفه مؤقتاً لتسليم المحادثات لموظف بشري دون إلغاء الاشتراك."),
    )

    # ── Meta / WhatsApp Business Cloud API credentials (BYOK) ─────────
    #   These belong to the TENANT's own Meta Developer App.
    _meta_access_token = models.TextField(
        blank=True, default="", db_column="meta_access_token_enc",
        verbose_name=_("Meta Access Token (مشفّر)"),
    )
    whatsapp_phone_number_id = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        verbose_name=_("WhatsApp Phone Number ID"),
        help_text=_("معرّف رقم واتساب المُستقبِل — يُستخدم لتوجيه الرسائل الواردة."),
    )
    whatsapp_business_account_id = models.CharField(
        max_length=64, blank=True, default="",
        verbose_name=_("WhatsApp Business Account ID (WABA)"),
    )
    facebook_page_id = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        verbose_name=_("Facebook Page ID"),
        help_text=_("معرّف صفحة ماسنجر المُستقبِلة — يُستخدم لتوجيه رسائل ماسنجر."),
    )
    _app_secret = models.TextField(
        blank=True, default="", db_column="app_secret_enc",
        verbose_name=_("Meta App Secret (مشفّر)"),
        help_text=_("يُستخدم للتحقق من توقيع X-Hub-Signature-256 لكل Webhook."),
    )
    webhook_verify_token = models.CharField(
        max_length=128, blank=True, default="",
        verbose_name=_("Webhook Verify Token"),
        help_text=_("رمز التحقق الذي تُدخله الشركة في إعدادات Webhook داخل تطبيق Meta."),
    )

    # ── Channel enable flags ──────────────────────────────────────────
    whatsapp_enabled = models.BooleanField(default=True, verbose_name=_("قناة واتساب مفعّلة؟"))
    messenger_enabled = models.BooleanField(default=True, verbose_name=_("قناة ماسنجر مفعّلة؟"))

    # ── Custom AI behaviour ───────────────────────────────────────────
    business_display_name = models.CharField(
        max_length=120, blank=True, default="",
        verbose_name=_("اسم النشاط كما يظهر للعملاء"),
    )
    tone_of_voice = models.CharField(
        max_length=255, blank=True,
        default=_("ودود، محترف، وموجز — يخاطب العميل باللهجة نفسها التي راسل بها."),
        verbose_name=_("نبرة الحوار (Tone of Voice)"),
    )
    discount_policy = models.TextField(
        blank=True, default="",
        verbose_name=_("سياسة الخصومات"),
        help_text=_("مثال: خصم 5% للطلبات فوق 5000 ج.م — يلتزم بها المساعد حرفياً."),
    )
    custom_instructions = models.TextField(
        blank=True, default="",
        verbose_name=_("تعليمات مخصّصة إضافية"),
        help_text=_("أي قواعد أخرى للرد (ساعات العمل، مناطق التوصيل، عبارات ممنوعة...)."),
    )
    fallback_message = models.TextField(
        blank=True,
        default=_("شكراً لتواصلك معنا 🙏 سيتم تحويلك لأحد موظفي خدمة العملاء للرد على استفسارك."),
        verbose_name=_("رسالة التحويل لموظف بشري"),
        help_text=_("تُرسَل عند تعذّر توليد رد آلي موثوق."),
    )
    max_reply_chars = models.PositiveIntegerField(
        default=900, verbose_name=_("أقصى طول للرد (حرف)"),
    )

    # ── BYO LLM (optional) ────────────────────────────────────────────
    llm_provider = models.CharField(
        max_length=16, choices=LLMProvider.choices, default=LLMProvider.PLATFORM,
        verbose_name=_("مزوّد الذكاء الاصطناعي"),
    )
    _llm_api_key = models.TextField(
        blank=True, default="", db_column="llm_api_key_enc",
        verbose_name=_("مفتاح LLM (مشفّر)"),
    )
    llm_model = models.CharField(
        max_length=80, blank=True, default="",
        verbose_name=_("موديل الـ LLM"),
        help_text=_("مثال: gpt-4o-mini أو gemini-2.0-flash — اتركه فارغاً للافتراضي."),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("إعدادات الأتمتة متعددة القنوات")
        verbose_name_plural = _("💬 إعدادات الأتمتة متعددة القنوات (Omnichannel)")

    def __str__(self) -> str:
        return f"OmnichannelConfig<{self.tenant.schema_name}>"

    # ── Encrypted-secret accessors ────────────────────────────────────
    @property
    def meta_access_token(self) -> str:
        return crypto.decrypt(self._meta_access_token)

    @meta_access_token.setter
    def meta_access_token(self, value: str) -> None:
        self._meta_access_token = crypto.encrypt(value or "")

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

    # ── Subscription lifecycle ────────────────────────────────────────
    @property
    def subscription_is_valid(self) -> bool:
        """True iff the paid add-on is currently live (active and not expired)."""
        if not self.is_subscription_active:
            return False
        if self.subscription_expires_at is None:
            return True  # lifetime grant
        return timezone.now() < self.subscription_expires_at

    @property
    def subscription_is_lifetime(self) -> bool:
        return self.is_subscription_active and self.subscription_expires_at is None

    @property
    def subscription_days_left(self):
        """Whole days remaining, None for lifetime, 0 if expired/inactive."""
        if not self.is_subscription_active:
            return 0
        if self.subscription_expires_at is None:
            return None
        delta = self.subscription_expires_at - timezone.now()
        return max(delta.days, 0)

    @property
    def subscription_state(self) -> str:
        """One of: lifetime | active | expired | inactive — for UI badges."""
        if self.subscription_is_lifetime:
            return "lifetime"
        if self.subscription_is_valid:
            return "active"
        if self.is_subscription_active and self.subscription_expires_at:
            return "expired"
        return "inactive"

    def grant_subscription(self, duration: timedelta | None = None, *, by_user=None):
        """Activate or extend the subscription.

        `duration` is added to the later of now() and the current expiry (so
        stacking months accumulates). `duration=None` means a lifetime grant.
        """
        self.is_subscription_active = True
        if self.subscription_started_at is None:
            self.subscription_started_at = timezone.now()
        if duration is None:
            self.subscription_expires_at = None
        else:
            base = self.subscription_expires_at if (
                self.subscription_expires_at
                and self.subscription_expires_at > timezone.now()
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
        """Are we allowed to auto-reply right now?"""
        return bool(
            self.subscription_is_valid
            and self.ai_enabled
            and self.meta_access_token
        )

    def has_whatsapp(self) -> bool:
        return bool(self.whatsapp_enabled and self.whatsapp_phone_number_id)

    def has_messenger(self) -> bool:
        return bool(self.messenger_enabled and self.facebook_page_id)


class ChannelMessageLog(models.Model):
    """
    Audit trail of every inbound message and outbound auto-reply (public schema).
    Kept lightweight — useful for debugging Meta delivery, tuning prompts, and
    giving the tenant a conversation history in the dashboard.
    """

    class Channel(models.TextChoices):
        WHATSAPP = "whatsapp", "WhatsApp"
        MESSENGER = "messenger", "Messenger"

    class Status(models.TextChoices):
        RECEIVED = "received", _("وردت")
        REPLIED = "replied", _("تم الرد")
        SKIPPED = "skipped", _("تم التجاوز")
        FAILED = "failed", _("فشل")

    tenant = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name="omnichannel_logs",
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    sender_id = models.CharField(max_length=128, db_index=True)
    inbound_text = models.TextField(blank=True, default="")
    outbound_text = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RECEIVED)
    error = models.TextField(blank=True, default="")
    meta_message_id = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("سجل رسالة قناة")
        verbose_name_plural = _("سجل رسائل القنوات")
        indexes = [
            models.Index(fields=["tenant", "-created_at"], name="omnichanne_tenant__6e9c8f_idx"),
        ]

    def __str__(self) -> str:
        return f"[{self.channel}] {self.sender_id} → {self.status}"
