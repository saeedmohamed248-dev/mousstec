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

# 🎯 أتمتة الفخ الذهبي: فترة تجريبية 3 أيام فقط
def default_trial_end():
    return timezone.now().date() + timedelta(days=3)

# =====================================================================
# 🏢 1. جدول المستأجرين (شركات Mouss Tec Ecosystem)
# =====================================================================
class Client(SoftDeleteMixin, TenantMixin):
    ADDON_PRICE_PER_MONTH = Decimal('125.00')
    # ⚠️ Legacy fallback prices — source of truth is Plan.monthly_price (DB).
    # Kept for any legacy code path that asks before a Plan row is loaded.
    PLAN_BASE_PRICES = {
        # سيارات
        'silver': Decimal('550.00'),
        'gold': Decimal('850.00'),
        'empire': Decimal('2500.00'),
        # طباعة
        'print_basic': Decimal('875.00'),
        'print_pro': Decimal('1250.00'),
        'print_enterprise': Decimal('2000.00'),
    }

    name = models.CharField(max_length=100, verbose_name=_("اسم المركز/الشركة"))
    owner_name = models.CharField(max_length=100, verbose_name=_("اسم المالك"))
    phone = models.CharField(max_length=20, verbose_name=_("رقم الهاتف"))
    email = models.EmailField(blank=True, null=True, verbose_name=_("البريد الإلكتروني للإدارة"))
    
    # 🏭 القطاع الصناعي (Industry Vertical) — يحدد أي app يظهر للمستأجر
    INDUSTRY_CHOICES = (
        ('automotive', _('🚗 سيارات — صيانة وقطع غيار')),
        ('printing', _('🎨 طباعة وتصميم جرافيك')),
    )
    industry = models.CharField(max_length=20, choices=INDUSTRY_CHOICES, default='automotive', verbose_name=_("القطاع"))

    BUSINESS_TYPE_CHOICES = (
        # قطاع السيارات
        ('service_center', _('مركز صيانة متكامل')),
        ('parts_dealer', _('تاجر قطع غيار (مبيعات تجزئة وجملة)')),
        ('scrap_importer', _('مستورد تقطيع وأنصاف (محرك الـ Scrap)')),
        ('both', _('توكيل شامل (صيانة + تجارة + استيراد)')),
        # قطاع الطباعة والتصميم
        ('print_shop', _('مطبعة (طباعة رقمية وأوفست)')),
        ('design_studio', _('استوديو تصميم جرافيك')),
        ('print_and_design', _('مطبعة + تصميم (شامل)')),
    )
    business_type = models.CharField(max_length=20, choices=BUSINESS_TYPE_CHOICES, default='service_center', verbose_name=_("نوع النشاط"))
    
    # 🛡️ المعمارية المالية (FinTech Standards)
    wallet_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0.00, verbose_name=_("الرصيد المتاح للسحب"))
    escrow_held = models.DecimalField(max_digits=15, decimal_places=2, default=0.00, verbose_name=_("رصيد مجمد في الضمان (Escrow)"))
    platform_fee_rate = models.DecimalField(max_digits=5, decimal_places=2, default=2.50, verbose_name=_("عمولة Mouss Tec (%)"))
    
    # 🌟 الثقة والسوق المفتوح (Marketplace & SLA)
    is_marketplace_active = models.BooleanField(default=True, verbose_name=_("مشارك في سوق التجار (B2B)؟"))
    is_verified_merchant = models.BooleanField(default=False, verbose_name=_("تاجر موثق (علامة زرقاء)"))
    commercial_register = models.CharField(max_length=100, blank=True, null=True, verbose_name=_("السجل التجاري"))
    
    # 🤖 مؤشرات الثقة ومكافحة الاحتيال بالذكاء الاصطناعي (AI Shield Telemetry)
    market_rating = models.DecimalField(max_digits=3, decimal_places=2, default=5.00, verbose_name=_("التقييم العام"))
    successful_deals = models.IntegerField(default=0, verbose_name=_("الصفقات الناجحة"))
    dispute_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, verbose_name=_("نسبة النزاعات (%)"))
    ai_trust_score = models.IntegerField(default=100, help_text="مؤشر الثقة الديناميكي للتاجر (يُحسب آلياً بواسطة AI)")
    is_fraud_flagged = models.BooleanField(default=False, verbose_name=_("مُحظر آلياً للاشتباه بالاحتيال"))

    # 🚀 محرك الباقات والإضافات الديناميكية (Smart Quotas & Add-ons)
    SUBSCRIPTION_CHOICES = (
        # باقات السيارات
        ('silver', _('باقة سيلفر — لمراكز الصيانة وتجار قطع الغيار')),
        ('gold', _('باقة جولد — لمراكز الصيانة وتجار قطع الغيار الشامل')),
        ('empire', _('باقة Empire — لتجار القطع والشركات الكبيرة')),
        # باقات الطباعة والتصميم
        ('print_basic', _('Print Basic — للمطابع الصغيرة واستوديوهات التصميم')),
        ('print_pro', _('Print Pro — للمطابع المتوسطة ومكاتب التصميم')),
        ('print_enterprise', _('Print Enterprise — للمطابع الكبيرة ومجموعات التصميم')),
    )
    plan = models.CharField(max_length=20, choices=SUBSCRIPTION_CHOICES, default='gold', verbose_name=_("الباقة"))
    
    max_branches = models.IntegerField(default=2, verbose_name=_("الفروع المشمولة بالباقة"))
    max_users = models.IntegerField(default=5, verbose_name=_("المستخدمين المشمولين بالباقة"))
    max_repair_cards = models.IntegerField(default=0, help_text="0 تعني غير محدود", verbose_name=_("حد كروت الصيانة الشهري"))
    max_inventory_items = models.IntegerField(default=0, help_text="0 تعني غير محدود", verbose_name=_("حد أصناف المخزن"))
    
    max_treasuries = models.IntegerField(default=2, verbose_name=_("الخزائن المشمولة بالباقة"))

    extra_branches_purchased = models.IntegerField(default=0, verbose_name=_("فروع إضافية مشتراة"))
    extra_users_purchased = models.IntegerField(default=0, verbose_name=_("مستخدمين إضافيين مشتراة"))
    extra_treasuries_purchased = models.IntegerField(default=0, verbose_name=_("خزائن إضافية مشتراة"))
    
    STATUS_CHOICES = (
        ('trial', _('فترة تجريبية')),
        ('active', _('نشط (مدفوع)')),
        ('suspended', _('معلق (لعدم الدفع)')),
        ('cancelled', _('ملغي (محذوف سوفت)')),
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='trial', verbose_name=_("حالة الحساب"))
    trial_ends_at = models.DateField(default=default_trial_end, verbose_name=_("نهاية الفترة التجريبية"))
    subscription_end_date = models.DateField(blank=True, null=True, verbose_name=_("تاريخ انتهاء الاشتراك"))
    
    logo = models.ImageField(upload_to='tenant_logos/', blank=True, null=True, verbose_name=_("لوجو المركز"))
    theme_color = models.CharField(max_length=7, default='#007bff', verbose_name=_("اللون الأساسي للسيستم"))

    created_on = models.DateField(auto_now_add=True)
    is_active = models.BooleanField(default=True, verbose_name=_("الاشتراك فعال؟"))

    # 🔧 OBD / Diagnostics-Room paid add-on — gated independently from the main Plan.
    #   has_obd_access=True  + obd_access_expiry=None   ⇒ lifetime
    #   has_obd_access=True  + future expiry            ⇒ timed grant
    #   has_obd_access=False                            ⇒ no access
    has_obd_access = models.BooleanField(
        default=False, verbose_name=_("الوصول لغرفة التشخيص (OBD)"),
    )
    obd_access_expiry = models.DateTimeField(
        blank=True, null=True,
        verbose_name=_("انتهاء صلاحية وصول OBD"),
        help_text=_("اتركها فارغة للوصول مدى الحياة."),
    )

    auto_create_schema = True
    auto_drop_schema = False

    class Meta:
        verbose_name = _("شركة / مركز (عميل SaaS)")
        verbose_name_plural = _("شركات Mouss Tec Ecosystem")

    def __str__(self):
        return self.name

    @property
    def is_valid_subscription(self):
        """🚀 تمت إضافة خوارزمية فترة السماح (Grace Period 3 days) لزيادة الاحتفاظ بالعملاء"""
        if self.status == 'suspended' or not self.is_active or self.is_fraud_flagged: 
            return False
        today = timezone.now().date()
        if self.status == 'trial' and today > self.trial_ends_at: 
            return False
        if self.status == 'active' and self.subscription_end_date:
            # فترة سماح 3 أيام بعد الانتهاء
            grace_period_end = self.subscription_end_date + timedelta(days=3)
            if today > grace_period_end: 
                return False
        return True

    @property
    def total_allowed_branches(self):
        return self.max_branches + self.extra_branches_purchased

    @property
    def total_allowed_users(self):
        return self.max_users + self.extra_users_purchased

    @property
    def total_allowed_treasuries(self):
        return self.max_treasuries + self.extra_treasuries_purchased

    def calculate_prorated_addon_cost(self, addon_monthly_price=None):
        if addon_monthly_price is None:
            addon_monthly_price = self.ADDON_PRICE_PER_MONTH
        if not self.subscription_end_date:
            return addon_monthly_price
        today = timezone.now().date()
        remaining = (self.subscription_end_date - today).days
        if remaining <= 0:
            return addon_monthly_price
        return (addon_monthly_price * Decimal(str(remaining)) / Decimal('30')).quantize(Decimal('0.01'))

    def save(self, *args, **kwargs):
        # ⚠️  Phase 0b cleanup (2026-06): شلنا الـ hardcoded if/elif اللي كان
        # بيـ overwrite max_* بناءً على Client.plan (CharField). الـ source
        # of truth بقى TenantSubscription.plan (FK لـ Plan model)؛ مزامنة الـ
        # limits بتـ enforce في TenantSubscription.save(). الـ Client.plan
        # CharField متروك كـ legacy display field لحد Phase 5.
        super().save(*args, **kwargs)

    # 🛡️ ربط الـ Soft Delete بـ is_active عشان عملية الحذف الوهمي
    #    تقفل الـ login تلقائياً من غير ما نلمس FK Foreign keys.
    def soft_delete(self, user=None, reason=''):
        self.is_active = False
        self.status = 'cancelled'
        super().soft_delete(user=user, reason=reason)

    def restore(self):
        self.is_active = True
        if self.status == 'cancelled':
            self.status = 'suspended'  # يحتاج تفعيل يدوي
        super().restore()

    # ─── 🔧 OBD subscription helpers ───────────────────────────────────
    @property
    def obd_access_is_valid(self) -> bool:
        """True iff this tenant currently has live OBD/Diagnostics access."""
        if not self.has_obd_access:
            return False
        if self.obd_access_expiry is None:
            return True  # lifetime
        return timezone.now() < self.obd_access_expiry

    @property
    def obd_access_is_lifetime(self) -> bool:
        return self.has_obd_access and self.obd_access_expiry is None

    def grant_obd_access(self, duration=None, *, by_user=None):
        """Grant or extend OBD access.

        `duration` is a `datetime.timedelta` to add to *the later of*
        `now()` and the current `obd_access_expiry` (so stacking +1m on
        +1m gives 2 months). `duration=None` means lifetime.
        """
        from django.utils import timezone as _tz
        self.has_obd_access = True
        if duration is None:
            self.obd_access_expiry = None
        else:
            base = self.obd_access_expiry if (
                self.obd_access_expiry and self.obd_access_expiry > _tz.now()
            ) else _tz.now()
            self.obd_access_expiry = base + duration
        self.save(update_fields=['has_obd_access', 'obd_access_expiry'])

    def revoke_obd_access(self, *, by_user=None):
        self.has_obd_access = False
        self.obd_access_expiry = None
        self.save(update_fields=['has_obd_access', 'obd_access_expiry'])

