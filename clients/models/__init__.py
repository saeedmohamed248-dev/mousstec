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


# 🔀 Domain submodules — re-exported below so external imports
# (`from clients.models import X`) keep working unchanged.
from .tenancy import *  # noqa: F401, F403

from .marketplace_c2c import *  # noqa: F401, F403
from .marketplace_c2c import _verification_upload_path  # noqa: F401 — referenced by historical migrations
from .design_store import *  # noqa: F401, F403
from .marketplace_b2b import *  # noqa: F401, F403
from .marketplace_b2b import _validate_warranty_days  # noqa: F401 — referenced by historical migrations


# =====================================================================
# 📊 Visitor & Activity Tracking (Super Admin Analytics)
# =====================================================================

class VisitorLog(models.Model):
    """
    سجل زوار المنصة — يُستخدم في لوحة السوبر أدمن.
    يُسجل كل طلب HTTP مع البيانات الجغرافية والجهاز.
    Shared app → جدول واحد في الـ public schema.
    """
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    path = models.CharField(max_length=500)
    method = models.CharField(max_length=10, default='GET')
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    tenant_schema = models.CharField(max_length=100, blank=True, db_index=True)
    user_agent = models.TextField(blank=True)
    referer = models.URLField(max_length=1000, blank=True)
    device_type = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100, blank=True)
    response_time_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        verbose_name = _("سجل زائر")
        verbose_name_plural = _("سجلات الزوار")
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['-timestamp', 'tenant_schema']),
            models.Index(fields=['ip_address', '-timestamp']),
        ]

    def __str__(self):
        return f"{self.ip_address} → {self.path} ({self.timestamp:%H:%M})"


class PlatformEvent(models.Model):
    """
    أحداث المنصة المهمة — تسجيل دخول، تسجيل شركة، دفع، إلخ.
    يظهر كـ Activity Feed في لوحة السوبر أدمن.
    """
    EVENT_TYPES = (
        ('signup', _('تسجيل شركة جديدة')),
        ('login', _('تسجيل دخول')),
        ('payment', _('عملية دفع')),
        ('subscription', _('تفعيل اشتراك')),
        ('suspension', _('تعليق حساب')),
        ('fraud_flag', _('تعليم احتيال')),
        ('invoice', _('إنشاء فاتورة')),
        ('error', _('خطأ في النظام')),
        ('other', _('أخرى')),
    )

    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES)
    tenant_schema = models.CharField(max_length=100, blank=True, db_index=True)
    tenant_name = models.CharField(max_length=150, blank=True)
    user_name = models.CharField(max_length=150, blank=True)
    description = models.TextField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    metadata = models.JSONField(null=True, blank=True)

    class Meta:
        verbose_name = _("حدث منصة")
        verbose_name_plural = _("أحداث المنصة")
        ordering = ['-timestamp']

    def __str__(self):
        return f"[{self.event_type}] {self.description[:80]}"
