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

# Cross-domain: Client (and other tenancy models) referenced by FKs below.
from .tenancy import *  # noqa: F401, F403


# Marketplace C2C: customer marketplace + tender flow + notifications.


# =====================================================================
# 🛍️ سوق العملاء والمناقصات المجهولة (Customer Marketplace & Blind Tenders)
# =====================================================================

class MarketplaceCustomer(SoftDeleteMixin, models.Model):
    """
    عميل نهائي في سوق المناقصات — فرد أو شركة يبحث عن خدمات/منتجات.
    مستقل تماماً عن نظام المستأجرين (Tenants).
    """
    CUSTOMER_TYPE_CHOICES = (
        ('individual', _('فرد')),
        ('company', _('شركة / مؤسسة')),
    )
    SECTOR_CHOICES = (
        ('automotive', _('🚗 سيارات — صيانة وقطع غيار')),
        ('printing', _('🎨 طباعة وتصميم')),
    )

    uid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    customer_type = models.CharField(max_length=20, choices=CUSTOMER_TYPE_CHOICES, verbose_name=_("نوع العميل"))
    full_name = models.CharField(max_length=150, verbose_name=_("الاسم الكامل"))
    company_name = models.CharField(max_length=200, blank=True, verbose_name=_("اسم الشركة"))
    phone = models.CharField(max_length=20, unique=True, db_index=True, verbose_name=_("رقم الموبايل"))
    email = models.EmailField(blank=True, null=True, verbose_name=_("البريد الإلكتروني"))
    job_title = models.CharField(max_length=100, blank=True, verbose_name=_("الوظيفة / المسمى"))
    sector = models.CharField(max_length=20, choices=SECTOR_CHOICES, verbose_name=_("القطاع"))
    city = models.CharField(max_length=100, blank=True, verbose_name=_("المدينة / المحافظة"))

    # Auth — phone + password (مع OTP اختياري للتحقق)
    password_hash = models.CharField(max_length=128, blank=True, verbose_name=_("كلمة المرور (مُشفّرة)"))
    otp_code = models.CharField(max_length=6, blank=True)
    otp_expires_at = models.DateTimeField(null=True, blank=True)
    is_verified = models.BooleanField(default=False, verbose_name=_("تم التحقق من الموبايل"))
    session_token = models.UUIDField(default=uuid.uuid4, unique=True)
    last_login_at = models.DateTimeField(null=True, blank=True, verbose_name=_("آخر تسجيل دخول"))

    # Free trial designs — 2 for individual, 4 for company
    free_designs_total = models.IntegerField(default=0, verbose_name=_("تصاميم مجانية (إجمالي)"),
        help_text=_("فرد = 2 مجاني، شركة = 4 مجاني. يتم تعيينها تلقائياً عند التسجيل"))
    free_designs_used = models.IntegerField(default=0, verbose_name=_("تصاميم مجانية مستخدمة"))

    # Trust & Stats
    total_requests = models.IntegerField(default=0)
    total_accepted_offers = models.IntegerField(default=0)
    avg_rating_given = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal('0.00'))
    is_blocked = models.BooleanField(default=False, verbose_name=_("محظور"))

    created_at = models.DateTimeField(auto_now_add=True)
    last_active = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("عميل السوق")
        verbose_name_plural = _("🛍️ عملاء سوق المناقصات")
        ordering = ['-created_at']

    def __str__(self):
        label = self.company_name or self.full_name
        return f"{label} ({self.get_sector_display()})"

    def generate_otp(self):
        import secrets
        self.otp_code = str(secrets.randbelow(900000) + 100000)
        self.otp_expires_at = timezone.now() + timedelta(minutes=10)
        self.save(update_fields=['otp_code', 'otp_expires_at'])
        return self.otp_code

    # ── Password authentication ──
    def set_password(self, raw_password):
        """تعيين كلمة المرور مع التشفير الآمن (PBKDF2)."""
        from django.contrib.auth.hashers import make_password
        self.password_hash = make_password(raw_password)

    def check_password(self, raw_password):
        """التحقق من كلمة المرور."""
        from django.contrib.auth.hashers import check_password
        if not self.password_hash or not raw_password:
            return False
        return check_password(raw_password, self.password_hash)

    def has_usable_password(self):
        return bool(self.password_hash)

    def verify_otp(self, code):
        import hmac
        if self.otp_code and hmac.compare_digest(self.otp_code, str(code)) and self.otp_expires_at and timezone.now() < self.otp_expires_at:
            self.is_verified = True
            self.otp_code = ''
            self.session_token = uuid.uuid4()
            self.save(update_fields=['is_verified', 'otp_code', 'session_token'])
            return True
        return False

    @property
    def free_designs_remaining(self):
        return max(self.free_designs_total - self.free_designs_used, 0)

    @property
    def has_free_designs(self):
        return self.free_designs_remaining > 0

    def consume_free_design(self):
        """خصم تصميم مجاني (atomic)"""
        from django.db import transaction as _tx
        with _tx.atomic():
            type(self).objects.filter(pk=self.pk).update(
                free_designs_used=F('free_designs_used') + 1)
            self.refresh_from_db()

    def save(self, *args, **kwargs):
        # 🛡️ [Anti-abuse 2026-06-11]: التصميم المجاني التلقائي اتشال.
        # كان أي حد يقدر يـ script signup بأرقام عشوائية ويحرق provider
        # quota (FLUX ~$0.05/صورة). الـ super admin يقدر يديها يدوياً
        # من super_admin_customer_gift (POST بـ designs=N) لما يحب
        # يـ promote عميل موثوق — الـ URL: /superadmin/customer/<id>/gift/.

        # Normalize Egyptian phone numbers — consistent +20 prefix
        if self.phone and not self.phone.startswith('+'):
            digits = self.phone.lstrip('0')
            if len(digits) == 10 and digits.startswith('1'):
                self.phone = f'+20{digits}'          # bare mobile (1xxxxxxxxx)
            elif len(digits) == 11 and digits.startswith('01'):
                self.phone = f'+2{digits}'           # with leading 0 (01xxxxxxxxx)
            elif len(digits) == 12 and digits.startswith('201'):
                self.phone = f'+{digits}'            # already has country code
        super().save(*args, **kwargs)


