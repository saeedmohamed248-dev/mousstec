from django.db import models, transaction
from django_tenants.models import TenantMixin, DomainMixin
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save
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
    
    BUSINESS_TYPE_CHOICES = (
        ('service_center', _('مركز صيانة متكامل')),
        ('parts_dealer', _('تاجر قطع غيار (مبيعات تجزئة وجملة)')),
        ('scrap_importer', _('مستورد تقطيع وأنصاف (محرك الـ Scrap)')), 
        ('both', _('توكيل شامل (صيانة + تجارة + استيراد)')),
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
        ('silver', _('باقة الصانع - لمراكز الصيانة وتجار قطع الغيار')),
        ('gold', _('باقة المركز - لمراكز الصيانة وتجار قطع الغيار الشامل')),
        ('empire', _('باقة الشبكة - لتجار القطع والشركات الكبيرة')),
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
        return f"{self.name} ({self.get_plan_display()})"

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
    
    ai_quality_confidence = models.IntegerField(default=95, help_text="مؤشر ذكاء اصطناعي لجودة هذا الصنف من هذا التاجر")
    
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
# 🧠 الإشارات المحاسبية المؤتمتة (Bank-Grade FinTech Ledger Signals)
# =====================================================================

@receiver(post_save, sender=EscrowLedger)
def update_client_balances_on_ledger_entry(sender, instance, created, **kwargs):
    """
    🚀 ابتكار أمني: استخدام تعبيرات F() الذرية لمنع الـ Race Conditions وتدمير الأرصدة.
    """
    if created:
        with transaction.atomic():
            client_id = instance.client_id
            amount = instance.amount
            
            if instance.transaction_type == 'deposit':
                Client.objects.filter(pk=client_id).update(wallet_balance=F('wallet_balance') + amount)
                logger.info(f"💰 [FINTECH ACC]: Deposited {amount} EGP to client ID {client_id}.")
                
            elif instance.transaction_type == 'hold':
                client = Client.objects.select_for_update().get(pk=client_id)
                if client.wallet_balance < amount:
                    raise ValidationError("❌ الرصيد المتاح لا يكفي لتجميد ثمن المزاد.")
                Client.objects.filter(pk=client_id).update(
                    wallet_balance=F('wallet_balance') - amount,
                    escrow_held=F('escrow_held') + amount
                )
                logger.info(f"🔒 [FINTECH ACC]: Frozen {amount} EGP into escrow from ID {client_id}.")
                
            elif instance.transaction_type == 'release':
                Client.objects.filter(pk=client_id).update(escrow_held=F('escrow_held') - amount)
                
                # تحويل الأرباح بشكل ذري 100% للتاجر الفائز
                if instance.bidding_request and instance.bidding_request.winner_id:
                    seller_id = instance.bidding_request.winner_id
                    fee = instance.bidding_request.platform_fee_collected
                    Client.objects.filter(pk=seller_id).update(wallet_balance=F('wallet_balance') + (amount - fee))
                    logger.info(f"💸 [FINTECH ACC]: Released {amount - fee} EGP to seller ID {seller_id}.")
                    
            elif instance.transaction_type == 'refund':
                Client.objects.filter(pk=client_id).update(
                    escrow_held=F('escrow_held') - amount,
                    wallet_balance=F('wallet_balance') + amount
                )
                logger.info(f"🔄 [FINTECH ACC]: Refunded {amount} EGP back to ID {client_id}.")
                
            elif instance.transaction_type == 'withdrawal':
                client = Client.objects.select_for_update().get(pk=client_id)
                if client.wallet_balance < amount:
                    raise ValidationError("❌ الرصيد المتاح للسحب أقل من المبلغ المطلوب.")
                Client.objects.filter(pk=client_id).update(wallet_balance=F('wallet_balance') - amount)
                logger.info(f"📤 [FINTECH ACC]: Withdrawn {amount} EGP for ID {client_id}.")