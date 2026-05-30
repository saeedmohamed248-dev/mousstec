from django.conf import settings
from django.db import models, transaction
from django_tenants.models import TenantMixin, DomainMixin
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
class Client(TenantMixin):
    ADDON_PRICE_PER_MONTH = Decimal('125.00')
    PLAN_BASE_PRICES = {
        'silver': Decimal('685.00'),
        'gold': Decimal('1185.00'),
        'empire': Decimal('3000.00'),
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
        ('silver', _('باقة سيلفر — لمراكز الصيانة وتجار قطع الغيار')),
        ('gold', _('باقة جولد — لمراكز الصيانة وتجار قطع الغيار الشامل')),
        ('empire', _('باقة Empire — لتجار القطع والشركات الكبيرة')),
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
        plan_changed = self._state.adding
        if not plan_changed and self.pk:
            old_plan = Client.objects.filter(pk=self.pk).values_list('plan', flat=True).first()
            if old_plan and old_plan != self.plan:
                plan_changed = True

        if plan_changed:
            if self.plan == 'silver':
                self.max_branches, self.max_users, self.max_treasuries = 1, 1, 1
                self.max_repair_cards, self.max_inventory_items = 150, 500
            elif self.plan == 'gold':
                self.max_branches, self.max_users, self.max_treasuries = 2, 4, 2
                self.max_repair_cards, self.max_inventory_items = 0, 0
            elif self.plan == 'empire':
                self.max_branches, self.max_users, self.max_treasuries = 999, 9999, 999
                self.max_repair_cards, self.max_inventory_items = 0, 0

        super().save(*args, **kwargs)

# =====================================================================
# 🌐 2. جدول النطاقات
# =====================================================================
class Domain(DomainMixin):
    pass

# =====================================================================
# 🛒 3. المؤشر المركزي لسوق التجار (Global B2B Marketplace)
# =====================================================================
class GlobalB2BMarketplace(models.Model):
    CONDITION_CHOICES = (
        ('new', _('جديد (أصلي/بديل)')),
        ('used', _('استيراد / تقطيع')),
        ('core', _('تالف قابل للتجديد (Core)')),
    )
    
    tenant = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='market_products', verbose_name=_("التاجر"))
    part_number = models.CharField(max_length=100, db_index=True, verbose_name=_("رقم القطعة (P/N)"))
    product_name = models.CharField(max_length=200, verbose_name=_("اسم القطعة"))
    brand = models.CharField(max_length=100, default="BMW", verbose_name=_("الماركة"))
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default='new', verbose_name=_("الحالة"))
    
    wholesale_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("سعر الجملة للمراكز"))
    available_qty = models.IntegerField(default=0, verbose_name=_("الكمية المتاحة"))
    
    ai_quality_confidence = models.IntegerField(default=95, verbose_name=_("مؤشر جودة AI"), help_text="مؤشر ذكاء اصطناعي لجودة هذا الصنف من هذا التاجر")
    
    # 🚀 ابتكار تسعيري: تتبع الطلب لتغذية مستشار الـ AI
    demand_hits = models.IntegerField(default=0, help_text="عدد مرات البحث/الطلب على هذه القطعة")
    last_sold_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True, help_text="آخر سعر تم الترسية به")
    
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("آخر تحديث للمخزون"))

    class Meta:
        verbose_name = _("صنف في السوق المركزي")
        verbose_name_plural = _("🛒 سوق التجار المركزي")
        unique_together = ('tenant', 'part_number', 'condition') 

    def __str__(self):
        return f"{self.part_number} - {self.tenant.name} ({self.wholesale_price} ج.م)"