# =====================================================================
# 🌐 2. جدول النطاقات
# =====================================================================
class Domain(DomainMixin):
    pass

# =====================================================================
# 🛒 3. المؤشر المركزي لسوق التجار (Global B2B Marketplace)
# =====================================================================

# =====================================================================
# 💎 7. باقات الاشتراك الموحدة (Unified Subscription Plans)
# =====================================================================
# 💎 7a. Feature Catalog — مرجع مركزي لكل feature codes صالحة
# =====================================================================
# الفلسفة: code = source of truth لـ entitlements gating logic.
# الـ Plan.entitlements JSONField بتـ reference codes من الجدول ده،
# والـ Plan.clean() بتـ validate إن مفيش code غلط. ده بيمنع typos
# زي "ai_studoi" تعدّي بدون error في الـ admin UI.
# =====================================================================
class Feature(models.Model):
    CATEGORY_CHOICES = (
        ('core',         _('🏛️ Core — أساسية')),
        ('workshop',     _('🚗 Workshop — مراكز صيانة')),
        ('printing',     _('🎨 Printing — مطابع')),
        ('marketplace',  _('🛒 Marketplace — أسواق')),
        ('analytics',    _('📊 Analytics — تقارير')),
        ('integrations', _('🔌 Integrations — تكاملات')),
        ('support',      _('🛟 Support — دعم')),
    )

    code = models.SlugField(
        max_length=60, unique=True, verbose_name=_("الكود البرمجي"),
        help_text=_("معرف فريد بـ snake_case — يتـ reference من Plan.entitlements"),
    )
    name_ar = models.CharField(max_length=120, verbose_name=_("الاسم بالعربية"))
    name_en = models.CharField(max_length=120, verbose_name=_("الاسم بالإنجليزية"))
    description = models.TextField(blank=True, verbose_name=_("الوصف"))
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, verbose_name=_("التصنيف"))

    is_quantitative = models.BooleanField(
        default=False,
        verbose_name=_("له حد رقمي؟"),
        help_text=_("True لو الـ feature لها monthly_limit أو quantitative cap"),
    )
    unit_label_ar = models.CharField(
        max_length=40, blank=True, verbose_name=_("وحدة القياس"),
        help_text=_("مثلاً: 'تصميم/شهر' أو 'رسالة/شهر' — للعرض في الـ UI"),
    )

    is_active = models.BooleanField(default=True, verbose_name=_("مفعّل"))
    sort_order = models.IntegerField(default=0)

    class Meta:
        verbose_name = _("ميزة")
        verbose_name_plural = _("💎 Feature Catalog — مرجع المزايا")
        ordering = ['category', 'sort_order', 'code']
        indexes = [models.Index(fields=['category', 'is_active'])]

    def __str__(self):
        return f"[{self.category}] {self.name_ar} ({self.code})"


