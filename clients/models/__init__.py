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
            # Create ledger entry FIRST — if it fails, status stays unchanged
            EscrowLedger.objects.create(
                client=self.buyer,
                bidding_request=self,
                transaction_type='hold',
                amount=self.winning_price,
                description=f"تجميد مالي مؤقت لثمن قطعة {self.part_number} بالمزاد العكسي #{self.id}"
            )
            self.status = 'escrow_held'
            self.save(update_fields=['status'])

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
# =====================================================================
# 🚗 P2P سوق قطع غيار السيارات (Peer-to-Peer Parts Marketplace)
#    البائع (عميل أو شركة) يعرض قطعة، المشتري يدفع، الفلوس Escrow
#    حتى تنتهي فترة الضمان (1-90 يوم) ثم تحرَّر للبائع. عمولة المنصة:
#    8% للعملاء (أفراد) و 4% للشركات (Tenants). الشحن في الإرجاع
#    على المنصة من العمولة.
# =====================================================================

def _validate_warranty_days(value):
    from django.core.exceptions import ValidationError
    if value < 1 or value > 90:
        raise ValidationError(_("فترة الضمان يجب أن تكون بين 1 و 90 يوم"))


class PartCarMake(models.Model):
    """ماركة سيارة — للفلترة في سوق قطع الغيار."""
    name = models.CharField(max_length=80, unique=True, verbose_name=_("الماركة"))
    slug = models.SlugField(max_length=80, unique=True, db_index=True)
    logo = models.ImageField(upload_to='parts/makes/', blank=True, null=True)
    sort_order = models.IntegerField(default=100)
    is_active = models.BooleanField(default=True)
    listings_count = models.IntegerField(default=0, help_text=_("Cached count of active listings"))

    class Meta:
        verbose_name = _("ماركة سيارة")
        verbose_name_plural = _("🏷️ ماركات السيارات")
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name