# =====================================================================
# ⚖️ 4. محرك المزاد العكسي والترسية الذكية (AI Blind Bidding Engine)
# =====================================================================
class BlindBiddingRequest(models.Model):
    STATUS_CHOICES = (
        ('open', _('مفتوح لتلقي العروض')),
        ('awarding', _('جاري الترسية الآلية')),
        ('escrow_held', _('تم الترسية (الفلوس في الضمان)')),
        ('shipped', _('تم الشحن / جاري الاستلام')),
        ('completed', _('مكتمل (تم تحويل الأموال للتاجر)')),
        ('disputed', _('متنازع عليه (صورة غير مطابقة)')),
        ('cancelled', _('ملغي')),
    )
    
    request_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    buyer = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='bids_created', verbose_name=_("المركز الطالب"))
    part_number = models.CharField(max_length=100, verbose_name=_("القطعة المطلوبة"))
    required_qty = models.IntegerField(default=1, verbose_name=_("الكمية المطلوبة"))
    target_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True, verbose_name=_("السعر المستهدف (مخفي)"))
    
    auto_award = models.BooleanField(default=False, verbose_name=_("ترسية آلية لأفضل عرض؟"))
    
    ai_recommended_winner = models.ForeignKey('BidOffer', on_delete=models.SET_NULL, blank=True, null=True, related_name='recommended_for', help_text="أفضل عرض رشحه الـ AI")
    
    winner = models.ForeignKey(Client, on_delete=models.SET_NULL, blank=True, null=True, related_name='bids_won', verbose_name=_("التاجر الفائز"))
    winning_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True, verbose_name=_("سعر الترسية"))
    platform_fee_collected = models.DecimalField(max_digits=10, decimal_places=2, default=0.00, verbose_name=_("عمولة المنصة المحصلة"))
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open', verbose_name=_("الحالة"))
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(verbose_name=_("ينتهي العطاء في"))

    class Meta:
        verbose_name = _("طلب شراء / مزاد عكسي")
        verbose_name_plural = _("⚖️ مزادات الـ Blind Bidding")

    def __str__(self):
        return f"Bid #{self.id} - {self.part_number} (By: {self.buyer.name})"

    def trigger_escrow_hold(self):
        if self.status != 'open':
            raise ValidationError("المزاد ليس في الحالة المفتوحة للتجميد المالي.")
        if not self.winning_price:
            raise ValidationError("يجب تحديد سعر الترسية النهائي لخصم الضمان.")
        
        with transaction.atomic():
            self.status = 'escrow_held'
            self.save(update_fields=['status'])
            
            EscrowLedger.objects.create(
                client=self.buyer,
                bidding_request=self,
                transaction_type='hold',
                amount=self.winning_price,
                description=f"🔒 تجميد مالي مؤقت لثمن قطعة {self.part_number} بالمزاد العكسي #{self.id}"
            )

    def trigger_release_to_seller(self):
        if self.status != 'shipped':
            raise ValidationError("لا يمكن تحرير الضمان المالي إلا بعد إتمام عملية الشحن والتسليم.")
        if not self.winner or not self.winning_price:
            raise ValidationError("بيانات التاجر الفائز غير مكتملة.")
            
        with transaction.atomic():
            self.status = 'completed'
            fee = (self.winning_price * self.buyer.platform_fee_rate) / Decimal('100.00')
            self.platform_fee_collected = fee
            self.save(update_fields=['status', 'platform_fee_collected'])
            
            # تحديث سعر بيع القطعة في السوق المركزي وتغذية رادار الـ AI
            GlobalB2BMarketplace.objects.filter(
                tenant=self.winner, part_number=self.part_number
            ).update(last_sold_price=self.winning_price, demand_hits=F('demand_hits') + 1)
            
            # 🚀 ابتكار: رفع الثقة آلياً للتاجر (Gamification)
            Client.objects.filter(pk=self.winner.pk).update(
                successful_deals=F('successful_deals') + 1,
                ai_trust_score=F('ai_trust_score') + 2
            )
            
            EscrowLedger.objects.create(
                client=self.buyer,
                bidding_request=self,
                transaction_type='release',
                amount=self.winning_price,
                description=f"💸 إفراج مالي لثمن قطعة {self.part_number} للتاجر {self.winner.name}"
            )
            
            if fee > 0:
                EscrowLedger.objects.create(
                    client=self.winner,
                    bidding_request=self,
                    transaction_type='fee_deduction',
                    amount=fee,
                    description=f"⚙️ خصم عمولة Mouss Tec عن المزاد #{self.id}"
                )

    def trigger_refund_to_buyer(self):
        if self.status not in ['escrow_held', 'disputed']:
            raise ValidationError("لا يمكن رد المبالغ المجمّدة في هذه المرحلة.")
            
        with transaction.atomic():
            self.status = 'cancelled'
            self.save(update_fields=['status'])
            
            # 🚀 ابتكار: خصم نقاط ثقة قاسية من التاجر بسبب التلاعب أو إرجاع القطعة
            if self.winner:
                Client.objects.filter(pk=self.winner.pk).update(
                    ai_trust_score=F('ai_trust_score') - 10,
                    dispute_rate=F('dispute_rate') + Decimal('1.5')
                )
            
            EscrowLedger.objects.create(
                client=self.buyer,
                bidding_request=self,
                transaction_type='refund',
                amount=self.winning_price,
                description=f"🔄 رد الرصيد المجمد لإلغاء المزاد أو ربح النزاع الفني."
            )

