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


# Cross-domain references resolved via:
from .tenancy import *  # noqa: F401, F403
from .marketplace_c2c import *  # noqa: F401, F403
from .design_store import *  # noqa: F401, F403

# ManualPaymentReceipt: unified Vodafone Cash / InstaPay receipt review queue.

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
        # 🐛 [FIX]: PartOrder و CustomerDiagnosticsSubscription ماكانوش متعرفين
        #    في الموديول ده (مش داخلين في الـ star-imports اللي فوق)، فالـ
        #    NameError كان بيتبلع في except والدالة ترجع None — يعني موافقة
        #    الأدمن على إيصال فودافون كاش لقطعة غيار أو ترقية تشخيص كانت
        #    بتقلب الإيصال "متأكد" من غير ما تفعّل أي حاجة للعميل.
        from .marketplace_b2b import PartOrder
        from .diagnostics import CustomerDiagnosticsSubscription
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
            logger.exception("[ManualReceipt %s] failed to resolve purchase", self.receipt_code)
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

        from .marketplace_b2b import PartListing

        # Activate based on purchase type
        if self.purchase_type == 'design':
            purchase.status = 'paid'
            purchase.paid_at = timezone.now()
            purchase.payment_reference = self.txn_reference
            purchase.sender_phone = self.sender_phone
            purchase.save(update_fields=['status', 'paid_at', 'payment_reference', 'sender_phone'])
        elif self.purchase_type == 'subscription':
            # 🐛 [FIX]: أي خطأ في التفعيل كان بيتسجّل في اللوج بس — الإيصال يتعلّم
            #    «مؤكد» والاشتراك ماتمدّش، ومفيش طريقة يتعاد (المؤكد مابيتأكدش
            #    تاني). دلوقتي الخطأ بيطلع للأدمن والإيصال يفضل «في الانتظار».
            #    والفترة بتتحسب وقت التأكيد (settle_issued_invoice).
            from clients.services.platform_billing import settle_issued_invoice
            settle_issued_invoice(purchase, provider=self.payment_method,
                                  reference=self.txn_reference or None)
        elif self.purchase_type == 'parts':
            # 🐛 [FIX]: المسار اليدوي كان بيقلب الطلب لـ paid_held من غير ما
            #    يبلّغ البائع إن قطعته اتباعت (ولا المشتري) — البائع ماكانش
            #    يعرف إنه لازم يشحن. دلوقتي نفس خدمة الدفع الموحدة بتاعة Paymob.
            from marketplace_b2b.services.parts_orders import mark_paid
            if purchase.status == 'cancelled':
                # الطلب اتلغى (انتهت المهلة) قبل ما الأدمن يراجع الإيصال —
                # نرجّعه لو القطعة لسه متاحة، غير كده الأدمن لازم يرجّع الفلوس.
                listing = PartListing.objects.select_for_update().get(pk=purchase.listing_id)
                if listing.status != 'active' or listing.is_deleted:
                    raise ValidationError(
                        'القطعة اتباعت أو اتسحبت بعد إلغاء الطلب — لازم ترجّع المبلغ للعميل يدوياً.'
                    )
                listing.status = 'reserved'
                listing.save(update_fields=['status'])
                purchase.status = 'pending_payment'
                purchase.save(update_fields=['status'])
            mark_paid(purchase, txn_id=f'manual:{self.txn_reference}')
        elif self.purchase_type == 'diagnostics':
            purchase.upgrade(self.diagnostics_tier(), payment_ref=f'manual:{self.txn_reference}')
        elif self.purchase_type == 'diag_topup':
            # purchase is the DiagnosticsTopUpPack; credit its uses to the
            # tenant on this receipt.
            if self.tenant_id and getattr(purchase, 'uses_granted', 0) > 0:
                from clients.services.diagnostics_quota import add_topup
                add_topup(self.tenant, purchase.uses_granted)
        elif self.purchase_type == 'tenant_topup':
            # purchase is the TenantDesignTopUp itself; flip to paid.
            purchase.status = 'paid'
            purchase.paid_at = timezone.now()
            purchase.payment_reference = self.txn_reference
            purchase.payment_method = self.payment_method
            purchase.save(update_fields=[
                'status', 'paid_at', 'payment_reference', 'payment_method',
            ])

    def diagnostics_tier(self):
        """باقة التشخيص اللي الإيصال ده بيدفعها.

        🐛 [FIX]: الباقة كانت بتتخزّن في «ملاحظات العميل» — ولو العميل كتب أي
        ملاحظة وهو بيرفع الإيصال كانت بتمسحها، فالتأكيد يفشل («باقة غير صالحة»)
        والخطأ يتبلع: الإيصال مؤكد والفلوس اتدفعت والباقة ماتفعّلتش. أول كلمة
        في الملاحظات (المسار الجديد بيحافظ عليها)، وإلا الباقة اللي سعرها = المبلغ.
        """
        from .diagnostics import CustomerDiagnosticsSubscription as Sub
        paid_tiers = {t: p for t, p in Sub.TIER_PRICES_EGP.items() if p > 0}
        first = ((self.notes or '').strip().split() or [''])[0]
        if first in paid_tiers:
            return first
        for tier, price in paid_tiers.items():
            if Decimal(str(price)) == Decimal(str(self.amount)):
                return tier
        raise ValidationError(f"مش عارف أنهي باقة تشخيص اتدفعت بمبلغ {self.amount} — راجع الإيصال.")

    @transaction.atomic
    def reject(self, by_user=None, notes: str = ''):
        # 🛡️ [FIX]: رفض إيصال متأكد كان بيقلبه "مرفوض" والخدمة شغالة/الفلوس
        #    في الـ Escrow — تضارب في السجلات. الإيصال المؤكد نهائي.
        if self.status == 'confirmed':
            raise ValidationError('الإيصال ده متأكد بالفعل — مينفعش يترفض.')
        self.status = 'rejected'
        self.reviewed_by = by_user if by_user and by_user.is_authenticated else None
        self.reviewed_at = timezone.now()
        self.review_notes = notes or 'لم يتم العثور على التحويل'
        self.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_notes'])

        # 🐛 [FIX]: رفض إيصال اشتراك كان بيسيب الفاتورة «مصدرة» للأبد — بتنفخ
        #    «المعلّق» في الإيرادات و«المستحق» في صفحة الشركة. بتتلغي.
        if self.purchase_type == 'subscription':
            purchase = self.get_purchase_object()
            if purchase is not None and getattr(purchase, 'status', '') == 'issued':
                from clients.services.platform_billing import void_invoice
                void_invoice(purchase, reason=f'رفض إيصال التحويل: {self.review_notes[:120]}', user=by_user)

        # 🐛 [FIX]: رفض إيصال قطعة غيار كان بيسيب الطلب pending_payment
        #    والقطعة "محجوزة" للأبد — محدش يقدر يشتريها تاني.
        if self.purchase_type == 'parts':
            purchase = self.get_purchase_object()
            if purchase is not None and purchase.status == 'pending_payment':
                from marketplace_b2b.services.parts_orders import cancel_unpaid
                cancel_unpaid(purchase, reason=f'تم رفض إيصال التحويل: {self.review_notes[:120]}')