class PartListing(SoftDeleteMixin, models.Model):
    """قطعة غيار معروضة للبيع P2P. البائع إما عميل سوق أو شركة (Tenant)."""

    CONDITION_CHOICES = (
        ('new',             _('جديد بالضمانة')),
        ('used_excellent',  _('مستعمل — ممتاز')),
        ('used_good',       _('مستعمل — جيد')),
        ('used_fair',       _('مستعمل — مقبول')),
        ('refurbished',     _('مجدد')),
    )
    STATUS_CHOICES = (
        ('draft',    _('مسودة')),
        ('active',   _('نشط')),
        ('reserved', _('محجوز')),  # دفع جارٍ
        ('sold',     _('تم البيع')),
        ('removed',  _('محذوف')),
    )
    # Admin moderation gate — orthogonal to lifecycle `status`.
    # New listings start `pending_approval` and stay hidden from the public
    # feed until a Super Admin approves them. Rejected listings are kept
    # for audit but never resurface publicly.
    MODERATION_CHOICES = (
        ('pending_approval', _('بانتظار موافقة الإدارة')),
        ('approved',         _('معتمد')),
        ('rejected',         _('مرفوض')),
        ('suspended',        _('معلق بواسطة الإدارة')),
    )

    listing_code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)

    # Seller (exactly one of these must be set)
    seller_customer = models.ForeignKey(
        'MarketplaceCustomer', on_delete=models.PROTECT,
        null=True, blank=True, related_name='part_listings',
        verbose_name=_("البائع (عميل سوق)"),
    )
    seller_tenant = models.ForeignKey(
        'Client', on_delete=models.PROTECT,
        null=True, blank=True, related_name='part_listings',
        verbose_name=_("البائع (شركة)"),
    )

    title = models.CharField(max_length=200, verbose_name=_("اسم القطعة"))
    description = models.TextField(verbose_name=_("الوصف"),
        help_text=_("اوصف القطعة بالتفصيل: الحالة، أي عيوب، رقم القطعة الأصلي، إلخ"))
    car_make = models.ForeignKey(PartCarMake, on_delete=models.PROTECT,
                                 related_name='listings', verbose_name=_("ماركة السيارة"))
    car_model = models.CharField(max_length=100, blank=True, db_index=True,
                                 verbose_name=_("الموديل"),
                                 help_text=_("مثال: 320i, X5, Cooper S, F30"))
    car_year_from = models.IntegerField(null=True, blank=True, verbose_name=_("من سنة"))
    car_year_to   = models.IntegerField(null=True, blank=True, verbose_name=_("إلى سنة"))
    engine_code = models.CharField(max_length=30, blank=True, db_index=True,
                                   verbose_name=_("كود الموتور"),
                                   help_text=_("مثال: N13, N20, M54, K20A — حساس جداً لمطابقة قطع غيار محرك."))
    part_number = models.CharField(max_length=120, blank=True, db_index=True,
                                   verbose_name=_("رقم القطعة الأصلي (OEM)"))
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default='used_good')

    price_egp = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("السعر (ج.م)"))
    warranty_days = models.IntegerField(
        default=3, validators=[_validate_warranty_days],
        verbose_name=_("فترة الضمان (يوم)"),
        help_text=_("بعد التسليم — كلما طالت الفترة زادت ثقة المشتري (الحد الأقصى 90 يوم)"),
    )

    city = models.CharField(max_length=100, blank=True, verbose_name=_("المدينة"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft', db_index=True)
    moderation_status = models.CharField(
        max_length=20, choices=MODERATION_CHOICES,
        default='pending_approval', db_index=True,
        verbose_name=_("حالة المراجعة"),
    )
    moderated_at = models.DateTimeField(null=True, blank=True)
    moderated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
        verbose_name=_("راجَعَها"),
    )
    rejection_reason = models.CharField(max_length=255, blank=True, default='')
    views_count = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    sold_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("قطعة غيار معروضة")
        verbose_name_plural = _("🛒 سوق قطع الغيار")
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'car_make']),
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['moderation_status', '-created_at']),
        ]
        constraints = [
            models.CheckConstraint(
                name='partlisting_one_seller',
                check=(
                    models.Q(seller_customer__isnull=False, seller_tenant__isnull=True) |
                    models.Q(seller_customer__isnull=True,  seller_tenant__isnull=False)
                ),
            ),
        ]

    def __str__(self):
        return f"{self.title} — {self.car_make.name}"

    @property
    def seller_label(self):
        if self.seller_tenant_id:
            return self.seller_tenant.name
        if self.seller_customer_id:
            return self.seller_customer.company_name or self.seller_customer.full_name
        return '—'

    @property
    def seller_is_company(self):
        return self.seller_tenant_id is not None

    @property
    def commission_pct(self):
        """8% للأفراد، 4% للشركات."""
        return Decimal('4.00') if self.seller_is_company else Decimal('8.00')

    @property
    def commission_amount(self):
        return (self.price_egp * self.commission_pct / Decimal('100')).quantize(Decimal('0.01'))

    @property
    def seller_payout(self):
        return (self.price_egp - self.commission_amount).quantize(Decimal('0.01'))

    @property
    def primary_photo_url(self):
        photo = self.photos.filter(is_primary=True).first() or self.photos.first()
        if photo and photo.image:
            return photo.image.url
        return ''

    @property
    def is_publicly_visible(self):
        return (
            not self.is_deleted
            and self.status == 'active'
            and self.moderation_status == 'approved'
        )

    def approve(self, by_user):
        if self.moderation_status == 'approved':
            return False
        self.moderation_status = 'approved'
        self.moderated_at = timezone.now()
        self.moderated_by = by_user if (by_user and by_user.is_authenticated) else None
        self.rejection_reason = ''
        # Lift seller draft to active so the listing actually surfaces.
        if self.status == 'draft':
            self.status = 'active'
        self.save(update_fields=[
            'moderation_status', 'moderated_at', 'moderated_by',
            'rejection_reason', 'status',
        ])
        return True

    def reject(self, by_user, reason=''):
        self.moderation_status = 'rejected'
        self.moderated_at = timezone.now()
        self.moderated_by = by_user if (by_user and by_user.is_authenticated) else None
        self.rejection_reason = (reason or '')[:255]
        self.save(update_fields=[
            'moderation_status', 'moderated_at', 'moderated_by', 'rejection_reason',
        ])
        return True