# =====================================================================
class Plan(models.Model):
    INDUSTRY_CHOICES = (
        ('automotive', _('سيارات')),
        ('printing', _('طباعة وتصميم')),
    )
    slug = models.SlugField(max_length=40, unique=True, verbose_name=_("المعرف"))
    name = models.CharField(max_length=80, verbose_name=_("اسم الباقة"))
    industry = models.CharField(max_length=20, choices=INDUSTRY_CHOICES, verbose_name=_("القطاع"))

    monthly_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("السعر الشهري (ج.م)"))
    quarterly_discount = models.IntegerField(default=10, verbose_name=_("خصم ربع سنوي (%)"))
    semi_annual_discount = models.IntegerField(default=15, verbose_name=_("خصم نصف سنوي (%)"))
    annual_discount = models.IntegerField(default=20, verbose_name=_("خصم سنوي (%)"))

    max_branches = models.IntegerField(default=1)
    max_users = models.IntegerField(default=1)
    max_treasuries = models.IntegerField(default=1)

    # 🎁 حصة تصاميم AI الشهرية المضمنة في الباقة (تتجدد كل شهر تلقائياً)
    # — 550 ج = 50 تصميم | باقة 2 = 100 | باقة 3 = 300 (طلب الإمبراطورية)
    monthly_ai_designs_quota = models.IntegerField(
        default=0,
        verbose_name=_("حصة تصاميم AI شهرياً"),
        help_text=_("عدد التصاميم المجانية التي تحصل عليها الشركة شهرياً مع الاشتراك"),
    )

    # 🔍 حصة فحوصات صفحة التشخيص الشهرية (Silver=10, Gold=40, Empire=70)
    monthly_diagnostics_scans_quota = models.IntegerField(
        default=0,
        verbose_name=_("حصة فحوصات التشخيص شهرياً"),
        help_text=_("عدد الفحوصات الناجحة في صفحة التشخيص شهرياً"),
    )
    # 🤖 حصة محادثات بوت التشخيص الشهرية (Silver=20, Gold=40, Empire=70)
    monthly_diagnostics_bot_quota = models.IntegerField(
        default=0,
        verbose_name=_("حصة أسئلة بوت التشخيص شهرياً"),
        help_text=_("عدد أسئلة بوت التشخيص الذكي شهرياً"),
    )

    # 📝 Legacy: قائمة marketing copy strings للعرض على صفحة الـ pricing.
    # ⚠️ مش بتـ gate أي سلوك — ده لـ display بس. الـ behavior gating
    # بيتم عبر entitlements الجديد. يبقى موجود لـ backward compat.
    features = models.JSONField(default=list, blank=True, verbose_name=_("المميزات (عرض)"))

    # 🎯 Phase 1: Hybrid entitlements — dict of feature_code → config
    # Shape:
    # {
    #   "workshop_repair_cards": {"enabled": True, "monthly_limit": 150},
    #   "b2b_marketplace":       {"enabled": True},
    #   "reports_advanced":      {"enabled": False},
    # }
    # كل code لازم يكون موجود في Feature catalog (validated في clean()).
    entitlements = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("الصلاحيات (entitlements)"),
        help_text=_("dict من feature_code → {enabled: bool, monthly_limit?: int}"),
    )

    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        verbose_name = _("باقة اشتراك")
        verbose_name_plural = _("💎 باقات الاشتراك")
        ordering = ['sort_order']

    def __str__(self):
        return f"{self.name} — {self.monthly_price} ج.م/شهر"

    def clean(self):
        """يتحقق إن كل code في entitlements موجود في Feature catalog،
        وإن الـ shape (enabled/monthly_limit) صحيحة. يتنادى من admin UI
        قبل الـ save فـ المستخدم بيشوف validation error مفصل."""
        super().clean()
        from django.core.exceptions import ValidationError
        if not isinstance(self.entitlements, dict):
            raise ValidationError({'entitlements': _("entitlements لازم تكون dict، مش list/string.")})

        if not self.entitlements:
            return  # فاضي = OK، بنـ default للـ Feature catalog defaults

        # نتحقق من الـ catalog الحالي. بنـ import لازم هنا لتجنب circular import.
        valid_codes = set(Feature.objects.filter(is_active=True).values_list('code', flat=True))
        quantitative_codes = set(
            Feature.objects.filter(is_active=True, is_quantitative=True).values_list('code', flat=True)
        )

        errors = []
        for code, config in self.entitlements.items():
            if code not in valid_codes:
                errors.append(f"'{code}' مش feature معروف في الـ catalog")
                continue
            if not isinstance(config, dict):
                errors.append(f"'{code}': القيمة لازم تكون dict")
                continue
            if 'enabled' in config and not isinstance(config['enabled'], bool):
                errors.append(f"'{code}.enabled' لازم تكون True/False")
            if 'monthly_limit' in config:
                if code not in quantitative_codes:
                    errors.append(f"'{code}' مش quantitative — مش هياخد monthly_limit")
                elif not isinstance(config['monthly_limit'], int) or config['monthly_limit'] < 0:
                    errors.append(f"'{code}.monthly_limit' لازم تكون رقم صحيح ≥ 0")

        if errors:
            raise ValidationError({'entitlements': errors})

    def price_for_period(self, months):
        if months >= 12:
            discount = self.annual_discount
        elif months >= 6:
            discount = self.semi_annual_discount
        elif months >= 3:
            discount = self.quarterly_discount
        else:
            discount = 0
        total = self.monthly_price * months
        return (total * (100 - discount) / 100).quantize(Decimal('0.01'))