# =====================================================================
# 🛡️ UserVerification — KYC engine (Identity + Business documents)
# Inspired by eBay ID Verify, Amazon Seller Verification, Etsy KYC.
# =====================================================================
def _verification_upload_path(instance, filename):
    """Store verification documents under per-customer folders, keyed by UUID
    so filenames are non-guessable. Never expose these paths to the public."""
    return f'verifications/{instance.customer.uid}/{uuid.uuid4().hex}_{filename}'


class UserVerification(models.Model):
    """
    Holds KYC documents for a MarketplaceCustomer. One-to-one with the
    customer. The cached ``trust_score`` (0–100) is recomputed every save.

    Tiering — matches global marketplace standards:
      * NEW     (0)   : phone unverified, anonymous risk
      * BASIC   (20)  : phone OTP verified
      * EMAIL   (40)  : + email confirmed
      * ID      (70)  : + government ID approved
      * BUSINESS(100) : + commercial / workshop license approved
    """
    STATUS_CHOICES = (
        ('not_submitted', _('لم يُقدَّم')),
        ('pending',       _('قيد المراجعة')),
        ('approved',      _('معتمد')),
        ('rejected',      _('مرفوض')),
    )
    ID_TYPE_CHOICES = (
        ('national_id', _('بطاقة رقم قومي')),
        ('passport',    _('جواز سفر')),
    )
    TIER_CHOICES = (
        ('new',      _('جديد')),
        ('basic',    _('أساسي')),
        ('email',    _('موثّق بالبريد')),
        ('id',       _('هوية موثّقة')),
        ('business', _('عمل موثّق')),
    )

    customer = models.OneToOneField(
        'MarketplaceCustomer', on_delete=models.CASCADE,
        related_name='verification',
    )

    # — Government ID —
    id_type = models.CharField(max_length=20, choices=ID_TYPE_CHOICES, default='national_id')
    id_number_last4 = models.CharField(max_length=4, blank=True, default='',
        help_text=_("نخزّن آخر 4 أرقام فقط — التطابق الكامل يتم وقت المراجعة"))
    id_document_front = models.ImageField(upload_to=_verification_upload_path, null=True, blank=True)
    id_document_back  = models.ImageField(upload_to=_verification_upload_path, null=True, blank=True)
    id_status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='not_submitted', db_index=True)
    id_submitted_at = models.DateTimeField(null=True, blank=True)
    id_reviewed_at = models.DateTimeField(null=True, blank=True)
    id_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    id_rejection_reason = models.CharField(max_length=255, blank=True, default='')

    # — Selfie / liveness (optional, boosts trust but not gating) —
    selfie_image = models.ImageField(upload_to=_verification_upload_path, null=True, blank=True)
    selfie_status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='not_submitted')

    # — Workshop / mechanic license (automotive sellers) —
    workshop_license_number_last4 = models.CharField(max_length=8, blank=True, default='')
    workshop_license_image = models.ImageField(upload_to=_verification_upload_path, null=True, blank=True)
    workshop_license_status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='not_submitted')

    # — Business / commercial registration —
    business_registration_image = models.ImageField(upload_to=_verification_upload_path, null=True, blank=True)
    business_registration_status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='not_submitted')
    tax_card_image = models.ImageField(upload_to=_verification_upload_path, null=True, blank=True)
    tax_card_status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='not_submitted')

    # — Cached score / tier (recomputed on save) —
    trust_score = models.IntegerField(default=0, db_index=True,
        help_text=_("0–100. يُحسب تلقائياً من حالة المستندات."))
    trust_tier = models.CharField(max_length=10, choices=TIER_CHOICES, default='new', db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("توثيق هوية مستخدم")
        verbose_name_plural = _("🛡️ توثيقات الهوية (KYC)")

    def __str__(self):
        return f"{self.customer} — {self.trust_score}% ({self.get_trust_tier_display()})"

    # ─── Score calculation ───
    def compute_score(self) -> tuple[int, str]:
        """
        Returns (score, tier). Pure function of current field state — no DB
        writes. Tier breakpoints match global-marketplace conventions so the
        UI badge mapping stays predictable.
        """
        score = 0
        tier = 'new'
        c = self.customer
        if c and c.is_verified:                         # phone OTP
            score = 20; tier = 'basic'
        if c and c.email:                                # email present
            # Email "verified" is implied by presence here — wire to a real
            # confirmation flow later if needed.
            score = max(score, 40); tier = 'email'
        if self.id_status == 'approved':
            score = max(score, 70); tier = 'id'
        if (self.business_registration_status == 'approved'
                or self.workshop_license_status == 'approved'):
            score = 100; tier = 'business'
        # Selfie is a +5 boost but capped to 100.
        if self.selfie_status == 'approved' and score < 100:
            score = min(score + 5, 99)  # leaves 100 reserved for business tier
        return score, tier

    def recompute(self, save=True):
        score, tier = self.compute_score()
        self.trust_score = score
        self.trust_tier = tier
        if save:
            super().save(update_fields=['trust_score', 'trust_tier', 'updated_at'])
        return score

    def save(self, *args, **kwargs):
        # Always keep cached score in sync. Done before super().save() so the
        # written row already has the correct trust_score.
        score, tier = self.compute_score()
        self.trust_score = score
        self.trust_tier = tier
        super().save(*args, **kwargs)


class ServiceRequest(models.Model):
    """
    طلب خدمة / منتج من عميل — المناقصة الأساسية.
    يظهر لكل التجار المنتمين لنفس القطاع بشكل مجهول.
    """
    STATUS_CHOICES = (
        ('pending_approval', _('في انتظار موافقة الإدارة')),
        ('open', _('مفتوح — في انتظار العروض')),
        ('reviewing', _('جاري مراجعة العروض')),
        ('accepted', _('تم قبول عرض')),
        ('completed', _('مكتمل — تم التقييم')),
        ('expired', _('منتهي الصلاحية')),
        ('cancelled', _('ملغي بواسطة العميل')),
        ('rejected_by_admin', _('مرفوض من الإدارة')),
    )
    URGENCY_CHOICES = (
        ('normal', _('عادي — خلال أسبوع')),
        ('soon', _('قريب — خلال 3 أيام')),
        ('urgent', _('عاجل — خلال 24 ساعة')),
    )

    request_code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    customer = models.ForeignKey(MarketplaceCustomer, on_delete=models.CASCADE, related_name='requests', verbose_name=_("العميل"))
    sector = models.CharField(max_length=20, choices=MarketplaceCustomer.SECTOR_CHOICES, verbose_name=_("القطاع"))

    title = models.CharField(max_length=300, verbose_name=_("عنوان الطلب"))
    description = models.TextField(verbose_name=_("تفاصيل الطلب"))
    urgency = models.CharField(max_length=10, choices=URGENCY_CHOICES, default='normal', verbose_name=_("درجة الاستعجال"))

    # Customer preferences
    wants_images = models.BooleanField(default=False, verbose_name=_("يريد صور مع العروض"))
    customer_city = models.CharField(max_length=100, blank=True, verbose_name=_("مدينة العميل"))
    max_budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                     verbose_name=_("الميزانية القصوى (اختياري — مخفي عن التجار)"))

    # Attachments (customer can upload reference images)
    attachment_1 = models.ImageField(upload_to='marketplace/requests/', blank=True, null=True, verbose_name=_("صورة مرجعية 1"))
    attachment_2 = models.ImageField(upload_to='marketplace/requests/', blank=True, null=True, verbose_name=_("صورة مرجعية 2"))

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending_approval', db_index=True)
    is_approved = models.BooleanField(default=False, verbose_name=_("موافقة الإدارة"))
    admin_notes = models.TextField(blank=True, verbose_name=_("ملاحظات الإدارة"))
    offers_count = models.IntegerField(default=0)
    accepted_offer = models.ForeignKey('TenderOffer', on_delete=models.SET_NULL, null=True, blank=True, related_name='accepted_for')

    # Platform economics
    platform_commission_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('5.00'),
                                                    verbose_name=_("عمولة المنصة (%)"))
    platform_commission_earned = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(verbose_name=_("ينتهي الطلب في"))
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("طلب خدمة / مناقصة")
        verbose_name_plural = _("🛍️ طلبات سوق العملاء")
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['sector', 'status', '-created_at']),
        ]

    def __str__(self):
        return f"REQ-{str(self.request_code)[:8]} | {self.title[:50]}"

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at and self.status == 'open'

    def auto_expire(self):
        if self.is_expired:
            self.status = 'expired'
            self.save(update_fields=['status'])