class PartListingPhoto(models.Model):
    """صور لكل قطعة. حد أدنى 3 موصى به ليبان كل تفصيلة."""
    listing = models.ForeignKey(PartListing, on_delete=models.CASCADE, related_name='photos')
    image = models.ImageField(upload_to='parts/listings/%Y/%m/')
    caption = models.CharField(max_length=200, blank=True)
    is_primary = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_primary', 'sort_order', 'uploaded_at']

    def __str__(self):
        return f"Photo of {self.listing_id}"


class PartOrder(SoftDeleteMixin, models.Model):
    """
    أمر شراء قطعة. الفلوس escrow حتى ينتهي وقت الضمان.

    دورة الحياة:
      pending_payment → (Paymob) → paid_held → (تسليم) → warranty_window → released
      أو في أي وقت → refunded / disputed
    """
    STATUS_CHOICES = (
        ('pending_payment',  _('بانتظار الدفع')),
        ('paid_held',        _('مدفوع — في الـ Escrow')),
        ('shipped',          _('في الشحن')),
        ('delivered',        _('تم التسليم — في فترة الضمان')),
        ('released',         _('تم الإفراج — أُرسلت للبائع')),
        ('refund_requested', _('طلب إرجاع')),
        ('refunded',         _('تم الإرجاع للمشتري')),
        ('disputed',         _('نزاع — قيد المراجعة')),
        ('cancelled',        _('ملغي')),
    )

    order_code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    listing = models.ForeignKey(PartListing, on_delete=models.PROTECT, related_name='orders')

    # Buyer (exactly one)
    buyer_customer = models.ForeignKey(
        'MarketplaceCustomer', on_delete=models.PROTECT,
        null=True, blank=True, related_name='part_orders',
    )
    buyer_tenant = models.ForeignKey(
        'Client', on_delete=models.PROTECT,
        null=True, blank=True, related_name='part_orders',
    )

    # Frozen amounts (snapshot at order time)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2)
    commission_amount = models.DecimalField(max_digits=12, decimal_places=2)
    seller_payout = models.DecimalField(max_digits=12, decimal_places=2)
    warranty_days = models.IntegerField()

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending_payment', db_index=True)

    # Shipping info captured at checkout
    shipping_name = models.CharField(max_length=120, blank=True)
    shipping_phone = models.CharField(max_length=30, blank=True)
    shipping_address = models.TextField(blank=True)
    shipping_city = models.CharField(max_length=80, blank=True)

    # Paymob
    paymob_order_id = models.CharField(max_length=100, blank=True, db_index=True)
    paymob_txn_id   = models.CharField(max_length=100, blank=True, db_index=True)

    # Timeline
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    warranty_ends_at = models.DateTimeField(null=True, blank=True, db_index=True)
    released_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)

    # Notes / dispute
    refund_reason = models.TextField(blank=True)
    admin_notes = models.TextField(blank=True)

    # ── Return shipping liability ──────────────────────────────────
    # Policy: platform never pays return shipping. Buyer pays on remorse,
    # seller pays when the part is defective / incorrect / never arrived /
    # not as described. The mapping lives in clients/services/escrow.py;
    # this column is the *decision* recorded at the time the return was
    # initiated. NULL until a return is actually opened.
    RETURN_REASON_CHOICES = (
        ('buyer_remorse',    _('تراجع المشتري')),
        ('wrong_size_or_fit',_('مقاس / تركيب خطأ من المشتري')),
        ('defective',        _('القطعة معيبة')),
        ('incorrect',        _('القطعة مختلفة عن المعروض')),
        ('not_as_described', _('غير مطابق للوصف / الصور')),
        ('never_arrived',    _('لم تصل')),
    )
    RETURN_PAYER_CHOICES = (
        ('buyer',  _('المشتري')),
        ('seller', _('البائع')),
        # 'platform' is intentionally NOT a choice — see DB check constraint.
    )
    return_reason = models.CharField(
        max_length=25, choices=RETURN_REASON_CHOICES, blank=True, default='',
    )
    return_shipping_payer = models.CharField(
        max_length=10, choices=RETURN_PAYER_CHOICES, blank=True, default='',
    )

    class Meta:
        verbose_name = _("طلب شراء قطعة")
        verbose_name_plural = _("📦 طلبات شراء قطع الغيار")
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', 'warranty_ends_at'])]
        constraints = [
            models.CheckConstraint(
                name='partorder_one_buyer',
                check=(
                    models.Q(buyer_customer__isnull=False, buyer_tenant__isnull=True) |
                    models.Q(buyer_customer__isnull=True,  buyer_tenant__isnull=False)
                ),
            ),
            # 🛡️ Legal: platform must never appear as the return-shipping payer.
            models.CheckConstraint(
                name='partorder_return_payer_never_platform',
                check=models.Q(return_shipping_payer__in=['', 'buyer', 'seller']),
            ),
        ]

    def __str__(self):
        return f"Order {self.order_code} — {self.listing.title[:40]}"

    @property
    def buyer_label(self):
        if self.buyer_tenant_id:
            return self.buyer_tenant.name
        if self.buyer_customer_id:
            return self.buyer_customer.company_name or self.buyer_customer.full_name
        return '—'

    def mark_delivered(self):
        """يستدعى لما المشتري يأكد الاستلام — يبدأ عدّاد الضمان."""
        if self.status not in ('paid_held', 'shipped'):
            return False
        now = timezone.now()
        self.status = 'delivered'
        self.delivered_at = now
        self.warranty_ends_at = now + timedelta(days=self.warranty_days)
        self.save(update_fields=['status', 'delivered_at', 'warranty_ends_at'])
        return True

    def release_to_seller(self, by_user=None):
        """يحرّر الفلوس للبائع — يستدعى تلقائياً بعد انتهاء فترة الضمان."""
        if self.status not in ('delivered',):
            return False
        if self.warranty_ends_at and timezone.now() < self.warranty_ends_at:
            return False
        self.status = 'released'
        self.released_at = timezone.now()
        self.save(update_fields=['status', 'released_at'])
        # Update escrow ledger — the financial record must reflect the release.
        try:
            from clients.services import escrow as escrow_svc
            escrow_svc.release_to_seller(self, by_user=by_user, reason='warranty period elapsed')
        except Exception:
            import logging
            logging.getLogger('mouss_tec_core').exception(
                "[ESCROW] release_to_seller failed for order %s", self.order_code
            )
        # Notify seller
        if self.listing.seller_customer_id:
            CustomerNotification.objects.create(
                customer=self.listing.seller_customer,
                title='💰 تم تحويل أموالك',
                body=f'فترة الضمان انتهت لطلب «{self.listing.title}». المبلغ {self.seller_payout} ج.م في طريقه لحسابك.',
                level='success', icon='fa-money-bill-wave',
            )
        return True