# =====================================================================
# 📜 7c. PlanRevision — Append-only audit log لتغييرات الـ Plan
# =====================================================================
# كل ما الـ Plan يتعدّل (سعر أو entitlements)، signal بيـ create row جديد
# هنا. الـ TenantSubscription.locked_plan_revision بيـ reference revision
# معين كـ "ده الإصدار اللي العميل اشترك عليه".
#
# على مستوى الـ business: السوبر أدمن يقدر يـ rollback الـ Plan لـ revision
# قديم، يقارن الفرق بين revisions، ويعمل audit trail كامل لكل تغيير سعر.
# =====================================================================
class PlanRevision(models.Model):
    plan = models.ForeignKey(
        Plan, on_delete=models.PROTECT, related_name='revisions',
        verbose_name=_("الباقة"),
    )
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("السعر الشهري"))
    entitlements = models.JSONField(default=dict, blank=True, verbose_name=_("الصلاحيات"))

    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='plan_revisions_authored',
        verbose_name=_("غيّر بواسطة"),
    )
    change_reason = models.CharField(max_length=250, blank=True, verbose_name=_("سبب التغيير"))
    effective_from = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الفعل"))

    class Meta:
        verbose_name = _("revision باقة")
        verbose_name_plural = _("📜 سجل تعديلات الباقات")
        ordering = ['-effective_from']
        indexes = [
            models.Index(fields=['plan', '-effective_from']),
        ]

    def __str__(self):
        return f"{self.plan.slug} @ {self.monthly_price} ج.م ({self.effective_from:%Y-%m-%d %H:%M})"

    @classmethod
    def latest_for(cls, plan):
        """Helper: أحدث revision لـ plan معين."""
        return cls.objects.filter(plan=plan).order_by('-effective_from').first()

    @classmethod
    def create_from_plan(cls, plan, changed_by=None, change_reason=''):
        """ينشئ revision جديد بـ snapshot من الـ plan الحالي.
        الـ signal و الـ admin بـ يـ call ده مباشرة."""
        return cls.objects.create(
            plan=plan,
            monthly_price=plan.monthly_price,
            entitlements=dict(plan.entitlements or {}),
            changed_by=changed_by,
            change_reason=change_reason or '',
        )


# =====================================================================
# 💸 7d. PlatformInvoice — SaaS-level invoices للمنصة
# =====================================================================
# Phase 3: كل tenant بيدفع للمنصة (renewals/upgrades) يـ generate invoice
# هنا. ده مش invoice المبيعات بتاع الـ tenant لعملائه — ده الـ invoice
# اللي الـ MoussTec بتدّيه للـ tenant عشان الـ subscription.
#
# الـ Paymob webhook بيـ create + mark_paid في transaction واحدة، اللي
# بتـ trigger الـ snapshot لـ TenantSubscription.locked_* تلقائياً.
# =====================================================================
class PlatformInvoice(models.Model):
    STATUS_CHOICES = (
        ('issued',   _('مصدرة (لم تُدفع بعد)')),
        ('paid',     _('مدفوعة')),
        ('failed',   _('فشلت المعالجة')),
        ('refunded', _('مردودة')),
        ('void',     _('ملغاة')),
    )

    invoice_number = models.SlugField(
        max_length=30, unique=True, blank=True,
        verbose_name=_("رقم الفاتورة"),
        help_text=_("auto-generated في save() بصيغة INV-YYYY-NNNNNN"),
    )

    tenant = models.ForeignKey(
        Client, on_delete=models.PROTECT, related_name='platform_invoices',
        verbose_name=_("الشركة"),
    )
    subscription = models.ForeignKey(
        'TenantSubscription', on_delete=models.PROTECT, related_name='invoices',
        null=True, blank=True,
        verbose_name=_("الاشتراك"),
    )
    plan_revision = models.ForeignKey(
        PlanRevision, on_delete=models.PROTECT, related_name='invoices',
        verbose_name=_("revision الباقة"),
        help_text=_("الإصدار اللي اتفوتر عليه — مهم للـ audit والـ legal"),
    )

    # ── Period covered ──
    period_start = models.DateField(verbose_name=_("بداية الفترة"))
    period_end = models.DateField(verbose_name=_("نهاية الفترة"))
    billing_cycle_months = models.IntegerField(verbose_name=_("عدد الشهور"))

    # ── Pricing snapshot ──
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("الإجمالي قبل الخصم"))
    discount_percent = models.IntegerField(default=0, verbose_name=_("نسبة الخصم (%)"))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name=_("قيمة الخصم"))
    total = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("الإجمالي المستحق"))
    currency = models.CharField(max_length=3, default='EGP', verbose_name=_("العملة"))

    # ── Entitlements snapshot (immutable record at billing time) ──
    entitlements_snapshot = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("نسخة المزايا وقت الفوترة"),
        help_text=_("immutable — للـ audit والـ legal لو حصل dispute"),
    )

    # ── Payment tracking ──
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='issued', db_index=True)
    payment_provider = models.CharField(max_length=20, blank=True, verbose_name=_("بوابة الدفع"))
    payment_reference = models.CharField(max_length=120, blank=True, db_index=True, verbose_name=_("مرجع الدفع"))
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ الدفع"))

    # ── Metadata ──
    issued_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإصدار"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name=_("أصدرها"),
    )

    class Meta:
        verbose_name = _("فاتورة منصة")
        verbose_name_plural = _("💸 فواتير المنصة (SaaS)")
        ordering = ['-issued_at']
        indexes = [
            models.Index(fields=['tenant', '-issued_at']),
            models.Index(fields=['status', '-issued_at']),
            models.Index(fields=['payment_provider', 'payment_reference']),
        ]
        # 🛡️ منع duplicates من نفس الـ provider بنفس الـ reference (idempotency)
        constraints = [
            models.UniqueConstraint(
                fields=['payment_provider', 'payment_reference'],
                condition=models.Q(payment_reference__gt=''),
                name='unique_provider_payment_ref',
            ),
        ]

    def __str__(self):
        return f"{self.invoice_number or 'INV-pending'} — {self.tenant.name} — {self.total} {self.currency}"

    def save(self, *args, **kwargs):
        # Auto-generate invoice_number بعد ما الـ pk يبقى موجود
        super().save(*args, **kwargs)
        if not self.invoice_number:
            year = (self.issued_at or timezone.now()).year
            self.invoice_number = f"INV-{year}-{self.pk:06d}"
            super().save(update_fields=['invoice_number'])

    # ─────────────────────────────────────────────────────────────────
    # 🎯 الـ business core: mark_paid يـ propagate كل الـ side effects
    # ─────────────────────────────────────────────────────────────────
    def mark_paid(self, *, payment_provider=None, payment_reference=None, paid_at=None):
        """يـ finalize الفاتورة + يـ trigger snapshot + يـ extend الاشتراك.

        Steps:
          1. Update self.status='paid', paid_at, payment_*
          2. Update subscription.plan لو الـ revision.plan مختلف
          3. snapshot_from_plan(revision=self.plan_revision) — yetelock الـ
             entitlements + price على إصدار الفاتورة بالظبط
          4. Extend subscription.current_period_*
          5. Update tenant.subscription_end_date + status='active'

        كله في الـ DB transaction الـ caller (ميـ wrapش في transaction.atomic
        داخلياً عشان الـ caller يـ control الـ scope).
        """
        from django.utils import timezone as _tz
        self.status = 'paid'
        self.paid_at = paid_at or _tz.now()
        if payment_provider:
            self.payment_provider = payment_provider
        if payment_reference:
            self.payment_reference = str(payment_reference)
        self.save(update_fields=['status', 'paid_at', 'payment_provider', 'payment_reference'])

        sub = self.subscription
        if sub is None:
            logger.warning(
                f"[Invoice {self.invoice_number}] paid but no subscription attached — skipping side effects"
            )
            return

        # 1. Update subscription.plan لو اتغير
        target_plan = self.plan_revision.plan
        if sub.plan_id != target_plan.id:
            sub.plan = target_plan
        sub.billing_cycle_months = self.billing_cycle_months
        sub.current_period_start = self.period_start
        sub.current_period_end = self.period_end
        sub.is_active = True
        sub.save()  # Phase 0b TenantSubscription.save() بـ يـ sync Client.max_* لو الـ plan اتغير

        # 2. Snapshot على الـ revision المحدد (مش الـ latest — عشان الـ audit accurate)
        sub.snapshot_from_plan(revision=self.plan_revision, save=True)

        # 3. Extend الـ tenant
        tenant = self.tenant
        tenant.subscription_end_date = self.period_end
        tenant.status = 'active'
        tenant.is_active = True
        tenant.save(update_fields=['subscription_end_date', 'status', 'is_active'])

        logger.info(
            f"💸 [Invoice {self.invoice_number}] paid via {self.payment_provider} "
            f"ref={self.payment_reference} — tenant '{tenant.schema_name}' "
            f"locked @ {sub.locked_monthly_price} until {sub.current_period_end}"
        )