# =====================================================================
# 🚨 SystemErrorLog — مركز رصد الأخطاء عبر كل المستأجرين (Super Admin)
# =====================================================================
class SystemErrorLog(models.Model):
    LEVEL_CHOICES = (
        ('warning', _('تحذير')),
        ('error', _('خطأ')),
        ('critical', _('حرج')),
    )
    tenant_schema = models.CharField(max_length=63, db_index=True, blank=True, default='')
    tenant_name = models.CharField(max_length=100, blank=True, default='')
    user_id = models.IntegerField(null=True, blank=True)
    username = models.CharField(max_length=150, blank=True, default='')
    path = models.CharField(max_length=500)
    method = models.CharField(max_length=10)
    status_code = models.IntegerField(db_index=True)
    exception_class = models.CharField(max_length=200, blank=True, default='')
    message = models.TextField(blank=True, default='')
    traceback = models.TextField(blank=True, default='')
    request_data = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default='error')
    is_resolved = models.BooleanField(default=False, db_index=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("سجل خطأ نظام")
        verbose_name_plural = _("سجلات أخطاء النظام")
        indexes = [
            models.Index(fields=['-created_at', 'is_resolved']),
            models.Index(fields=['tenant_schema', '-created_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.status_code}] {self.exception_class or self.path} @ {self.tenant_schema or 'public'}"


# =====================================================================
# 🔐 StaffRole — Enterprise RBAC للوحة الـ Super Admin
# =====================================================================
class StaffRole(models.Model):
    """
    يعرّف صلاحيات موظفي الـ Super Admin (مش موظفين الـ tenants).
    is_superuser=True يتجاوز كل القيود (god mode).
    """
    ROLE_CHOICES = (
        ('god',         _('المالك الأعلى')),
        ('tech_admin',  _('مدير تقني')),
        ('support',     _('موظف دعم')),
        ('sales',       _('مبيعات')),
        ('finance',     _('محاسبة ومالية')),
    )
    # خريطة الصلاحيات: أي widgets يقدر يشوفها كل دور
    ROLE_WIDGETS = {
        'god':        {'revenue', 'tenants', 'tickets', 'chat', 'errors', 'plans', 'escrow', 'b2b', 'visitors'},
        'tech_admin': {'tenants', 'tickets', 'chat', 'errors', 'plans', 'visitors'},
        'support':    {'tickets', 'chat'},
        'sales':      {'revenue', 'tenants', 'plans', 'visitors'},
        'finance':    {'revenue', 'escrow', 'plans'},
    }

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='staff_role', verbose_name=_("المستخدم"),
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, verbose_name=_("الدور"))
    can_force_delete = models.BooleanField(default=False, verbose_name=_("صلاحية الحذف النهائي؟"))
    notes = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("دور موظف")
        verbose_name_plural = _("أدوار موظفي الإدارة")

    def __str__(self):
        return f"{self.user.username} — {self.get_role_display()}"

    @property
    def visible_widgets(self):
        return self.ROLE_WIDGETS.get(self.role, set())

    def can_view(self, widget_name):
        return widget_name in self.visible_widgets


# =====================================================================
# 📨 SupportTicket — تذاكر دعم العملاء (Help Form + Chat Offline)
# =====================================================================
class SupportTicket(SoftDeleteMixin, models.Model):
    STATUS_CHOICES = (
        ('open',        _('مفتوحة')),
        ('in_progress', _('جاري الحل')),
        ('waiting',     _('بانتظار رد العميل')),
        ('closed',      _('مغلقة')),
    )
    PRIORITY_CHOICES = (
        ('low',    _('عادية')),
        ('medium', _('متوسطة')),
        ('high',   _('عاجلة')),
        ('urgent', _('طارئة')),
    )
    SOURCE_CHOICES = (
        ('form',         _('فورم اتصل بنا')),
        ('chat_offline', _('شات خارج أوقات العمل')),
        ('ai_chatbot',   _('المساعد الذكي')),
        ('email',        _('بريد إلكتروني')),
        ('phone',        _('مكالمة هاتفية')),
    )

    tenant = models.ForeignKey(
        Client, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='support_tickets', verbose_name=_("المستأجر"),
    )
    name = models.CharField(max_length=120, verbose_name=_("اسم المرسل"))
    email = models.EmailField(verbose_name=_("البريد الإلكتروني"))
    phone = models.CharField(max_length=30, blank=True, default='', verbose_name=_("الهاتف"))
    subject = models.CharField(max_length=200, verbose_name=_("الموضوع"))
    message = models.TextField(verbose_name=_("الرسالة"))

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='open', db_index=True)
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='medium')
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='form')

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='assigned_tickets',
    )
    admin_notes = models.TextField(blank=True, default='', verbose_name=_("ملاحظات داخلية"))

    email_delivered = models.BooleanField(default=False)
    email_error = models.CharField(max_length=255, blank=True, default='')

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("تذكرة دعم")
        verbose_name_plural = _("تذاكر الدعم")
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', '-created_at'])]

    def __str__(self):
        return f"#{self.id} {self.subject[:40]} ({self.get_status_display()})"