# =====================================================================
# 🆘 PartWantedRequest — buyer's "I need this part" post.
# Sellers browse a feed of these filtered by exact car spec.
# Mirrors eBay Motors / RockAuto fitment-matching: required year +
# make + model + engine code is the strong filter.
# =====================================================================
class PartWantedRequest(SoftDeleteMixin, models.Model):
    STATUS_CHOICES = (
        ('open',       _('مفتوح — بانتظار عروض')),
        ('matched',    _('تم قبول عرض')),
        ('fulfilled',  _('تم الشراء')),
        ('cancelled',  _('ملغي بواسطة المشتري')),
        ('expired',    _('منتهي الصلاحية')),
    )

    request_code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)

    # Buyer (exactly one)
    buyer_customer = models.ForeignKey(
        'MarketplaceCustomer', on_delete=models.PROTECT,
        null=True, blank=True, related_name='part_wanted_requests',
    )
    buyer_tenant = models.ForeignKey(
        'Client', on_delete=models.PROTECT,
        null=True, blank=True, related_name='part_wanted_requests',
    )

    # ── Vehicle metadata (the strict-matching filter) ──
    car_make = models.ForeignKey(PartCarMake, on_delete=models.PROTECT,
                                 related_name='wanted_requests',
                                 verbose_name=_("الماركة"))
    car_model = models.CharField(max_length=100, db_index=True,
                                 verbose_name=_("الموديل"),
                                 help_text=_("مثال: F30, X5, Civic — مطلوب."))
    car_year = models.IntegerField(db_index=True, verbose_name=_("سنة الصنع"),
                                   help_text=_("سنة واحدة محددة (مطلوبة)."))
    engine_code = models.CharField(max_length=30, blank=True, db_index=True,
                                   verbose_name=_("كود الموتور"),
                                   help_text=_("N13, N20, etc. اتركه فارغاً لو القطعة هيكلية أو مش متعلقة بالمحرك."))

    # ── Part details ──
    part_name = models.CharField(max_length=200, verbose_name=_("اسم القطعة"))
    part_number_oem = models.CharField(max_length=120, blank=True, db_index=True,
                                       verbose_name=_("رقم OEM (اختياري)"))
    description = models.TextField(blank=True, verbose_name=_("ملاحظات إضافية"))
    max_budget_egp = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        verbose_name=_("الميزانية القصوى (ج.م) — اختياري"),
    )

    status = models.CharField(max_length=15, choices=STATUS_CHOICES,
                              default='open', db_index=True)

    created_at  = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at  = models.DateTimeField(db_index=True,
        help_text=_("الطلب يختفي تلقائياً بعد 14 يوم."))
    fulfilled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("طلب قطعة (Wanted)")
        verbose_name_plural = _("🆘 طلبات القطع")
        ordering = ['-created_at']
        indexes = [
            # The seller-feed query: open + (make, model, year) — fast.
            models.Index(fields=['status', 'car_make', 'car_model', 'car_year']),
            models.Index(fields=['status', 'engine_code']),
            models.Index(fields=['status', 'expires_at']),
        ]
        constraints = [
            models.CheckConstraint(
                name='partwanted_one_buyer',
                check=(
                    models.Q(buyer_customer__isnull=False, buyer_tenant__isnull=True) |
                    models.Q(buyer_customer__isnull=True,  buyer_tenant__isnull=False)
                ),
            ),
        ]

    def __str__(self):
        return f"Wanted: {self.part_name} — {self.car_make.name} {self.car_model} {self.car_year}"

    def save(self, *args, **kwargs):
        # Default 14-day expiry if not explicitly set.
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(days=14)
        # Normalize engine_code casing — N13 not n13/N13/n-13.
        if self.engine_code:
            self.engine_code = self.engine_code.upper().strip()
        super().save(*args, **kwargs)

    @property
    def is_visible_to_sellers(self):
        return (
            not self.is_deleted
            and self.status == 'open'
            and self.expires_at > timezone.now()
        )