# =====================================================================
# 🤖 8. حزم إضافات الذكاء الاصطناعي (AI Studio Add-ons)
# =====================================================================
class AIAddonPackage(models.Model):
    slug = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80, verbose_name=_("اسم الحزمة"))
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("السعر الشهري (ج.م)"))

    ai_generations_limit = models.IntegerField(default=0, verbose_name=_("حد التوليد بالذكاء الاصطناعي"))
    whatsapp_messages_limit = models.IntegerField(default=0, verbose_name=_("حد رسائل واتساب"))

    features = models.JSONField(default=list, blank=True, verbose_name=_("مميزات الحزمة"))
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        verbose_name = _("حزمة AI إضافية")
        verbose_name_plural = _("🤖 حزم AI Studio")
        ordering = ['sort_order']

    def __str__(self):
        return f"{self.name} — {self.monthly_price} ج.م/شهر"


# =====================================================================
# 🔧 8.5 حزمة Smart Diagnostics (Add-on) — تنضاف لأي باقة موجودة
# =====================================================================
# الفلسفة: الـ Smart Diagnostics مش باقة مستقلة بـ تـ replace الـ plan
# الأساسي للشركة. بدل كده، هي **add-on** يتـ activate على tenant
# عنده Empire/Gold/Pro/أي حاجة. الـ effective_entitlements بتـ merge:
#   plan.entitlements ∪ diagnostics_addon.entitlements
# عشان الفني/المركز يستفيد من كل مميزات باقته + الـ diagnostics.
# =====================================================================
class DiagnosticsAddon(models.Model):
    slug = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=120, verbose_name=_("اسم الحزمة"))
    monthly_price = models.DecimalField(
        max_digits=10, decimal_places=2,
        verbose_name=_("السعر الشهري (ج.م)"),
    )

    monthly_api_quota = models.IntegerField(
        default=200,
        verbose_name=_("حصة فحوصات API الخارجية شهرياً"),
        help_text=_("بـ يـ refill diag_api_quota_remaining في 1st كل شهر"),
    )

    # الميزات المفعّلة في الـ addon ده (feature codes من Feature catalog)
    entitlements = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("الصلاحيات (entitlements)"),
        help_text=_("dict من feature_code → {enabled: bool, monthly_limit?: int}"),
    )

    features = models.JSONField(
        default=list, blank=True,
        verbose_name=_("مميزات الحزمة (للعرض على الـ pricing page)"),
    )

    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        verbose_name = _("حزمة Smart Diagnostics")
        verbose_name_plural = _("🔧 حزم Smart Diagnostics (Add-ons)")
        ordering = ['sort_order']

    def __str__(self):
        return f"🔧 {self.name} — {self.monthly_price} ج.م/شهر"