# =====================================================================
# 💬 Live Chat — جلسات الدعم الحي + Business Hours routing
# =====================================================================
class ChatSession(models.Model):
    STATUS_CHOICES = (
        ('waiting', _('بانتظار رد')),
        ('active',  _('جارية')),
        ('closed',  _('مغلقة')),
    )
    tenant = models.ForeignKey(
        Client, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='chat_sessions',
    )
    visitor_name = models.CharField(max_length=120, blank=True, default='')
    visitor_email = models.EmailField(blank=True, default='')
    visitor_session_key = models.CharField(max_length=64, db_index=True, blank=True, default='')
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='handled_chats',
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='waiting', db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_activity_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("جلسة شات")
        verbose_name_plural = _("جلسات الشات")
        ordering = ['-started_at']

    def __str__(self):
        return f"Chat #{self.id} — {self.visitor_name or 'ضيف'} ({self.get_status_display()})"

    @property
    def unread_count(self):
        return self.messages.filter(sender='visitor', is_read=False).count()


class ChatMessage(models.Model):
    SENDER_CHOICES = (
        ('visitor', _('زائر')),
        ('agent',   _('موظف دعم')),
        ('bot',     _('بوت تلقائي')),
        ('system',  _('نظام')),
    )
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name='messages')
    sender = models.CharField(max_length=10, choices=SENDER_CHOICES)
    body = models.TextField()
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['created_at']
        indexes = [models.Index(fields=['session', 'created_at'])]