# =====================================================================
# 📥 5. جدول عروض الأسعار (Bid Offers)
# =====================================================================
class BidOffer(models.Model):
    bidding_request = models.ForeignKey(BlindBiddingRequest, on_delete=models.CASCADE, related_name='offers', verbose_name=_("طلب المزاد"))
    seller = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='offers_made', verbose_name=_("التاجر مقدم العرض"))
    
    offer_price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name=_("سعر العرض"))
    condition = models.CharField(max_length=20, choices=GlobalB2BMarketplace.CONDITION_CHOICES, default='new', verbose_name=_("حالة القطعة المعروضة"))
    estimated_delivery_days = models.IntegerField(default=1, verbose_name=_("أيام التوصيل المتوقعة"))
    
    ai_match_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, help_text="تقييم الـ AI الشامل لهذا العرض")
    is_winner = models.BooleanField(default=False, verbose_name=_("هل هو العرض الفائز؟"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("عرض سعر")
        verbose_name_plural = _("عروض أسعار التجار")
        unique_together = ('bidding_request', 'seller') 

    def __str__(self):
        return f"Offer by {self.seller.name} for Bid #{self.bidding_request.id} - {self.offer_price} EGP"

# =====================================================================
# 🏦 6. دفتر الأستاذ المالي (Immutable Escrow Ledger)
# =====================================================================
class EscrowLedger(models.Model):
    TRANSACTION_TYPES = (
        ('deposit', _('إيداع في المحفظة')),
        ('hold', _('تجميد أموال (دخول مزاد)')),
        ('release', _('تحرير أموال (استلام البضاعة)')),
        ('refund', _('استرداد أموال (إلغاء/نزاع)')),
        ('fee_deduction', _('خصم عمولة المنصة')),
        ('withdrawal', _('سحب للأرباح خارج المنصة')),
    )
    
    transaction_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name='ledger_entries', verbose_name=_("الشركة/المركز"))
    bidding_request = models.ForeignKey(BlindBiddingRequest, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("مرتبط بمزاد"))
    
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES, verbose_name=_("نوع الحركة"))
    amount = models.DecimalField(max_digits=15, decimal_places=2, verbose_name=_("المبلغ"))
    
    description = models.CharField(max_length=255, verbose_name=_("البيان"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("حركة مالية (Escrow)")
        verbose_name_plural = _("🏦 دفتر الأستاذ المالي (Ledger)")
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_transaction_type_display()} - {self.amount} EGP ({self.client.name})"


# =====================================================================
# 💎 7. باقات الاشتراك الموحدة (Unified Subscription Plans)
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

    features = models.JSONField(default=list, blank=True, verbose_name=_("المميزات"))
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        verbose_name = _("باقة اشتراك")
        verbose_name_plural = _("💎 باقات الاشتراك")
        ordering = ['sort_order']

    def __str__(self):
        return f"{self.name} — {self.monthly_price} ج.م/شهر"

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
# 📋 9. اشتراك المستأجر (Tenant Subscription)
# =====================================================================
class TenantSubscription(models.Model):
    tenant = models.OneToOneField(Client, on_delete=models.CASCADE, related_name='subscription', verbose_name=_("المستأجر"))
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, null=True, blank=True, verbose_name=_("الباقة"))
    ai_addon = models.ForeignKey(AIAddonPackage, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("حزمة AI"))

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


# =====================================================================
# 🧠 الإشارات المحاسبية المؤتمتة (Bank-Grade FinTech Ledger Signals)
# =====================================================================

