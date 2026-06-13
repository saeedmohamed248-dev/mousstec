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