class PartWantedOffer(models.Model):
    """A seller's offer in response to a PartWantedRequest."""
    STATUS_CHOICES = (
        ('pending',  _('مرسل — بانتظار رد المشتري')),
        ('accepted', _('قبله المشتري')),
        ('rejected', _('رفضه المشتري')),
        ('withdrawn',_('سحبه البائع')),
    )

    request = models.ForeignKey(
        PartWantedRequest, on_delete=models.CASCADE, related_name='offers',
    )
    seller_customer = models.ForeignKey(
        'MarketplaceCustomer', on_delete=models.PROTECT,
        null=True, blank=True, related_name='wanted_offers_made',
    )
    seller_tenant = models.ForeignKey(
        'Client', on_delete=models.PROTECT,
        null=True, blank=True, related_name='wanted_offers_made',
    )
    # If the seller already has a listing matching this request,
    # point at it so the buyer can click through.
    linked_listing = models.ForeignKey(
        'PartListing', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='wanted_offers',
    )

    price_egp = models.DecimalField(max_digits=12, decimal_places=2)
    condition = models.CharField(max_length=20, choices=PartListing.CONDITION_CHOICES, default='used_good')
    notes = models.TextField(blank=True, max_length=500)
    warranty_days = models.IntegerField(default=3, validators=[_validate_warranty_days])

    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='pending', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("عرض على طلب قطعة")
        verbose_name_plural = _("عروض على طلبات القطع")
        ordering = ['price_egp', '-created_at']
        constraints = [
            models.CheckConstraint(
                name='partwantedoffer_one_seller',
                check=(
                    models.Q(seller_customer__isnull=False, seller_tenant__isnull=True) |
                    models.Q(seller_customer__isnull=True,  seller_tenant__isnull=False)
                ),
            ),
            # One offer per (request, seller). Two conditional constraints
            # because Postgres treats NULL ≠ NULL, so a single fields=[...]
            # UniqueConstraint with a nullable seller_* column won't fire.
            models.UniqueConstraint(
                name='partwantedoffer_one_per_customer_seller',
                fields=['request', 'seller_customer'],
                condition=models.Q(seller_customer__isnull=False),
            ),
            models.UniqueConstraint(
                name='partwantedoffer_one_per_tenant_seller',
                fields=['request', 'seller_tenant'],
                condition=models.Q(seller_tenant__isnull=False),
            ),
        ]

    def __str__(self):
        return f"Offer {self.price_egp} EGP on {self.request_id}"