# =====================================================================
# 🔍 8.6 حزمة شحن تشخيص (Top-Up) — شراء لمرة واحدة فوق أي باقة
# =====================================================================
# العميل يخلص حصته الشهرية من الفحوصات/البوت؟ يشتري حزمة شحن.
# الافتراضي: 150 ج.م = 30 استخدام (فحص أو بوت).
# =====================================================================
class DiagnosticsTopUpPack(models.Model):
    slug = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=120, verbose_name=_("اسم الحزمة"))
    price_egp = models.DecimalField(
        max_digits=10, decimal_places=2, verbose_name=_("السعر (ج.م)"),
    )
    uses_granted = models.IntegerField(
        verbose_name=_("عدد الاستخدامات"),
        help_text=_("استخدام = فحص واحد أو سؤال بوت واحد"),
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        verbose_name = _("حزمة شحن تشخيص")
        verbose_name_plural = _("🔍 حزم شحن التشخيص")
        ordering = ['sort_order', 'price_egp']

    def __str__(self):
        return f"🔍 {self.name} — {self.uses_granted} × {self.price_egp} ج.م"


# =====================================================================
# 📋 9. اشتراك المستأجر (Tenant Subscription)
# =====================================================================
class TenantSubscription(models.Model):
    tenant = models.OneToOneField(Client, on_delete=models.CASCADE, related_name='subscription', verbose_name=_("المستأجر"))
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, null=True, blank=True, verbose_name=_("الباقة"))
    ai_addon = models.ForeignKey(AIAddonPackage, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("حزمة AI"))
    diagnostics_addon = models.ForeignKey(
        DiagnosticsAddon, on_delete=models.SET_NULL,
        null=True, blank=True,
        verbose_name=_("🔧 حزمة Smart Diagnostics"),
        help_text=_("اختياري — يفعّل ميزات التشخيص الذكي فوق الباقة الأساسية"),
    )

    billing_cycle_months = models.IntegerField(default=1, verbose_name=_("دورة الفوترة (أشهر)"))
    current_period_start = models.DateField(null=True, blank=True)
    current_period_end = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("اشتراك مستأجر")
        verbose_name_plural = _("📋 اشتراكات المستأجرين")

    def __str__(self):
        plan_name = self.plan.name if self.plan else 'بدون باقة'
        return f"{self.tenant.name} — {plan_name}"

    # ─────────────────────────────────────────────────────────────────
    # 🎯 Phase 2: Grandfathering snapshots — كل tenant بيـ lock
    # السعر والمزايا وقت الاشتراك/التجديد. تعديل Plan لاحقاً ميـ
    # affect الـ tenant الموجود لحد الـ next renewal (الـ default)
    # إلا لو SuperAdmin عمل force-apply صراحةً.
    # ─────────────────────────────────────────────────────────────────
    locked_monthly_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        verbose_name=_("السعر المثبت (snapshot)"),
        help_text=_("نسخة من Plan.monthly_price وقت الاشتراك/التجديد"),
    )
    locked_entitlements = models.JSONField(
        default=dict, blank=True,
        verbose_name=_("الصلاحيات المثبتة (snapshot)"),
        help_text=_("نسخة من Plan.entitlements وقت الاشتراك/التجديد"),
    )
    locked_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name=_("تاريخ تثبيت الـ snapshot"),
    )
    locked_plan_revision = models.ForeignKey(
        'PlanRevision', on_delete=models.PROTECT, null=True, blank=True,
        related_name='locked_subscriptions',
        verbose_name=_("revision المرجعية"),
        help_text=_("للـ audit — أي إصدار من الـ plan كان وقت الـ snapshot"),
    )

    # ─────────────────────────────────────────────────────────────────
    # 🔧 Smart Diagnostics — external API budget (pay-per-call)
    # ─────────────────────────────────────────────────────────────────
    # حصة الـ external DTC/VIN API calls المتبقية للشهر الحالي.
    # بـ تتدفع 1 لكل scan جديد بـ يخبط الـ external provider (CarMD وأمثاله).
    # الـ cache hits ميـ deductش. الـ refill بـ يحصل من admin action أو cron.
    diag_api_quota_remaining = models.IntegerField(
        default=0,
        verbose_name=_("حصة فحوصات الـ API المتبقية"),
        help_text=_("بـ تتدفع 1 لكل external API call يـ miss الـ cache. الـ refill شهرياً أو يدوياً."),
    )
    diag_api_scans_used_total = models.IntegerField(
        default=0,
        verbose_name=_("إجمالي فحوصات API المستهلكة (lifetime)"),
    )
    diag_api_last_refill_at = models.DateTimeField(null=True, blank=True)

    # ─────────────────────────────────────────────────────────────────
    # 🔍 2026 Relaunch — per-tenant monthly diagnostics quota tracking
    # ─────────────────────────────────────────────────────────────────
    # Plan defines the *limit* (monthly_diagnostics_scans_quota /
    # monthly_diagnostics_bot_quota); these fields track the current
    # period's *consumption*. Top-up balance is a separate pool used
    # only after the monthly allowance is fully consumed.
    diag_scans_used_this_period = models.IntegerField(
        default=0,
        verbose_name=_("فحوصات التشخيص المستهلكة (الفترة الحالية)"),
    )
    diag_bot_used_this_period = models.IntegerField(
        default=0,
        verbose_name=_("أسئلة بوت التشخيص المستهلكة (الفترة الحالية)"),
    )
    diag_topup_balance = models.IntegerField(
        default=0,
        verbose_name=_("رصيد شحن التشخيص (استخدامات إضافية)"),
        help_text=_("استخدامات إضافية فوق حصة الباقة الشهرية، تنزل عند انتهاء الحصة الشهرية."),
    )
    diag_period_start = models.DateField(
        null=True, blank=True,
        verbose_name=_("بداية فترة حصة التشخيص الحالية"),
    )

    def refill_diag_api_quota(self, amount: int, reset: bool = False):
        """يـ refill الـ scan quota. amount > 0 = إضافة؛ reset=True يـ set مباشرة."""
        from django.utils import timezone as _tz
        if reset:
            self.diag_api_quota_remaining = max(0, int(amount))
        else:
            self.diag_api_quota_remaining = max(0, self.diag_api_quota_remaining + int(amount))
        self.diag_api_last_refill_at = _tz.now()
        self.save(update_fields=[
            'diag_api_quota_remaining', 'diag_api_last_refill_at',
        ])

    # ─────────────────────────────────────────────────────────────────
    # 🛡️ Phase 0b: مزامنة مركزية للـ limits — Client.max_* تتحدث
    # تلقائياً لما الـ subscription.plan يتغير. ده الـ source of truth
    # الوحيد للـ copying logic؛ مفيش كود تاني في النظام مسموح يكتب
    # على Client.max_* مباشرة (شُيلت من admin.py:257,648,723,748).
    # ─────────────────────────────────────────────────────────────────
    def save(self, *args, **kwargs):
        # هل الـ plan FK اتغير؟ (يشمل أول save لما يكون _state.adding)
        plan_changed = False
        if self._state.adding:
            plan_changed = self.plan_id is not None
        elif self.pk:
            old_plan_id = (
                type(self).objects.filter(pk=self.pk)
                .values_list('plan_id', flat=True).first()
            )
            if old_plan_id != self.plan_id:
                plan_changed = True

        super().save(*args, **kwargs)

        # Sync بعد الـ save الأساسي عشان نضمن إن الـ FK محفوظ والـ DB consistent.
        if plan_changed and self.plan_id and self.tenant_id:
            self.sync_limits_to_tenant()

    def sync_limits_to_tenant(self):
        """ينقل max_* من Plan إلى Client. يتنادى من save() عند تغيير الـ plan،
        أو يدوياً من admin actions كـ defensive re-sync. **Idempotent** —
        لو القيم متطابقة بالفعل ميـ trigger أي save."""
        tenant = self.tenant
        plan = self.plan
        # نسجل اللي اتغير عشان update_fields يبقى minimal
        changes = {}
        if tenant.max_branches != plan.max_branches:
            changes['max_branches'] = plan.max_branches
        if tenant.max_users != plan.max_users:
            changes['max_users'] = plan.max_users
        if tenant.max_treasuries != plan.max_treasuries:
            changes['max_treasuries'] = plan.max_treasuries

        if not changes:
            return

        for field, value in changes.items():
            setattr(tenant, field, value)
        # update_fields يمنع إعادة تشغيل أي logic تانية في Client.save()
        tenant.save(update_fields=list(changes.keys()))

    # ─────────────────────────────────────────────────────────────────
    # 🎯 Phase 2: Snapshot mechanism — grandfathering
    # ─────────────────────────────────────────────────────────────────
    def snapshot_from_plan(self, revision=None, save=True):
        """يـ copy الـ price والـ entitlements من الـ current Plan إلى الـ
        locked_* fields. يتنادى:
          - عند الاشتراك الأول (signal post-create)
          - عند الـ renewal (من Paymob webhook في Phase 4)
          - عند force-apply من SuperAdmin (admin action في Phase 5)

        Params:
          - revision: PlanRevision لاستخدامها كـ reference (لو None، نـ pick
            الأحدث من الـ plan).
          - save: لو False، نـ mutate self بس بدون DB save (للـ callers اللي
            عاوزين يـ batch مع تغييرات تانية).
        """
        from django.utils import timezone as _tz
        if not self.plan_id:
            return  # مفيش plan = مفيش snapshot
        plan = self.plan

        # نختار الـ revision: المحدد، أو الأحدث من الـ plan
        if revision is None:
            revision = (
                PlanRevision.objects.filter(plan_id=self.plan_id)
                .order_by('-effective_from').first()
            )

        self.locked_monthly_price = plan.monthly_price
        self.locked_entitlements = dict(plan.entitlements or {})
        self.locked_at = _tz.now()
        self.locked_plan_revision = revision

        if save:
            self.save(update_fields=[
                'locked_monthly_price', 'locked_entitlements',
                'locked_at', 'locked_plan_revision',
            ])

    @property
    def effective_entitlements(self) -> dict:
        """ترجع الـ entitlements الـ effective: locked لو موجود، وإلا fallback
        لـ plan.entitlements. الـ source of truth الواحدة للـ EntitlementService.

        🔧 Add-on merging: لو في diagnostics_addon مفعّل على الـ tenant،
        بـ نـ merge الـ entitlements بتاعته فوق الـ base. ده بيخلي الـ tenant
        يحتفظ بـ مميزات باقته الأساسية (Empire/Gold) + يحصل على مميزات
        التشخيص بدون ما يـ swap الـ plan.
        """
        # 1. الـ base entitlements من الـ plan/snapshot
        base = self._base_entitlements()
        # 2. merge أي add-on entitlements (diagnostics_addon حالياً)
        if self.diagnostics_addon_id and self.diagnostics_addon and self.diagnostics_addon.is_active:
            addon_ents = dict(self.diagnostics_addon.entitlements or {})
            # دمج: addon overrides base لو كان فيه conflict على نفس الـ feature
            base = {**base, **addon_ents}
        return base

    def _base_entitlements(self) -> dict:
        """الـ entitlements من الـ plan قبل أي addon merging."""
        if self.locked_at and isinstance(self.locked_entitlements, dict) and self.locked_entitlements:
            return dict(self.locked_entitlements)
        # Fallback: لو الـ snapshot لسة ما اتعملش، نقرأ من الـ plan الحالي
        if self.plan_id:
            return dict(self.plan.entitlements or {})
        return {}

    @property
    def effective_monthly_price(self):
        """نفس الـ logic للسعر."""
        if self.locked_at and self.locked_monthly_price is not None:
            return self.locked_monthly_price
        if self.plan_id:
            return self.plan.monthly_price
        return None