@receiver(pre_save, sender=EscrowLedger)
def validate_escrow_balance_before_save(sender, instance, **kwargs):
    """
    🛡️ فحص الرصيد قبل الحفظ — يمنع إنشاء سجل ledger بدون رصيد كافٍ.
    """
    if instance.pk:
        return  # Only validate new entries
    with transaction.atomic():
        if instance.transaction_type == 'hold':
            client = Client.objects.select_for_update().get(pk=instance.client_id)
            if client.wallet_balance < instance.amount:
                raise ValidationError("الرصيد المتاح لا يكفي لتجميد ثمن المزاد.")
        elif instance.transaction_type == 'withdrawal':
            client = Client.objects.select_for_update().get(pk=instance.client_id)
            if client.wallet_balance < instance.amount:
                raise ValidationError("الرصيد المتاح للسحب أقل من المبلغ المطلوب.")


@receiver(post_save, sender=EscrowLedger)
def update_client_balances_on_ledger_entry(sender, instance, created, **kwargs):
    """
    🚀 تحديث الأرصدة بعد حفظ سجل الـ Ledger — باستخدام F() الذرية.
    الفحص على الرصيد يتم في pre_save لمنع حفظ سجلات بدون رصيد.
    """
    if created:
        with transaction.atomic():
            client_id = instance.client_id
            amount = instance.amount

            if instance.transaction_type == 'deposit':
                Client.objects.filter(pk=client_id).update(wallet_balance=F('wallet_balance') + amount)
                logger.info(f"[FINTECH ACC]: Deposited {amount} EGP to client ID {client_id}.")

            elif instance.transaction_type == 'hold':
                Client.objects.filter(pk=client_id).update(
                    wallet_balance=F('wallet_balance') - amount,
                    escrow_held=F('escrow_held') + amount
                )
                logger.info(f"[FINTECH ACC]: Frozen {amount} EGP into escrow from ID {client_id}.")

            elif instance.transaction_type == 'release':
                Client.objects.filter(pk=client_id).update(escrow_held=F('escrow_held') - amount)

                if instance.bidding_request and instance.bidding_request.winner_id:
                    seller_id = instance.bidding_request.winner_id
                    fee = instance.bidding_request.platform_fee_collected
                    Client.objects.filter(pk=seller_id).update(wallet_balance=F('wallet_balance') + (amount - fee))
                    logger.info(f"[FINTECH ACC]: Released {amount - fee} EGP to seller ID {seller_id}.")

            elif instance.transaction_type == 'refund':
                Client.objects.filter(pk=client_id).update(
                    escrow_held=F('escrow_held') - amount,
                    wallet_balance=F('wallet_balance') + amount
                )
                logger.info(f"[FINTECH ACC]: Refunded {amount} EGP back to ID {client_id}.")

            elif instance.transaction_type == 'withdrawal':
                Client.objects.filter(pk=client_id).update(wallet_balance=F('wallet_balance') - amount)
                logger.info(f"[FINTECH ACC]: Withdrawn {amount} EGP for ID {client_id}.")


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
# 🛍️ سوق العملاء والمناقصات المجهولة (Customer Marketplace & Blind Tenders)
# =====================================================================

class MarketplaceCustomer(models.Model):
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

    # Auth — OTP-based, no password
    otp_code = models.CharField(max_length=6, blank=True)
    otp_expires_at = models.DateTimeField(null=True, blank=True)
    is_verified = models.BooleanField(default=False, verbose_name=_("تم التحقق من الموبايل"))
    session_token = models.UUIDField(default=uuid.uuid4, unique=True)

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
        # Auto-assign free trial on first save (new registration)
        if not self.pk and self.free_designs_total == 0:
            if self.customer_type == 'company':
                self.free_designs_total = 4
            else:
                self.free_designs_total = 2

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