# =====================================================================
# 💰 EscrowHold — financial custody record for P2P part orders
# =====================================================================
class EscrowHold(models.Model):
    """
    One-to-one with PartOrder. Represents the buyer's payment held by the
    platform until ownership + warranty period close. Separating money
    custody from order lifecycle keeps the financial audit trail clean
    and matches how regulated marketplaces (eBay Managed Payments,
    Amazon A-to-z) structure their books.

    Lifecycle:
        held → released_to_seller          (warranty expired, seller paid)
        held → refunded_to_buyer           (full refund granted)
        held → split                       (partial refund, rest to seller)
    """
    STATUS_CHOICES = (
        ('held',                _('محجوز')),
        ('released_to_seller',  _('تم التحويل للبائع')),
        ('refunded_to_buyer',   _('تم الرد للمشتري')),
        ('split',               _('مقسوم — جزئي')),
    )

    order = models.OneToOneField(
        'PartOrder', on_delete=models.PROTECT, related_name='escrow_hold',
        verbose_name=_("الطلب"),
    )
    status = models.CharField(max_length=25, choices=STATUS_CHOICES,
                              default='held', db_index=True)

    # Frozen amounts — set at creation, never edited.
    held_amount = models.DecimalField(max_digits=12, decimal_places=2,
        help_text=_("إجمالي المبلغ المحجوز عند الدفع."))
    seller_payout_amount = models.DecimalField(max_digits=12, decimal_places=2,
        default=Decimal('0.00'))
    buyer_refund_amount = models.DecimalField(max_digits=12, decimal_places=2,
        default=Decimal('0.00'))
    platform_commission_amount = models.DecimalField(max_digits=12, decimal_places=2,
        default=Decimal('0.00'))

    # Audit
    held_at     = models.DateTimeField(auto_now_add=True, db_index=True)
    settled_at  = models.DateTimeField(null=True, blank=True)
    settled_by  = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    settlement_reason = models.CharField(max_length=255, blank=True, default='')

    # Disclaimer acceptance (which version did the buyer agree to at checkout?)
    accepted_disclaimer = models.ForeignKey(
        'PlatformLiabilityDisclaimer', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='holds',
    )

    class Meta:
        verbose_name = _("حجز ضمان (Escrow Hold)")
        verbose_name_plural = _("💰 حجوزات الضمان")
        ordering = ['-held_at']
        constraints = [
            # All amounts must be non-negative
            models.CheckConstraint(
                name='escrowhold_amounts_nonnegative',
                check=(
                    models.Q(held_amount__gte=0)
                    & models.Q(seller_payout_amount__gte=0)
                    & models.Q(buyer_refund_amount__gte=0)
                    & models.Q(platform_commission_amount__gte=0)
                ),
            ),
            # Money conservation: disbursements must not exceed what was held.
            # Enforced at the DB level to survive concurrent updates.
            models.CheckConstraint(
                name='escrowhold_conservation',
                check=models.Q(
                    held_amount__gte=models.F('seller_payout_amount')
                    + models.F('buyer_refund_amount')
                    + models.F('platform_commission_amount')
                ),
            ),
        ]

    def __str__(self):
        return f"EscrowHold[{self.order.order_code}] {self.status} — {self.held_amount} EGP"