# =====================================================================
# 📊 10. متتبع حصص الذكاء الاصطناعي (AI Limit Tracker)
# =====================================================================
class AIBonusGrant(models.Model):
    """🎁 هدايا التصاميم من السوبر أدمن للشركات (Free design credits)."""
    tenant = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='ai_bonus_grants', verbose_name=_("المستأجر"))
    granted_designs = models.IntegerField(default=0, verbose_name=_("تصاميم مهداه"))
    granted_whatsapp = models.IntegerField(default=0, verbose_name=_("رسائل واتساب مهداه"))
    granted_watermarks = models.IntegerField(default=0, verbose_name=_("علامات مائية مهداه"))
    consumed_designs = models.IntegerField(default=0)
    consumed_whatsapp = models.IntegerField(default=0)
    consumed_watermarks = models.IntegerField(default=0)
    reason = models.CharField(max_length=250, blank=True, verbose_name=_("سبب الهدية"))
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    granted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ الانتهاء (اختياري)"))
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = _("هدية رصيد AI")
        verbose_name_plural = _("🎁 هدايا رصيد AI Studio")
        ordering = ['-granted_at']

    def __str__(self):
        return f"🎁 {self.tenant.name} — {self.granted_designs} تصميم ({self.granted_at:%Y-%m-%d})"

    @property
    def remaining_designs(self):
        return max(self.granted_designs - self.consumed_designs, 0)

    @property
    def remaining_whatsapp(self):
        return max(self.granted_whatsapp - self.consumed_whatsapp, 0)

    @property
    def remaining_watermarks(self):
        return max(self.granted_watermarks - self.consumed_watermarks, 0)

    @property
    def is_valid(self):
        if not self.is_active:
            return False
        if self.expires_at and timezone.now() > self.expires_at:
            return False
        return True


# =====================================================================
# 💳 شحن تصاميم إضافية للشركات (Tenant Design Top-ups)
# =====================================================================
# باقات الشحن الواحدة (one-time purchase) للشركات لما حصة الباقة الشهرية تخلص:
#   1000 تصميم = 250 ج | 2500 = 500 ج | 5000 = 900 ج
# مستقلة عن AIAddonPackage (اللي هو monthly recurring).
class TenantDesignTopUp(models.Model):
    STATUS_CHOICES = (
        ('pending', _('في انتظار الدفع')),
        ('paid', _('مدفوعة — جاهزة للاستخدام')),
        ('exhausted', _('تم استهلاكها بالكامل')),
        ('refunded', _('مردودة')),
        ('expired', _('منتهية الصلاحية')),
    )
    PAYMENT_METHODS = (
        ('paymob', _('Paymob (Visa/Mastercard)')),
        ('vodafone_cash', _('فودافون كاش')),
        ('instapay', _('إنستاباي')),
        ('admin_grant', _('منحة إدارية')),
    )

    purchase_code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    tenant = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='design_topups')

    designs_total = models.IntegerField(verbose_name=_("إجمالي التصاميم"))
    designs_used = models.IntegerField(default=0, verbose_name=_("المستهلك"))
    price_paid = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("السعر المدفوع (ج.م)"))
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS, default='paymob')
    payment_reference = models.CharField(max_length=200, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name=_("ينتهي في (اختياري)"))

    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("شحن تصاميم للشركة")
        verbose_name_plural = _("💳 شحنات تصاميم الشركات")
        ordering = ['-created_at']
        indexes = [models.Index(fields=['tenant', 'status'])]

    def __str__(self):
        return f"TopUp #{self.id} | {self.tenant.name} | {self.designs_total}×"

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
        """خصم تصميم واحد (atomic). يقفل التوب-أب لما يخلص."""
        from django.db import transaction as _tx
        with _tx.atomic():
            type(self).objects.filter(pk=self.pk).update(designs_used=F('designs_used') + 1)
            self.refresh_from_db()
            if self.designs_used >= self.designs_total:
                self.status = 'exhausted'
                self.save(update_fields=['status'])


