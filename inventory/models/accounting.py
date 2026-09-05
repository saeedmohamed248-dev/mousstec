"""
🏛️ Full Accounting Cycle — Accrual double-entry backbone.

This module adds the document layer that turns the flat ``AccountingEntry``
ledger into a real, standards-grade accounting system:

    FiscalYear  ─┬─ AccountingPeriod ──┐
                 │                      │
                 └──────────────────────┤
                                        ▼
        JournalEntry (قيد يومية) ── lines ──▶ AccountingEntry
                                        │
                    posted / draft / reversed
                                        │
             Trial Balance ▸ Income Statement ▸ Balance Sheet ▸ Cash Flow

Every economic event (sale, purchase, payment, adjustment, closing) is a
balanced ``JournalEntry`` whose lines are ``AccountingEntry`` rows. Debits
must equal credits before an entry can post — enforced at the service layer
and re-asserted here — so the general ledger is *always* balanced.

All models live in TENANT schemas (per company), so the numbering,
fiscal calendar and ledger are fully isolated per company.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# Re-use the existing ledger primitives (ChartOfAccount / AccountingEntry).
from .finance import ChartOfAccount, AccountingEntry  # noqa: F401


# =====================================================================
# 📅 Fiscal calendar
# =====================================================================
class FiscalYear(models.Model):
    """سنة مالية — تجمع فترات محاسبية (شهور) وتُقفل ككل عند نهاية العام."""

    code = models.CharField(max_length=20, unique=True, verbose_name=_("رمز السنة المالية"))
    name = models.CharField(max_length=100, verbose_name=_("اسم السنة المالية"))
    start_date = models.DateField(verbose_name=_("بداية السنة"))
    end_date = models.DateField(verbose_name=_("نهاية السنة"))
    is_closed = models.BooleanField(default=False, db_index=True, verbose_name=_("مُقفلة"))
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ الإقفال"))
    closed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("سنة مالية")
        verbose_name_plural = _("السنوات المالية")
        ordering = ['-start_date']

    def __str__(self):
        return f"{self.name} ({self.start_date} → {self.end_date})"

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError(_("نهاية السنة المالية يجب أن تكون بعد بدايتها."))

    @classmethod
    def for_date(cls, on_date):
        return cls.objects.filter(start_date__lte=on_date, end_date__gte=on_date).first()


class AccountingPeriod(models.Model):
    """فترة محاسبية (شهر عادةً) — أصغر وحدة يمكن إقفالها ومنع القيد فيها."""

    fiscal_year = models.ForeignKey(
        FiscalYear, on_delete=models.CASCADE, related_name='periods',
        verbose_name=_("السنة المالية"),
    )
    name = models.CharField(max_length=100, verbose_name=_("اسم الفترة"))
    start_date = models.DateField(db_index=True, verbose_name=_("بداية الفترة"))
    end_date = models.DateField(db_index=True, verbose_name=_("نهاية الفترة"))
    is_closed = models.BooleanField(default=False, db_index=True, verbose_name=_("مُقفلة"))
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ الإقفال"))
    closed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )

    class Meta:
        verbose_name = _("فترة محاسبية")
        verbose_name_plural = _("الفترات المحاسبية")
        ordering = ['start_date']
        unique_together = ('fiscal_year', 'start_date', 'end_date')

    def __str__(self):
        lock = " 🔒" if self.is_closed else ""
        return f"{self.name}{lock}"

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError(_("نهاية الفترة يجب أن تكون بعد بدايتها."))

    @classmethod
    def for_date(cls, on_date):
        """أرجع الفترة التي يقع فيها التاريخ (إن وُجدت)."""
        return cls.objects.filter(start_date__lte=on_date, end_date__gte=on_date).first()


# =====================================================================
# 📒 Journal Entry (قيد اليومية) — the balanced document
# =====================================================================
class JournalEntry(models.Model):
    """
    قيد يومية متوازن — مستند محاسبي يجمع عدة سطور (مدين/دائن) يجب أن
    يتساوى مجموع مدينها مع مجموع دائنها قبل الترحيل (post).
    """

    JOURNAL_TYPES = (
        ('sales', _('يومية المبيعات')),
        ('purchase', _('يومية المشتريات')),
        ('cash_receipt', _('يومية المقبوضات')),
        ('cash_payment', _('يومية المدفوعات')),
        ('general', _('يومية عامة')),
        ('opening', _('قيد افتتاحي')),
        ('adjustment', _('قيد تسوية')),
        ('closing', _('قيد إقفال')),
    )
    STATUS_CHOICES = (
        ('draft', _('مسودة')),
        ('posted', _('مُرحَّل')),
        ('reversed', _('معكوس')),
    )

    number = models.CharField(
        max_length=32, db_index=True, blank=True, verbose_name=_("رقم القيد"),
    )
    date = models.DateField(default=timezone.localdate, db_index=True, verbose_name=_("تاريخ القيد"))
    journal_type = models.CharField(
        max_length=20, choices=JOURNAL_TYPES, default='general',
        db_index=True, verbose_name=_("نوع اليومية"),
    )
    reference = models.CharField(max_length=100, db_index=True, blank=True, verbose_name=_("المرجع"))
    description = models.CharField(max_length=255, verbose_name=_("البيان"))
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default='posted',
        db_index=True, verbose_name=_("الحالة"),
    )
    period = models.ForeignKey(
        AccountingPeriod, null=True, blank=True, on_delete=models.PROTECT,
        related_name='journal_entries', verbose_name=_("الفترة المحاسبية"),
    )

    # Source-document linkage (any one, or none for manual entries)
    sale_invoice = models.ForeignKey(
        'SaleInvoice', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='journal_entries',
    )
    purchase_invoice = models.ForeignKey(
        'PurchaseInvoice', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='journal_entries',
    )
    financial_transaction = models.ForeignKey(
        'FinancialTransaction', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='journal_entries',
    )
    reversal_of = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='reversed_by', verbose_name=_("عكس للقيد"),
    )

    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("قيد يومية")
        verbose_name_plural = _("قيود اليومية (Journal)")
        ordering = ['-date', '-id']
        indexes = [
            models.Index(fields=['journal_type', '-date'], name='inv_je_type_date_idx'),
            models.Index(fields=['status', '-date'], name='inv_je_status_date_idx'),
        ]

    def __str__(self):
        return f"{self.number or f'JE#{self.pk}'} — {self.description}"

    # -- balance helpers ------------------------------------------------
    @property
    def total_debit(self):
        return self.lines.aggregate(t=models.Sum('debit'))['t'] or Decimal('0.00')

    @property
    def total_credit(self):
        return self.lines.aggregate(t=models.Sum('credit'))['t'] or Decimal('0.00')

    @property
    def is_balanced(self):
        return self.total_debit == self.total_credit

    def assert_balanced(self):
        td, tc = self.total_debit, self.total_credit
        if td != tc:
            raise ValidationError(
                _("قيد غير متوازن (#%(n)s): مدين=%(d)s دائن=%(c)s")
                % {'n': self.number or self.pk, 'd': td, 'c': tc}
            )
        if td == Decimal('0.00'):
            raise ValidationError(_("قيد فارغ — لا يحتوي على أي مبالغ."))
        return True


# =====================================================================
# 🧾 Tax rate (ضريبة القيمة المضافة وغيرها)
# =====================================================================
class TaxRate(models.Model):
    """معدل ضريبي — يربط نسبة الضريبة بحساب الالتزام الذي تُرحَّل إليه."""

    name = models.CharField(max_length=100, verbose_name=_("اسم الضريبة"))
    rate = models.DecimalField(
        max_digits=6, decimal_places=3, default=Decimal('0.000'),
        verbose_name=_("النسبة %"),
    )
    account = models.ForeignKey(
        ChartOfAccount, null=True, blank=True, on_delete=models.PROTECT,
        related_name='tax_rates', verbose_name=_("حساب الضريبة المستحقة"),
    )
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = _("معدل ضريبي")
        verbose_name_plural = _("المعدلات الضريبية")
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.rate}%)"