class ServiceRequest(models.Model):
    """
    طلب خدمة / منتج من عميل — المناقصة الأساسية.
    يظهر لكل التجار المنتمين لنفس القطاع بشكل مجهول.
    """
    STATUS_CHOICES = (
        ('open', _('مفتوح — في انتظار العروض')),
        ('reviewing', _('جاري مراجعة العروض')),
        ('accepted', _('تم قبول عرض')),
        ('completed', _('مكتمل — تم التقييم')),
        ('expired', _('منتهي الصلاحية')),
        ('cancelled', _('ملغي بواسطة العميل')),
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

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open', db_index=True)
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
        # Customer packages
        ('single', _('🎯 تصميم واحد')),
        ('cust_2', _('👤 باقة 2 تصميم')),
        ('cust_4', _('👤 باقة 4 تصاميم')),
        ('cust_8', _('👤 باقة 8 تصاميم')),
        # Designer packages
        ('des_15', _('🎨 باقة 15 تصميم')),
        ('des_25', _('🎨 باقة 25 تصميم')),
        ('des_50', _('🎨 باقة 50 تصميم')),
        ('des_100', _('🎨 باقة 100 تصميم')),
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
        ('social_post', _('بوست سوشيال ميديا')),
        ('flyer', _('فلاير')),
        ('poster', _('بوستر / إعلان')),
        ('banner', _('بنر / Roll-up')),
        ('packaging', _('تغليف / منتج')),
        ('tshirt', _('تيشرت / Merch')),
        ('menu', _('منيو')),
        ('invitation', _('دعوة / كارت')),
        ('sticker', _('ستيكر')),
        ('mockup', _('Mockup منتج')),
        ('other', _('أخرى')),
    )
    OUTPUT_FORMATS = (
        ('png', 'PNG'),
        ('jpg', 'JPEG'),
        ('webp', 'WebP'),
        ('pdf', 'PDF Print-Ready'),
    )
    SIZE_PRESETS = (
        ('1024x1024', '🟦 مربع — 1024×1024 (سوشيال ميديا)'),
        ('1024x1792', '📱 قصة — 1024×1792 (Story / Reel)'),
        ('1792x1024', '🖥️ أفقي — 1792×1024 (بنر / غلاف)'),
        ('2048x2048', '⚡ مربع HD — 2048×2048 (طباعة)'),
        ('a4', '📄 A4 — 2480×3508 (فلاير / بوستر)'),
        ('a3', '📑 A3 — 3508×4960 (بوستر كبير)'),
        ('business_card', '💳 كارت بزنس — 1050×600 (3.5×2 بوصة)'),
        ('tshirt_chest', '👕 تيشرت صدر — 3600×4500'),
        ('mug', '☕ ماج — 2700×1080'),
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

    # Result
    image_url = models.URLField(max_length=600, blank=True)
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
            '1024x1024': '1024×1024 px',
            '1024x1792': '1024×1792 px',
            '1792x1024': '1792×1024 px',
            '2048x2048': '2048×2048 px (HD)',
            'a4': '210×297 mm (A4)',
            'a3': '297×420 mm (A3)',
            'business_card': '85×55 mm',
            'tshirt_chest': '30×38 cm',
            'mug': '23×9 cm',
        }
        return size_map.get(self.size_preset, self.size_preset)


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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("طلب طباعة تصميم")
        verbose_name_plural = _("🖨️ طلبات طباعة تصاميم")
        ordering = ['-created_at']

    def __str__(self):
        return f"PRINT-{str(self.request_code)[:8]} | {self.customer.full_name} | {self.get_product_type_display()}"