# =====================================================================
# 📜 PlatformLiabilityDisclaimer — versioned legal text the buyer must
#    accept at checkout. The platform's exposure is contractually zero.
# =====================================================================
class PlatformLiabilityDisclaimer(models.Model):
    """
    Versioned. The active row (is_active=True with the highest version)
    is the one displayed at checkout. Previous versions stay queryable
    so we know which exact text each historic buyer agreed to.
    """
    version = models.CharField(max_length=20, unique=True, db_index=True,
        help_text=_("e.g. v1.0, v1.1, v2.0"))
    title_ar = models.CharField(max_length=200,
        default=_("إخلاء مسؤولية المنصة"))
    body_ar = models.TextField(
        help_text=_("النص القانوني الكامل بالعربية."))
    body_en = models.TextField(blank=True, default='',
        help_text=_("Optional English mirror for international buyers."))
    is_active = models.BooleanField(default=True, db_index=True,
        help_text=_("هل هذه النسخة المعروضة حالياً للمشترين الجدد؟"))
    effective_from = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("إخلاء مسؤولية")
        verbose_name_plural = _("📜 وثائق إخلاء المسؤولية")
        ordering = ['-effective_from']

    def __str__(self):
        return f"Disclaimer {self.version} ({'active' if self.is_active else 'archived'})"

    @classmethod
    def current(cls):
        """The disclaimer to show new buyers right now. None if no row exists yet."""
        return cls.objects.filter(is_active=True).order_by('-effective_from').first()