class TenderOffer(models.Model):
    """
    عرض سعر من تاجر على طلب عميل.
    التاجر والعميل مجهولان لبعضهما حتى يتم القبول.
    """
    STATUS_CHOICES = (
        ('pending', _('في انتظار مراجعة العميل')),
        ('accepted', _('مقبول')),
        ('rejected', _('مرفوض')),
        ('withdrawn', _('تم سحبه من التاجر')),
    )

    offer_code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    service_request = models.ForeignKey(ServiceRequest, on_delete=models.CASCADE, related_name='offers', verbose_name=_("طلب الخدمة"))
    merchant = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='tender_offers', verbose_name=_("التاجر"))

    price = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("السعر المقترح"))
    description = models.TextField(verbose_name=_("تفاصيل العرض"))
    estimated_days = models.IntegerField(default=1, verbose_name=_("أيام التنفيذ المتوقعة"))
    warranty_days = models.IntegerField(default=0, verbose_name=_("مدة الضمان (أيام)"))

    # Merchant location (visible to customer for proximity)
    merchant_city = models.CharField(max_length=100, verbose_name=_("مدينة التاجر"))
    merchant_address = models.CharField(max_length=300, blank=True, verbose_name=_("عنوان التاجر التفصيلي"))

    # Attachments
    image_1 = models.ImageField(upload_to='marketplace/offers/', blank=True, null=True, verbose_name=_("صورة 1"))
    image_2 = models.ImageField(upload_to='marketplace/offers/', blank=True, null=True, verbose_name=_("صورة 2"))
    image_3 = models.ImageField(upload_to='marketplace/offers/', blank=True, null=True, verbose_name=_("صورة 3"))
    file_attachment = models.FileField(upload_to='marketplace/offers/files/', blank=True, null=True, verbose_name=_("ملف مرفق (PDF/Word)"))

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)

    # Rating (after completion)
    customer_rating = models.IntegerField(null=True, blank=True, verbose_name=_("تقييم العميل (1-5)"))
    customer_review = models.TextField(blank=True, verbose_name=_("تعليق العميل"))

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("عرض سعر تاجر")
        verbose_name_plural = _("عروض أسعار التجار")
        unique_together = ('service_request', 'merchant')
        ordering = ['price']

    def __str__(self):
        return f"OFFER-{str(self.offer_code)[:8]} | {self.price} EGP"

    @property
    def merchant_display_name(self):
        """اسم مستعار للتاجر — مجهول حتى القبول"""
        return f"تاجر #{self.pk}"

    @property
    def is_images_required(self):
        return self.service_request.wants_images


