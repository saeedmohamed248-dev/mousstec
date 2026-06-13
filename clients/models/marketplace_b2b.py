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


# Cross-domain: tenancy provides Client (referenced by FKs & signal handlers);
# marketplace_c2c provides MarketplaceCustomer (referenced by PartListing/PartOrder/DisputeTicket).
from .tenancy import *  # noqa: F401, F403
from .marketplace_c2c import *  # noqa: F401, F403

# Marketplace B2B: bidding, escrow ledger, parts marketplace, disputes.

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