# =====================================================================
# 🔁 SavedPaymentCard — كارت محفوظ عند Paymob للتجديد التلقائي للاشتراك
# =====================================================================
class SavedPaymentCard(models.Model):
    """كارت الشركة المحفوظ عند Paymob (Card Tokenization) — العميل بيختار «احفظ
    الكارت» في صفحة الدفع، وPaymob بيبعتلنا `type=TOKEN` (clients/services/auto_renew.py).

    مابنخزّنش رقم الكارت: بس الـ token (مايتخصمش بيه غير من حساب Paymob بتاعنا)
    وآخر ٤ أرقام للعرض. كارت واحد نشط لكل شركة — الجديد بيحل محل القديم.
    """
    tenant = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='saved_cards',
                               verbose_name=_("الشركة"))
    provider = models.CharField(max_length=20, default='paymob', verbose_name=_("بوابة الدفع"))
    token = models.CharField(max_length=255, verbose_name=_("Token"))
    masked_pan = models.CharField(max_length=32, blank=True, verbose_name=_("رقم الكارت (مخفي)"))
    card_subtype = models.CharField(max_length=20, blank=True, verbose_name=_("نوع الكارت"))
    source_order = models.CharField(max_length=64, blank=True, verbose_name=_("طلب الحفظ"))
    is_active = models.BooleanField(default=True, verbose_name=_("نشط"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("كارت محفوظ")
        verbose_name_plural = _("💳 الكروت المحفوظة")
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(fields=('provider', 'token'), name='uniq_saved_card_provider_token'),
        ]

    def __str__(self):
        return f"{self.tenant.name} — {self.card_subtype} •••• {self.last4}"

    @property
    def last4(self):
        return (self.masked_pan or '')[-4:]