# =====================================================================
# 🔔 إشعارات وهدايا الأدمن لعملاء السوق (Admin → Customer Notifications)
# =====================================================================

class CustomerNotification(models.Model):
    """
    إشعار يرسله السوبر أدمن لعميل في السوق (هدية، عرض، تنبيه...).
    يظهر للعميل في جرس الإشعارات داخل داش بورد السوق.
    """
    LEVEL_CHOICES = (
        ('info',    _('معلومة')),
        ('success', _('نجاح / هدية')),
        ('warning', _('تنبيه')),
        ('danger',  _('تحذير')),
    )

    customer = models.ForeignKey(
        'MarketplaceCustomer', on_delete=models.CASCADE,
        related_name='notifications', verbose_name=_("العميل"),
    )
    title = models.CharField(max_length=200, verbose_name=_("العنوان"))
    body = models.TextField(verbose_name=_("النص"))
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default='info')
    icon = models.CharField(max_length=50, blank=True, default='fa-bell',
                            help_text=_("Font Awesome class, e.g. fa-gift"))
    action_url = models.CharField(max_length=300, blank=True,
                                  help_text=_("رابط اختياري للعميل ينقر عليه"))
    action_label = models.CharField(max_length=80, blank=True)

    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='sent_customer_notifications',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("إشعار عميل")
        verbose_name_plural = _("🔔 إشعارات العملاء")
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.level}] → {self.customer_id}: {self.title[:60]}"

    @property
    def is_read(self):
        return self.read_at is not None

    def mark_read(self):
        if not self.read_at:
            self.read_at = timezone.now()
            self.save(update_fields=['read_at'])


