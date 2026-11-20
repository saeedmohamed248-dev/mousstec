"""🧾 المطابع — الشيكات (أوراق القبض والدفع)."""
from decimal import Decimal
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator
from .customers import PrintCustomer
from .production import PrintOrder
from .purchasing import PrintSupplier
from .treasury import PrintTransaction, PrintTreasury, PrintTreasuryTransfer


# =====================================================================
# 🧾 19. الشيكات (أوراق القبض والدفع)
# =====================================================================

class PrintCheque(models.Model):
    """
    شيك وارد من عميل أو صادر لمورد.

    🐛 [FIX]: الشيك كان بيتسجّل فلوس في الخزنة يوم استلامه — وهو لسه ماتصرفش،
    ولو رجع مفيش طريقة ترجّع المديونية. دلوقتي الشيك الوارد بيدخل «حافظة الشيكات»
    (أوراق قبض) وبيسدّد الطلب، والتحصيل بينقله للبنك، والمرتجع بيرجّع المديونية على
    العميل. والصادر بيسدّد المورد ويفضل «أوراق دفع» لحد ما يتصرف من البنك.
    """
    DIRECTION_CHOICES = (
        ('in', _('شيك وارد من عميل')),
        ('out', _('شيك صادر لمورد')),
    )
    STATUS_CHOICES = (
        ('pending', _('في الحافظة — لسه ماتصرفش')),
        ('cleared', _('اتصرف')),
        ('bounced', _('مرتجع / اتلغى')),
    )
    direction = models.CharField(max_length=3, choices=DIRECTION_CHOICES, default='in', verbose_name=_("النوع"))
    number = models.CharField(max_length=50, verbose_name=_("رقم الشيك"))
    bank_name = models.CharField(max_length=100, blank=True, verbose_name=_("البنك المسحوب عليه"))
    issue_date = models.DateField(default=timezone.localdate, verbose_name=_("تاريخ الاستلام / الإصدار"))
    due_date = models.DateField(verbose_name=_("تاريخ الاستحقاق"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))],
                                 verbose_name=_("المبلغ"))
    customer = models.ForeignKey(PrintCustomer, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name='cheques', verbose_name=_("العميل"))
    order = models.ForeignKey(PrintOrder, on_delete=models.PROTECT, null=True, blank=True,
                              related_name='cheques', verbose_name=_("الطلب"))
    supplier = models.ForeignKey(PrintSupplier, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name='cheques', verbose_name=_("المورد"))
    purchase = models.ForeignKey('MaterialPurchase', on_delete=models.PROTECT, null=True, blank=True,
                                 related_name='cheques', verbose_name=_("فاتورة الشراء"))
    bill = models.ForeignKey('PrintExpenseBill', on_delete=models.PROTECT, null=True, blank=True,
                             related_name='cheques', verbose_name=_("فاتورة المصروف"))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending', editable=False,
                              verbose_name=_("الحالة"))
    txn = models.OneToOneField(PrintTransaction, on_delete=models.SET_NULL, null=True, blank=True,
                               editable=False, related_name='+')
    settle_transfer = models.OneToOneField('PrintTreasuryTransfer', on_delete=models.SET_NULL, null=True,
                                           blank=True, editable=False, related_name='+')
    bounce_txn = models.OneToOneField(PrintTransaction, on_delete=models.SET_NULL, null=True, blank=True,
                                      editable=False, related_name='+')
    settled_on = models.DateField(null=True, blank=True, editable=False, verbose_name=_("تاريخ الصرف"))
    bounced_on = models.DateField(null=True, blank=True, editable=False, verbose_name=_("تاريخ الرجوع"))
    notes = models.CharField(max_length=300, blank=True, verbose_name=_("ملاحظات"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_("بواسطة"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("شيك")
        verbose_name_plural = _("الشيكات (أوراق قبض ودفع)")
        ordering = ['due_date', 'id']

    def __str__(self):
        return f"{self.get_direction_display()} #{self.number} — {self.amount:,.2f}"

    @property
    def party_name(self):
        party = self.customer or (self.order.customer if self.order_id else None) or self.supplier
        return party.name if party else ''

    def clean(self):
        super().clean()
        if self.direction == 'in':
            if not (self.customer_id or self.order_id):
                raise ValidationError(_("الشيك الوارد لازم يتربط بعميل أو طلب."))
            if self.supplier_id or self.purchase_id or self.bill_id:
                raise ValidationError(_("الشيك الوارد مايتربطش بمورد."))
        else:
            if not (self.supplier_id or self.purchase_id or self.bill_id):
                raise ValidationError(_("الشيك الصادر لازم يتربط بمورد أو فاتورة."))
            if self.customer_id or self.order_id:
                raise ValidationError(_("الشيك الصادر مايتربطش بعميل."))
        if self.due_date and self.issue_date and self.due_date < self.issue_date:
            raise ValidationError(_("تاريخ الاستحقاق قبل تاريخ الاستلام."))

    def _wallet(self):
        return PrintTreasury.system_wallet('cheques_in' if self.direction == 'in' else 'cheques_out')

    def _links(self):
        if self.direction == 'in':
            return {'order_id': self.order_id, 'customer_id': self.customer_id}
        return {'purchase_id': self.purchase_id, 'bill_id': self.bill_id, 'supplier_id': self.supplier_id}

    @staticmethod
    def _dt(day):
        if day == timezone.localdate():
            return timezone.now()
        return timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time()))

    def save(self, *args, **kwargs):
        from django.db import transaction
        if self.pk:
            # بعد التسجيل الملاحظات بس اللي بتتعدّل — الفلوس بتتحرك من «صرف» و«رجوع».
            return super().save(update_fields=['notes'])
        self.clean()
        with transaction.atomic():
            super().save(*args, **kwargs)
            label = _("شيك وارد") if self.direction == 'in' else _("شيك صادر")
            bank = f" — {self.bank_name}" if self.bank_name else ''
            self.txn = PrintTransaction.objects.create(
                treasury=self._wallet(), transaction_type='in' if self.direction == 'in' else 'out',
                amount=self.amount, created_by=self.created_by, date=self._dt(self.issue_date),
                description=f"{label} رقم {self.number}{bank} — يستحق {self.due_date:%Y-%m-%d}", **self._links())
            PrintCheque.objects.filter(pk=self.pk).update(txn=self.txn)

    def clear(self, treasury, on_date=None, user=None):
        """الشيك اتصرف: الوارد بيتنقل من الحافظة للبنك، والصادر بيتخصم من البنك."""
        from django.db import transaction
        on_date = on_date or timezone.localdate()
        if not treasury.is_money:
            raise ValidationError(_("اختار البنك أو الخزنة اللي الشيك اتصرف فيها."))
        with transaction.atomic():
            cheque = PrintCheque.objects.select_for_update().get(pk=self.pk)
            if cheque.status != 'pending':
                raise ValidationError(_("الشيك ده اتقفل قبل كده (%(s)s).") % {'s': cheque.get_status_display()})
            wallet = cheque._wallet()
            src, dst = (wallet, treasury) if cheque.direction == 'in' else (treasury, wallet)
            transfer = PrintTreasuryTransfer.objects.create(
                from_treasury=src, to_treasury=dst, amount=cheque.amount, date=self._dt(on_date), created_by=user,
                notes=f"صرف شيك رقم {cheque.number}")
            PrintCheque.objects.filter(pk=cheque.pk).update(status='cleared', settled_on=on_date,
                                                           settle_transfer=transfer)
        self.refresh_from_db()
        return self

    def bounce(self, on_date=None, user=None, reason=''):
        """الشيك رجع (أو اتلغى): المديونية بترجع على العميل — أو للمورد لو كان صادر."""
        from django.db import transaction
        on_date = on_date or timezone.localdate()
        with transaction.atomic():
            cheque = PrintCheque.objects.select_for_update().get(pk=self.pk)
            if cheque.status != 'pending':
                raise ValidationError(_("الشيك ده اتقفل قبل كده (%(s)s).") % {'s': cheque.get_status_display()})
            why = f" — {reason}" if reason else ''
            bounce = PrintTransaction.objects.create(
                treasury=cheque._wallet(), transaction_type='out' if cheque.direction == 'in' else 'in',
                amount=cheque.amount, created_by=user, date=self._dt(on_date),
                description=f"شيك مرتجع رقم {cheque.number}{why}", **cheque._links())
            PrintCheque.objects.filter(pk=cheque.pk).update(status='bounced', bounced_on=on_date, bounce_txn=bounce)
        self.refresh_from_db()
        return self

    def delete(self, *args, **kwargs):
        """الشيك اللي لسه في الحافظة بيتمسح بحركته (اتسجّل غلط)؛ اللي اتصرف أو رجع لأ."""
        from django.db import transaction
        if self.status != 'pending':
            raise ValidationError(_("الشيك اتصرف أو رجع — مابيتمسحش."))
        with transaction.atomic():
            txn = PrintTransaction.objects.filter(pk=self.txn_id).first()
            result = super().delete(*args, **kwargs)
            if txn is not None:
                txn._with_cheque = True
                txn.delete()
            return result
