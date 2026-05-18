from django.db import models, transaction
from django_tenants.models import TenantMixin, DomainMixin
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save
from django.dispatch import receiver
from datetime import timedelta
from decimal import Decimal
import uuid
import logging

logger = logging.getLogger('mouss_tec_core')

# 🎯 أتمتة الفخ الذهبي: فترة تجريبية 3 أيام فقط لإجبار العميل على اتخاذ قرار
def default_trial_end():
    return timezone.now().date() + timedelta(days=3)

# =====================================================================
# 🏢 1. جدول المستأجرين (شركات Mouss Tec Ecosystem)
# =====================================================================
class Client(TenantMixin):
    name = models.CharField(max_length=100, verbose_name=_("اسم المركز/الشركة"))
    owner_name = models.CharField(max_length=100, verbose_name=_("اسم المالك"))
    phone = models.CharField(max_length=20, verbose_name=_("رقم الهاتف"))
    email = models.EmailField(blank=True, null=True, verbose_name=_("البريد الإلكتروني للإدارة"))
    
    BUSINESS_TYPE_CHOICES = (
        ('service_center', _('مركز صيانة متكامل (ورشة + قطع غيار)')),
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
        ('silver', _('باقة سيلفر - للورش الناشئة')),
        ('gold', _('باقة جولد - للمراكز المتنامية (تجربة افتراضية)')),
        ('empire', _('الباقة الشاملة - الإمبراطورية')),
    )
    plan = models.CharField(max_length=20, choices=SUBSCRIPTION_CHOICES, default='gold', verbose_name=_("الباقة"))
    
    max_branches = models.IntegerField(default=2, verbose_name=_("الفروع المشمولة بالباقة"))
    max_users = models.IntegerField(default=5, verbose_name=_("المستخدمين المشمولين بالباقة"))
    max_repair_cards = models.IntegerField(default=0, help_text="0 تعني غير محدود", verbose_name=_("حد كروت الصيانة الشهري"))
    max_inventory_items = models.IntegerField(default=0, help_text="0 تعني غير محدود", verbose_name=_("حد أصناف المخزن"))
    
    extra_branches_purchased = models.IntegerField(default=0, verbose_name=_("فروع إضافية مشتراة"))
    extra_users_purchased = models.IntegerField(default=0, verbose_name=_("مستخدمين إضافيين مشتراة"))
    
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
        if self.status == 'suspended' or not self.is_active or self.is_fraud_flagged: return False
        today = timezone.now().date()
        if self.status == 'trial' and today > self.trial_ends_at: return False
        if self.status == 'active' and self.subscription_end_date and today > self.subscription_end_date: return False
        return True

    @property
    def total_allowed_branches(self):
        return self.max_branches + self.extra_branches_purchased

    @property
    def total_allowed_users(self):
        return self.max_users + self.extra_users_purchased

    def save(self, *args, **kwargs):
        if self.plan == 'silver': 
            self.max_branches, self.max_users = 1, 2
            self.max_repair_cards, self.max_inventory_items = 150, 500
        elif self.plan == 'gold': 
            self.max_branches, self.max_users = 2, 5
            self.max_repair_cards, self.max_inventory_items = 0, 0 
        elif self.plan == 'empire': 
            self.max_branches, self.max_users = 999, 9999 
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
    
    ai_recommended_winner = models.ForeignKey('BidOffer', on_delete=models.SET_NULL, blank=True, null=True, related_name='recommended_for', help_text="أفضل عرض تم ترشيحه بواسطة محرك الـ AI بناءً على الجودة والسعر")
    
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

    # 🚀 ابتكارات أتمتة دورة حياة المزاد للمنظومة الذكية (MAS Atomic Triggers)
    def trigger_escrow_hold(self):
        """تجميد أموال المزاد في حساب الضمان للمشتري فور الترسية"""
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
        """تحرير الأموال وإرسالها للتاجر الفائز بعد فحص القطعة وتجاوز مرحلة النزاعات الفنية"""
        if self.status != 'shipped':
            raise ValidationError("لا يمكن تحرير الضمان المالي إلا بعد إتمام عملية الشحن والتسليم للورشة.")
        if not self.winner or not self.winning_price:
            raise ValidationError("بيانات التاجر الفائز أو سعر الترسية غير مكتملة.")
            
        with transaction.atomic():
            self.status = 'completed'
            # حساب عمولة المنصة ديناميكياً بناءً على نسبة عقد المشتري
            fee = (self.winning_price * self.buyer.platform_fee_rate) / Decimal('100.00')
            self.platform_fee_collected = fee
            self.save(update_fields=['status', 'platform_fee_collected'])
            
            # 1. إثبات تحرير الحركة من حساب الضمان للمشتري
            EscrowLedger.objects.create(
                client=self.buyer,
                bidding_request=self,
                transaction_type='release',
                amount=self.winning_price,
                description=f"💸 إفراج مالي عن ثمن قطعة {self.part_number} من حساب الضمان للمشتري للتاجر {self.winner.name}"
            )
            
            # 2. قيد خصم عمولة Mouss Tec من أرباح التاجر الصافية
            if fee > 0:
                EscrowLedger.objects.create(
                    client=self.winner,
                    bidding_request=self,
                    transaction_type='fee_deduction',
                    amount=fee,
                    description=f"⚙️ خصم عمولة تشغيل منصة Mouss Tec عن المزاد #{self.id}"
                )

    def trigger_refund_to_buyer(self):
        """إعادة الأموال بالكامل لمحفظة المشتري في حال إلغاء المزاد أو ربح النزاع الفني (تيل فرامل غير مطابق مثلاً)"""
        if self.status not in ['escrow_held', 'disputed']:
            raise ValidationError("الوضعية التشغيلية الحالية للمزاد لا تسمح برد المبالغ المجمّدة.")
            
        with transaction.atomic():
            self.status = 'cancelled'
            self.save(update_fields=['status'])
            
            EscrowLedger.objects.create(
                client=self.buyer,
                bidding_request=self,
                transaction_type='refund',
                amount=self.winning_price,
                description=f"🔄 رد الرصيد المجمد كاملاً لمحفظة المشتري لإلغاء المزاد أو فض النزاع الفني بنجاح."
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
    
    ai_match_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, help_text="تقييم الـ AI الشامل لهذا العرض (سعر + موثوقية التاجر + سرعة التوصيل)")
    
    is_winner = models.BooleanField(default=False, verbose_name=_("هل هو العرض الفائز؟"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("عرض سعر")
        verbose_name_plural = _("عروض أسعار التجار")
        unique_together = ('bidding_request', 'seller') 

    def __str__(self):
        return f"Offer by {self.seller.name} for Bid #{self.bidding_request.id} - {self.offer_price} EGP"

# =====================================================================
# 🏦 6. دفتر الأستاذ المالي لمنصة Mouss Tec (Immutable Escrow Ledger)
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
# 🧠 الإشارات المحاسبية المؤتمتة (Atomic FinTech Ledger Signals)
# =====================================================================

@receiver(post_save, sender=EscrowLedger)
def update_client_balances_on_ledger_entry(sender, instance, created, **kwargs):
    """
    محلل الحركات المحاسبية الفوري والمضاد للاختراق:
    يضمن المزامنة الرياضية التامة بين قيد الدفتر وأرصدة المحافظ الفعلية داخل بيئة معزولة وذرية.
    """
    if created:
        with transaction.atomic():
            client = instance.client
            amount = instance.amount
            
            if instance.transaction_type == 'deposit':
                client.wallet_balance += amount
                client.save(update_fields=['wallet_balance'])
                logger.info(f"💰 [FINTECH ACC]: Deposited {amount} EGP to client '{client.name}' wallet.")
                
            elif instance.transaction_type == 'hold':
                # 🛡️ منع تجميد أموال الورشة إذا كانت قيمة محفظتها أقل من ثمن الترسية المطلوب
                if client.wallet_balance < amount:
                    raise ValidationError(f"❌ الرصيد المتاح بمحفظة {client.name} لا يكفي لتجميد ثمن المزاد بقيمة ({amount}) ج.م")
                client.wallet_balance -= amount
                client.escrow_held += amount
                client.save(update_fields=['wallet_balance', 'escrow_held'])
                logger.info(f"🔒 [FINTECH ACC]: Frozen {amount} EGP into escrow from '{client.name}'.")
                
            elif instance.transaction_type == 'release':
                # خصم الأموال المجمدة من حساب المشتري رسمياً
                client.escrow_held -= amount
                client.save(update_fields=['escrow_held'])
                
                # تحويل صافي الأرباح (السعر - العمولة) فوراً لمحفظة التاجر الفائز الموثق بالسيستم
                if instance.bidding_request and instance.bidding_request.winner:
                    seller = instance.bidding_request.winner
                    fee = instance.bidding_request.platform_fee_collected
                    seller.wallet_balance += (amount - fee)
                    seller.save(update_fields=['wallet_balance'])
                    logger.info(f"💸 [FINTECH ACC]: Released {amount - fee} EGP to seller '{seller.name}' (Fee {fee} EGP deducted).")
                    
            elif instance.transaction_type == 'refund':
                # إعادة فك التجميد ورد الأموال من الضمان للمحفظة الأساسية
                client.escrow_held -= amount
                client.wallet_balance += amount
                client.save(update_fields=['wallet_balance', 'escrow_held'])
                logger.info(f"🔄 [FINTECH ACC]: Refunded {amount} EGP back to '{client.name}' wallet due to cancellation.")
                
            elif instance.transaction_type == 'fee_deduction':
                # توثيق داخلي لعمولات منصة Mouss Tec 
                pass
                
            elif instance.transaction_type == 'withdrawal':
                if client.wallet_balance < amount:
                    raise ValidationError("❌ الرصيد المتاح للسحب في المحفظة أقل من المبلغ المطلوب.")
                client.wallet_balance -= amount
                client.save(update_fields=['wallet_balance'])
                logger.info(f"📤 [FINTECH ACC]: Processed successful withdrawal of {amount} EGP for '{client.name}'.")