# =====================================================================
# 💎 Customer-tier Diagnostics Subscription (car owners — not workshops)
# =====================================================================
class CustomerDiagnosticsSubscription(models.Model):
    """اشتراك التشخيص لعميل سوق السيارات (MarketplaceCustomer).

    منفصل تماماً عن TenantSubscription (الوِرَش) — العميل بيشخّص عربيته بنفسه.

    دورة الحياة:
      register → trial (7 أيام، 5 سكانات) → upgrade لـ paid → renew/cancel.

    الـ tier هو مصدر الحقيقة:
      trial   → trial_ends_at  حصري
      basic|pro|empire → paid_until + scans_per_month + features
    """
    TIER_CHOICES = (
        ('trial',  _('تجربة مجانية')),
        ('basic',  _('Basic — 99 ج/شهر')),
        ('pro',    _('Pro — 199 ج/شهر')),
        ('empire', _('Empire — 399 ج/شهر')),
        ('expired', _('منتهية')),
    )
    TIER_PRICES_EGP = {
        'trial':  Decimal('0.00'),
        'basic':  Decimal('99.00'),
        'pro':    Decimal('199.00'),
        'empire': Decimal('399.00'),
    }
    TIER_QUOTAS = {  # سكانات/شهر
        'trial':  5,
        'basic':  30,
        'pro':    100,
        'empire': 10_000,  # عملياً غير محدود
    }
    TIER_FEATURES = {
        'trial':  ['ai_diagnosis'],
        'basic':  ['ai_diagnosis', 'vehicle_history'],
        'pro':    ['ai_diagnosis', 'vehicle_history', 'live_data', 'pdf_reports', 'tech_chat'],
        'empire': ['ai_diagnosis', 'vehicle_history', 'live_data', 'pdf_reports', 'tech_chat',
                   'priority_support', 'multi_vehicle', 'parts_rewards'],
    }
    TRIAL_DAYS = 7

    customer = models.OneToOneField(
        MarketplaceCustomer, on_delete=models.CASCADE,
        related_name='diagnostics_subscription',
    )
    tier = models.CharField(max_length=12, choices=TIER_CHOICES, default='trial', db_index=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    paid_until = models.DateTimeField(null=True, blank=True)
    auto_renew = models.BooleanField(default=False)

    # Quota tracking — refilled at the start of each paid month
    period_start = models.DateTimeField(default=timezone.now)
    scans_used = models.IntegerField(default=0)
    lifetime_scans = models.IntegerField(default=0)

    # Payment audit — last successful upgrade
    last_payment_at = models.DateTimeField(null=True, blank=True)
    last_payment_egp = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    last_payment_ref = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("اشتراك تشخيص عميل")
        verbose_name_plural = _("💎 اشتراكات تشخيص العملاء")

    def __str__(self):
        return f"{self.customer.full_name} — {self.get_tier_display()}"

    # ── State helpers ──────────────────────────────────────────────────
    def is_active(self) -> bool:
        """True iff customer can use diagnostics right now."""
        now = timezone.now()
        if self.tier == 'trial':
            return bool(self.trial_ends_at and now < self.trial_ends_at)
        if self.tier in ('basic', 'pro', 'empire'):
            return bool(self.paid_until and now < self.paid_until)
        return False

    def days_remaining(self) -> int:
        end = self.paid_until if self.tier != 'trial' else self.trial_ends_at
        if not end:
            return 0
        delta = end - timezone.now()
        return max(delta.days, 0)

    def quota_remaining(self) -> int:
        return max(self.TIER_QUOTAS.get(self.tier, 0) - self.scans_used, 0)

    def has_feature(self, code: str) -> bool:
        return code in self.TIER_FEATURES.get(self.tier, [])

    def can_scan(self) -> tuple[bool, str]:
        if not self.is_active():
            return False, "الاشتراك منتهي — جدّد للاستمرار."
        if self.quota_remaining() <= 0:
            return False, "انتهت السكانات الشهرية — رقّي الباقة أو انتظر التجديد."
        return True, ""

    def record_scan(self) -> None:
        """Atomic scan counter — safe under concurrent requests."""
        type(self).objects.filter(pk=self.pk).update(
            scans_used=F('scans_used') + 1,
            lifetime_scans=F('lifetime_scans') + 1,
        )
        self.refresh_from_db(fields=['scans_used', 'lifetime_scans'])

    def reset_period_if_needed(self) -> None:
        """Refill scans at the start of each 30-day window for paid tiers."""
        if self.tier not in ('basic', 'pro', 'empire'):
            return
        if (timezone.now() - self.period_start) >= timedelta(days=30):
            self.period_start = timezone.now()
            self.scans_used = 0
            self.save(update_fields=['period_start', 'scans_used'])

    def upgrade(self, new_tier: str, payment_ref: str = '') -> None:
        """Activate a paid tier for 30 days. Caller is responsible for payment."""
        if new_tier not in ('basic', 'pro', 'empire'):
            raise ValueError(f"Invalid tier: {new_tier}")
        now = timezone.now()
        # Stack on top of any remaining paid time (don't burn user's days)
        base = self.paid_until if (self.paid_until and self.paid_until > now) else now
        self.tier = new_tier
        self.paid_until = base + timedelta(days=30)
        self.period_start = now
        self.scans_used = 0
        self.last_payment_at = now
        self.last_payment_egp = self.TIER_PRICES_EGP[new_tier]
        self.last_payment_ref = payment_ref[:64]
        self.save()

    @classmethod
    def grant_trial(cls, customer: 'MarketplaceCustomer') -> 'CustomerDiagnosticsSubscription':
        """Idempotent: returns existing sub if any, else creates a 7-day trial."""
        sub, created = cls.objects.get_or_create(
            customer=customer,
            defaults={
                'tier': 'trial',
                'trial_ends_at': timezone.now() + timedelta(days=cls.TRIAL_DAYS),
            },
        )
        return sub


# =====================================================================
# 💵 ManualPaymentReceipt — unified Vodafone Cash / InstaPay receipts.
# One model for ALL purchase types (subscription / parts / design /
# diagnostics). Admin reviews them in a single place in Super Admin.
# =====================================================================
class ManualPaymentReceipt(models.Model):
    """
    إيصال دفع يدوي (فودافون كاش / إنستاباي) لأي نوع شراء في المنظومة.
    العميل يحوّل → يدخل رقم العملية + يرفع سكرين شوت → الأدمن يراجع ويوافق.
    """
    PURCHASE_TYPES = (
        ('subscription', _('اشتراك SaaS')),
        ('parts',        _('قطع غيار')),
        ('design',       _('باقة تصاميم')),
        ('diagnostics',  _('ترقية تشخيص')),
        ('addon',        _('إضافة (موظف/فرع/خزينة)')),
        ('diag_topup',   _('شحن تشخيص (30 استخدام)')),
        ('tenant_topup', _('شحن تصاميم للشركة')),
    )
    PAYMENT_METHODS = (
        ('vodafone_cash', _('فودافون كاش')),
        ('instapay',      _('إنستاباي')),
    )
    STATUS_CHOICES = (
        ('pending',   _('في انتظار المراجعة')),
        ('confirmed', _('تم التأكيد')),
        ('rejected',  _('مرفوض')),
    )

    receipt_code   = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    purchase_type  = models.CharField(max_length=20, choices=PURCHASE_TYPES, db_index=True)
    purchase_id    = models.PositiveIntegerField(db_index=True,
                        help_text=_("PK of the related DesignPurchase / PartOrder / PlatformInvoice / etc."))

    amount         = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS, default='vodafone_cash')
    sender_phone   = models.CharField(max_length=20, verbose_name=_("رقم المرسل"))
    txn_reference  = models.CharField(max_length=200, verbose_name=_("رقم العملية / Reference"))
    receipt_image  = models.ImageField(upload_to='manual_payments/%Y/%m/',
                        null=True, blank=True, verbose_name=_("سكرين شوت التحويل"))

    # Buyer identity (one of these will be set — depending on context)
    customer       = models.ForeignKey('MarketplaceCustomer', null=True, blank=True,
                        on_delete=models.SET_NULL, related_name='manual_receipts')
    tenant         = models.ForeignKey('Client', null=True, blank=True,
                        on_delete=models.SET_NULL, related_name='manual_receipts')
    contact_phone  = models.CharField(max_length=20, blank=True,
                        help_text=_("Phone to call back for clarification."))
    contact_name   = models.CharField(max_length=120, blank=True)
    notes          = models.TextField(blank=True, verbose_name=_("ملاحظات العميل"))

    status         = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    reviewed_by    = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                        on_delete=models.SET_NULL, related_name='reviewed_receipts')
    reviewed_at    = models.DateTimeField(null=True, blank=True)
    review_notes   = models.TextField(blank=True, verbose_name=_("ملاحظات الأدمن"))

    created_at     = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name        = _("إيصال دفع يدوي")
        verbose_name_plural = _("💵 إيصالات الدفع اليدوي (فودافون/إنستاباي)")
        ordering            = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['purchase_type', 'purchase_id']),
        ]

    def __str__(self):
        return f"{self.get_purchase_type_display()} #{self.purchase_id} — {self.amount} EGP — {self.get_status_display()}"

    @property
    def display_label(self):
        """Short label for the admin list (buyer name + what they're buying)."""
        who = (self.customer.full_name if self.customer_id
               else (self.tenant.name if self.tenant_id
                     else (self.contact_name or self.sender_phone)))
        return f"{who} — {self.get_purchase_type_display()}"

    def get_purchase_object(self):
        """Resolve the related purchase record. Returns None if missing."""
        try:
            if self.purchase_type == 'design':
                return DesignPurchase.objects.filter(pk=self.purchase_id).first()
            if self.purchase_type == 'parts':
                return PartOrder.objects.filter(pk=self.purchase_id).first()
            if self.purchase_type == 'subscription':
                return PlatformInvoice.objects.filter(pk=self.purchase_id).first()
            if self.purchase_type == 'diagnostics':
                return CustomerDiagnosticsSubscription.objects.filter(pk=self.purchase_id).first()
            if self.purchase_type == 'diag_topup':
                return DiagnosticsTopUpPack.objects.filter(pk=self.purchase_id).first()
            if self.purchase_type == 'tenant_topup':
                return TenantDesignTopUp.objects.filter(pk=self.purchase_id).first()
        except Exception:
            return None
        return None

    @transaction.atomic
    def confirm(self, by_user=None, notes: str = ''):
        """Mark receipt as confirmed AND activate the underlying purchase."""
        if self.status == 'confirmed':
            return
        self.status = 'confirmed'
        self.reviewed_by = by_user if by_user and by_user.is_authenticated else None
        self.reviewed_at = timezone.now()
        if notes:
            self.review_notes = notes
        self.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_notes'])

        purchase = self.get_purchase_object()
        if purchase is None:
            logger.warning("[ManualReceipt %s] purchase not found type=%s id=%s",
                          self.receipt_code, self.purchase_type, self.purchase_id)
            return

        # Activate based on purchase type
        if self.purchase_type == 'design':
            purchase.status = 'paid'
            purchase.paid_at = timezone.now()
            purchase.payment_reference = self.txn_reference
            purchase.sender_phone = self.sender_phone
            purchase.save(update_fields=['status', 'paid_at', 'payment_reference', 'sender_phone'])
        elif self.purchase_type == 'subscription':
            try:
                purchase.payment_reference = self.txn_reference
                purchase.payment_provider = self.payment_method
                purchase.save(update_fields=['payment_reference', 'payment_provider'])
                purchase.mark_paid()  # triggers subscription extension
            except Exception:
                logger.exception("[ManualReceipt] subscription mark_paid failed")
        elif self.purchase_type == 'parts':
            if purchase.status == 'pending_payment':
                purchase.status = 'paid_held'
                purchase.paid_at = timezone.now()
                purchase.paymob_txn_id = f'manual:{self.txn_reference}'
                purchase.save(update_fields=['status', 'paid_at', 'paymob_txn_id'])
                PartListing.objects.filter(pk=purchase.listing_id).update(
                    status='sold', sold_at=timezone.now(),
                )
                try:
                    from clients.services import escrow as escrow_svc
                    escrow_svc.place_hold(purchase)
                except Exception:
                    logger.exception("[ManualReceipt] place_hold failed")
        elif self.purchase_type == 'diagnostics':
            tier = (self.notes or '').strip() or 'basic'  # tier stored in notes
            try:
                purchase.upgrade(tier, payment_ref=f'manual:{self.txn_reference}')
            except Exception:
                logger.exception("[ManualReceipt] diagnostics upgrade failed")
        elif self.purchase_type == 'diag_topup':
            # purchase is the DiagnosticsTopUpPack; credit its uses to the
            # tenant on this receipt.
            if self.tenant_id and getattr(purchase, 'uses_granted', 0) > 0:
                try:
                    from clients.services.diagnostics_quota import add_topup
                    add_topup(self.tenant, purchase.uses_granted)
                except Exception:
                    logger.exception("[ManualReceipt] diag_topup credit failed")
        elif self.purchase_type == 'tenant_topup':
            # purchase is the TenantDesignTopUp itself; flip to paid.
            try:
                purchase.status = 'paid'
                purchase.paid_at = timezone.now()
                purchase.payment_reference = self.txn_reference
                purchase.payment_method = self.payment_method
                purchase.save(update_fields=[
                    'status', 'paid_at', 'payment_reference', 'payment_method',
                ])
            except Exception:
                logger.exception("[ManualReceipt] tenant_topup activate failed")

    @transaction.atomic
    def reject(self, by_user=None, notes: str = ''):
        self.status = 'rejected'
        self.reviewed_by = by_user if by_user and by_user.is_authenticated else None
        self.reviewed_at = timezone.now()
        self.review_notes = notes or 'لم يتم العثور على التحويل'
        self.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_notes'])


# OBD device identity & secrets — defined in a separate module for clarity.
# Imported here so Django registers them under the `clients` app.
from clients.obd_device_models import OBDDevice, OBDDeviceNonce  # noqa: E402, F401