def seed_default_design_packages():
    """يستدعى من management command أو migration لتأسيس الباقات.

    باقات منفصلة للعملاء الأفراد والمصممين/الشركات.
    كل تصميم له محاولتين إعادة توليد مجانية.
    """
    # إلغاء تفعيل الباقات القديمة
    DesignPackage.objects.filter(slug__in=['starter', 'pro', 'business', 'studio']).update(is_active=False)

    # === باقات العملاء الأفراد ===
    customer_packages = [
        {'slug': 'single', 'name_ar': 'تصميم واحد', 'designs_count': 1,
         'designer_designs_count': 0, 'price_egp': Decimal('10.00'),
         'target_audience': 'customer',
         'free_regenerations_per_design': 2,
         'icon_emoji': '🎯', 'accent_color': '#06b6d4', 'sort_order': 1,
         'description_html': 'جرّب أول تصميم ذكي. جودة عالية مع إعادة توليد مجانية.'},
        {'slug': 'cust_2', 'name_ar': 'باقة 2 تصميم', 'designs_count': 2,
         'designer_designs_count': 0, 'price_egp': Decimal('60.00'),
         'target_audience': 'customer',
         'free_regenerations_per_design': 2,
         'icon_emoji': '✨', 'accent_color': '#fbbf24', 'sort_order': 2,
         'description_html': 'مثالية للبدء. تصميمين بجودة HD وتوصيل واتساب.'},
        {'slug': 'cust_4', 'name_ar': 'باقة 4 تصاميم', 'designs_count': 4,
         'designer_designs_count': 0, 'price_egp': Decimal('110.00'),
         'target_audience': 'customer',
         'free_regenerations_per_design': 2, 'is_featured': True,
         'badge_text': 'الأوفر',
         'icon_emoji': '🔥', 'accent_color': '#ec4899', 'sort_order': 3,
         'allows_whatsapp_delivery': True,
         'description_html': 'أفضل قيمة! كارت + لوجو + سوشيال ميديا + فلاير.'},
        {'slug': 'cust_8', 'name_ar': 'باقة 8 تصاميم', 'designs_count': 8,
         'designer_designs_count': 0, 'price_egp': Decimal('200.00'),
         'target_audience': 'customer',
         'free_regenerations_per_design': 2,
         'icon_emoji': '💎', 'accent_color': '#8b5cf6', 'sort_order': 4,
         'allows_whatsapp_delivery': True, 'allows_logo_upload': True,
         'description_html': 'هوية بصرية كاملة لمشروعك. لوجو + كروت + سوشيال.'},
    ]
    # === باقات المصممين والشركات ===
    designer_packages = [
        {'slug': 'des_15', 'name_ar': 'باقة 15 تصميم', 'designs_count': 15,
         'designer_designs_count': 0, 'price_egp': Decimal('185.00'),
         'target_audience': 'designer',
         'free_regenerations_per_design': 2,
         'icon_emoji': '🎨', 'accent_color': '#06b6d4', 'sort_order': 10,
         'description_html': 'للمصمم المبتدئ. 15 تصميم بجودة احترافية.'},
        {'slug': 'des_25', 'name_ar': 'باقة 25 تصميم', 'designs_count': 25,
         'designer_designs_count': 0, 'price_egp': Decimal('285.00'),
         'target_audience': 'designer',
         'free_regenerations_per_design': 2, 'is_featured': True,
         'badge_text': 'الأكثر طلباً',
         'icon_emoji': '🚀', 'accent_color': '#ec4899', 'sort_order': 11,
         'allows_whatsapp_delivery': True, 'allows_logo_upload': True,
         'description_html': 'الأنسب للمصمم المحترف. جودة فائقة + توصيل واتساب.'},
        {'slug': 'des_50', 'name_ar': 'باقة 50 تصميم', 'designs_count': 50,
         'designer_designs_count': 0, 'price_egp': Decimal('500.00'),
         'target_audience': 'designer',
         'free_regenerations_per_design': 2,
         'icon_emoji': '⚡', 'accent_color': '#facc15', 'sort_order': 12,
         'allows_whatsapp_delivery': True, 'allows_logo_upload': True,
         'allows_watermark': True,
         'description_html': 'للاستوديوهات. علامة مائية + لوجو + جودة فائقة.'},
        {'slug': 'des_100', 'name_ar': 'باقة 100 تصميم', 'designs_count': 100,
         'designer_designs_count': 0, 'price_egp': Decimal('900.00'),
         'target_audience': 'designer',
         'free_regenerations_per_design': 2,
         'icon_emoji': '👑', 'accent_color': '#8b5cf6', 'sort_order': 13,
         'allows_whatsapp_delivery': True, 'allows_logo_upload': True,
         'allows_watermark': True, 'allows_source_files': True,
         'quality_level': 'ultra', 'resolution_max': '4096x4096',
         'badge_text': 'أقوى باقة',
         'description_html': 'للوكالات الكبرى. كل المزايا + ملفات مصدر + أعلى دقة.'},
    ]
    for d in customer_packages + designer_packages:
        DesignPackage.objects.update_or_create(slug=d['slug'], defaults=d)