# =====================================================================
# ⚖️ DisputeTicket — buyer/seller claim within 3-day inspection window
# =====================================================================
class DisputeTicket(SoftDeleteMixin, models.Model):
    """
    Either side of a PartOrder can open a dispute within
    ``DISPUTE_WINDOW_DAYS`` of delivery (or anytime before delivery for
    'never_arrived' claims). Opening flips the order status to 'disputed',
    which causes the existing auto-release routine to skip it — escrow
    funds are frozen until an admin resolves the case.

    Resolution dispatches to clients.services.escrow:
      * resolved_refund   → refund_to_buyer
      * resolved_release  → release_to_seller (order temporarily restored to delivered)
      * resolved_split    → split_settlement
      * cancelled         → no-op (claim retracted by opener)
    """
    DISPUTE_WINDOW_DAYS = 3  # global standard inspection window

    OPENER_CHOICES = (
        ('buyer',  _('المشتري')),
        ('seller', _('البائع')),
    )
    CATEGORY_CHOICES = (
        ('item_not_received',     _('لم تصل القطعة')),
        ('item_not_as_described', _('غير مطابقة للوصف')),
        ('damaged_on_arrival',    _('وصلت تالفة')),
        ('wrong_item',            _('قطعة مختلفة')),
        ('counterfeit',           _('غير أصلية / تقليد')),
        ('buyer_misuse',          _('سوء استخدام من المشتري')),
        ('payment_issue',         _('مشكلة في الدفع')),
        ('other',                 _('أخرى')),
    )
    STATUS_CHOICES = (
        ('open',              _('مفتوحة')),
        ('under_review',      _('قيد المراجعة')),
        ('resolved_refund',   _('محلولة — رد للمشتري')),
        ('resolved_release',  _('محلولة — تحرير للبائع')),
        ('resolved_split',    _('محلولة — تسوية جزئية')),
        ('cancelled',         _('ملغاة من مقدّمها')),
    )

    ticket_code = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    order = models.ForeignKey('PartOrder', on_delete=models.PROTECT, related_name='disputes')

    opened_by_role = models.CharField(max_length=10, choices=OPENER_CHOICES)
    opened_by_customer = models.ForeignKey(
        'MarketplaceCustomer', on_delete=models.PROTECT,
        null=True, blank=True, related_name='disputes_opened',
    )
    opened_by_tenant = models.ForeignKey(
        'Client', on_delete=models.PROTECT,
        null=True, blank=True, related_name='disputes_opened',
    )

    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES)
    description = models.TextField(verbose_name=_("التفاصيل"))

    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                              default='open', db_index=True)
    # Snapshot of the order status when the dispute was opened — helpful
    # for audit when the resolution path later flips things around.
    order_status_at_open = models.CharField(max_length=20, blank=True, default='')

    resolution_notes = models.TextField(blank=True, default='')
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )

    opened_at   = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("تذكرة نزاع")
        verbose_name_plural = _("⚖️ تذاكر النزاعات")
        ordering = ['-opened_at']
        indexes = [
            models.Index(fields=['status', '-opened_at']),
            models.Index(fields=['order', 'status']),
        ]
        constraints = [
            models.CheckConstraint(
                name='dispute_one_opener',
                check=(
                    models.Q(opened_by_customer__isnull=False, opened_by_tenant__isnull=True) |
                    models.Q(opened_by_customer__isnull=True,  opened_by_tenant__isnull=False)
                ),
            ),
        ]

    def __str__(self):
        return f"Dispute {self.ticket_code} on order {self.order.order_code} ({self.status})"

    @classmethod
    def is_within_window(cls, order) -> bool:
        """
        Buyer/seller may open a dispute iff:
          * order was delivered AND now - delivered_at ≤ 3 days, OR
          * order is paid_held / shipped (item not received scenarios), OR
          * order is already disputed (additional context).
        Released / refunded orders are closed for dispute.
        """
        if order.status in ('released', 'refunded', 'cancelled'):
            return False
        if order.status in ('paid_held', 'shipped', 'disputed'):
            return True
        if order.status == 'delivered' and order.delivered_at:
            return timezone.now() - order.delivered_at <= timedelta(days=cls.DISPUTE_WINDOW_DAYS)
        return False


class DisputeEvidence(models.Model):
    """Photos / screenshots attached to a dispute. Image-only at v1."""
    ticket = models.ForeignKey(DisputeTicket, on_delete=models.CASCADE, related_name='evidence')
    image = models.ImageField(upload_to='disputes/%Y/%m/')
    caption = models.CharField(max_length=200, blank=True, default='')
    uploaded_by_role = models.CharField(max_length=10, choices=DisputeTicket.OPENER_CHOICES, blank=True, default='')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['uploaded_at']


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