class AIStudioSession(models.Model):
    """💾 سجل جلسات AI Studio — كل تصميم بيتسجل علشان العميل يرجعله بعدين."""
    tenant = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='ai_sessions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    raw_input = models.TextField(verbose_name=_("النص الأصلي"))
    engineered_prompt = models.TextField(blank=True, verbose_name=_("البرومبت المحسّن"))
    negative_prompt = models.TextField(blank=True)
    design_category = models.CharField(max_length=50, blank=True)

    logo_used = models.BooleanField(default=False, verbose_name=_("استُخدم لوجو؟"))
    logo_image = models.ImageField(upload_to='ai_studio/logos/', blank=True, null=True)

    image_url = models.URLField(max_length=600, blank=True, verbose_name=_("رابط التصميم"))
    image_size = models.CharField(max_length=20, default='1024x1024')
    image_quality = models.CharField(max_length=20, default='hd')
    model_used = models.CharField(max_length=50, blank=True)

    watermarked = models.BooleanField(default=False, verbose_name=_("بعلامة مائية"))
    watermarked_image_url = models.URLField(max_length=600, blank=True)

    sent_to_phone = models.CharField(max_length=30, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    is_favorite = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("جلسة AI Studio")
        verbose_name_plural = _("💾 سجل جلسات AI Studio")
        ordering = ['-created_at']
        indexes = [models.Index(fields=['tenant', '-created_at'])]

    def __str__(self):
        return f"AI-{self.pk} | {self.raw_input[:50]}"


class AILimitTracker(models.Model):
    ACTION_CHOICES = (
        ('ai_generation', _('توليد صورة بالذكاء الاصطناعي')),
        ('whatsapp_send', _('إرسال رسالة واتساب')),
        ('smart_watermark', _('علامة مائية ذكية')),
    )
    tenant = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='ai_usage_log', verbose_name=_("المستأجر"))
    action_type = models.CharField(max_length=30, choices=ACTION_CHOICES, verbose_name=_("نوع العملية"))
    used_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict, blank=True, verbose_name=_("بيانات إضافية"))

    class Meta:
        verbose_name = _("سجل استهلاك AI")
        verbose_name_plural = _("📊 سجل استهلاك AI Studio")
        ordering = ['-used_at']

    def __str__(self):
        return f"{self.tenant.name} — {self.get_action_type_display()} — {self.used_at:%Y-%m-%d %H:%M}"

    @classmethod
    def get_monthly_usage(cls, tenant, action_type):
        """إرجاع عدد العمليات المستهلكة في الشهر الحالي"""
        now = timezone.now()
        return cls.objects.filter(
            tenant=tenant,
            action_type=action_type,
            used_at__year=now.year,
            used_at__month=now.month,
        ).count()

    @classmethod
    def _get_bonus_remaining(cls, tenant, action_type):
        """جمع الرصيد المتبقي من كل هدايا السوبر أدمن"""
        field_map = {
            'ai_generation': ('granted_designs', 'consumed_designs'),
            'whatsapp_send': ('granted_whatsapp', 'consumed_whatsapp'),
            'smart_watermark': ('granted_watermarks', 'consumed_watermarks'),
        }
        if action_type not in field_map:
            return 0
        granted_f, consumed_f = field_map[action_type]
        total = 0
        for grant in tenant.ai_bonus_grants.filter(is_active=True):
            if not grant.is_valid:
                continue
            total += max(getattr(grant, granted_f) - getattr(grant, consumed_f), 0)
        return total

    @classmethod
    def can_use(cls, tenant, action_type):
        """التحقق من أن المستأجر عنده رصيد (إما من الباقة أو من هدية السوبر أدمن)"""
        # First: check bonus quota
        bonus_remaining = cls._get_bonus_remaining(tenant, action_type)
        if bonus_remaining > 0:
            return True

        # Otherwise: check paid subscription
        try:
            sub = tenant.subscription
        except TenantSubscription.DoesNotExist:
            return False
        if not sub.ai_addon:
            return False

        used = cls.get_monthly_usage(tenant, action_type)
        if action_type == 'ai_generation':
            return used < sub.ai_addon.ai_generations_limit
        elif action_type in ('whatsapp_send', 'smart_watermark'):
            return used < sub.ai_addon.whatsapp_messages_limit
        return False

    @classmethod
    def deduct(cls, tenant, action_type, metadata=None):
        """
        خصم رصيد — يخصم من الهدية أولاً، ثم من الباقة المدفوعة.
        🛡️ يستخدم select_for_update + transaction.atomic لمنع race conditions.
        🛡️ [FIX]: أزلنا can_use() المنفصل وعملنا الفحص والخصم في atomic block واحد.
        🛡️ [FIX]: أزلنا skip_locked وأصلحنا grant_id indentation.
        """
        if metadata is None:
            metadata = {}

        field_map = {
            'ai_generation': ('granted_designs', 'consumed_designs'),
            'whatsapp_send': ('granted_whatsapp', 'consumed_whatsapp'),
            'smart_watermark': ('granted_watermarks', 'consumed_watermarks'),
        }
        consumed_from_bonus = False

        with transaction.atomic():
            # Check-and-deduct atomically (no separate can_use)
            if action_type in field_map:
                granted_f, consumed_f = field_map[action_type]
                # 🔒 Lock grants WITHOUT skip_locked to prevent TOCTOU
                grants = (tenant.ai_bonus_grants
                          .select_for_update()
                          .filter(is_active=True)
                          .order_by('granted_at'))
                for grant in grants:
                    if not grant.is_valid:
                        continue
                    remaining = getattr(grant, granted_f) - getattr(grant, consumed_f)
                    if remaining > 0:
                        from django.db.models import F as _F
                        type(grant).objects.filter(pk=grant.pk).update(**{consumed_f: _F(consumed_f) + 1})
                        metadata['source'] = 'bonus_grant'
                        metadata['grant_id'] = grant.pk
                        consumed_from_bonus = True
                        break

            # If not consumed from bonus, verify subscription limit
            if not consumed_from_bonus:
                if not cls.can_use(tenant, action_type):
                    return False
                metadata['source'] = 'subscription'

            cls.objects.create(
                tenant=tenant,
                action_type=action_type,
                metadata=metadata,
            )
        return True


