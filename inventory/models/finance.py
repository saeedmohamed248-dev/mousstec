from django.db import models, transaction, connection
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from simple_history.models import HistoricalRecords
from django.utils.translation import gettext_lazy as _
from decimal import Decimal
from datetime import timedelta
from django.contrib.auth.models import User
from django.db.models import F, Sum, Q, ExpressionWrapper, DecimalField

import uuid
import logging

logger = logging.getLogger('mouss_tec_core')

# Treasury, expenses, transactions, accounting ledger, bank statements.

from .organization import *  # noqa: F401, F403
from .catalog import *  # noqa: F401, F403
from .customers import *  # noqa: F401, F403

class Treasury(models.Model):
    TYPE_CHOICES = (('cash', _('كاش')), ('bank', _('حساب بنكي')), ('visa', _('فيزا')), ('wallet', _('محفظة')))
    name = models.CharField(max_length=100, verbose_name=_("اسم الخزنة/الحساب"))
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='treasuries', verbose_name=_("الفرع"))
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='cash', verbose_name=_("النوع"))
    balance = models.DecimalField(max_digits=15, decimal_places=2, default=0.00, verbose_name=_("الرصيد الأساسي"))
    is_active = models.BooleanField(default=True, verbose_name=_("نشط"))
    history = HistoricalRecords()
    class Meta:
        verbose_name = _("خزنة / حساب")
        verbose_name_plural = _("الخزائن والحسابات")
        unique_together = ('name', 'branch')
    def __str__(self): return f"{self.name} ({self.balance})"

class ExpenseCategory(models.Model):
    SYSTEM_KEYS = (
        ('salaries',  _('رواتب وأجور')),
        ('rent',      _('إيجار')),
        ('utilities', _('مرافق')),
        ('other',     _('أخرى')),
    )
    name = models.CharField(max_length=100, unique=True, verbose_name=_("بند المصروف"))
    system_key = models.CharField(
        max_length=30, choices=SYSTEM_KEYS, blank=True, db_index=True,
        verbose_name=_("مفتاح النظام"),
        help_text=_("مفتاح ثابت للتعرف الآلي — مثلاً 'salaries' يفعّل قائمة الموظفين تلقائياً"),
    )
    class Meta: verbose_name_plural = _("بنود المصروفات")
    def __str__(self): return self.name

class FinancialTransaction(models.Model):
    TRANSACTION_TYPES = (('in', _('إيداع / إيراد')), ('out', _('سحب / مصروف')))
    CURRENCY_CHOICES = (('EGP', 'جنية مصري'), ('AED', 'درهم إماراتي'), ('USD', 'دولار أمريكي'))

    treasury = models.ForeignKey(Treasury, on_delete=models.PROTECT, related_name='transactions', verbose_name=_("الخزنة"))
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES, verbose_name=_("النوع"))
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='EGP', verbose_name=_("العملة"))
    exchange_rate = models.DecimalField(max_digits=10, decimal_places=4, default=1.0000, verbose_name=_("سعر الصرف وقت العملية"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("المبلغ (بالعملة المحلية)"))

    category = models.ForeignKey(ExpenseCategory, null=True, blank=True, on_delete=models.SET_NULL, verbose_name=_("البند"))
    description = models.CharField(max_length=255, verbose_name=_("البيان"))
    date = models.DateTimeField(default=timezone.now, verbose_name=_("التاريخ"))

    employee = models.ForeignKey(EmployeeProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name='financial_transactions', verbose_name=_("الموظف (للرواتب/السلف)"))

    sale_invoice = models.ForeignKey('SaleInvoice', null=True, blank=True, on_delete=models.SET_NULL, related_name='payments', verbose_name=_("فاتورة بيع"))
    purchase_invoice = models.ForeignKey('PurchaseInvoice', null=True, blank=True, on_delete=models.SET_NULL, related_name='payments', verbose_name=_("فاتورة شراء"))
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL, verbose_name=_("دفعة من عميل"))
    vendor = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.SET_NULL, verbose_name=_("دفعة لمورد"))
    history = HistoricalRecords()
    class Meta: verbose_name_plural = _("الخزينة (حركات مالية)")
    def __str__(self): return f"{self.amount} {self.currency} - {self.treasury.name}"

# =====================================================================
# 📦 6. الفواتير والعمليات المتطورة (Odoo Standard Workflow)
# =====================================================================
class ChartOfAccount(models.Model):
    ACCOUNT_TYPES = (
        ('asset', _('أصول (Assets)')),
        ('liability', _('خصوم (Liabilities)')),
        ('equity', _('حقوق ملكية (Equity)')),
        ('revenue', _('إيرادات (Revenue)')),
        ('expense', _('مصروفات (Expenses)')),
    )
    code = models.CharField(max_length=20, unique=True, verbose_name=_("رقم الحساب"))
    name = models.CharField(max_length=200, verbose_name=_("اسم الحساب"))
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPES, verbose_name=_("نوع الحساب"))
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL, related_name='children', verbose_name=_("الحساب الأب"))
    is_active = models.BooleanField(default=True, verbose_name=_("نشط"))
    description = models.TextField(blank=True, verbose_name=_("وصف"))

    class Meta:
        verbose_name = _("حساب محاسبي")
        verbose_name_plural = _("دليل الحسابات (Chart of Accounts)")
        ordering = ['code']

    def __str__(self):
        return f"{self.code} — {self.name}"

    @property
    def balance(self):
        agg = self.entries.aggregate(
            total_debit=models.Sum('debit'),
            total_credit=models.Sum('credit')
        )
        d = agg['total_debit'] or Decimal('0')
        c = agg['total_credit'] or Decimal('0')
        if self.account_type in ('asset', 'expense'):
            return d - c
        return c - d


class AccountingEntry(models.Model):
    entry_date = models.DateTimeField(default=timezone.now, db_index=True, verbose_name=_("تاريخ القيد"))
    reference = models.CharField(max_length=100, db_index=True, verbose_name=_("المرجع"))
    description = models.CharField(max_length=255, verbose_name=_("البيان"))
    account = models.ForeignKey(ChartOfAccount, on_delete=models.PROTECT, related_name='entries', verbose_name=_("الحساب"))
    debit = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'), verbose_name=_("مدين"))
    credit = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'), verbose_name=_("دائن"))
    sale_invoice = models.ForeignKey('SaleInvoice', null=True, blank=True, on_delete=models.SET_NULL, related_name='accounting_entries')
    purchase_invoice = models.ForeignKey('PurchaseInvoice', null=True, blank=True, on_delete=models.SET_NULL, related_name='accounting_entries')
    financial_transaction = models.ForeignKey('FinancialTransaction', null=True, blank=True, on_delete=models.SET_NULL, related_name='accounting_entries')
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        verbose_name = _("قيد محاسبي")
        verbose_name_plural = _("القيود المحاسبية (Accounting Ledger)")
        ordering = ['-entry_date']
        indexes = [
            models.Index(fields=['reference']),
            models.Index(fields=['account', '-entry_date']),
        ]

    def clean(self):
        """Validate that either debit or credit is set, not both."""
        if self.debit > 0 and self.credit > 0:
            raise ValidationError(_("القيد لا يمكن أن يكون مدين ودائن في نفس الوقت."))
        if self.debit == 0 and self.credit == 0:
            raise ValidationError(_("القيد يجب أن يحتوي على قيمة مدينة أو دائنة."))

    @classmethod
    def validate_balanced(cls, reference):
        """Verify all entries for a given reference are balanced (total debit == total credit)."""
        agg = cls.objects.filter(reference=reference).aggregate(
            total_debit=models.Sum('debit'),
            total_credit=models.Sum('credit')
        )
        total_debit = agg['total_debit'] or Decimal('0')
        total_credit = agg['total_credit'] or Decimal('0')
        if total_debit != total_credit:
            raise ValidationError(
                _(f"القيود غير متوازنة للمرجع {reference}: "
                  f"مدين={total_debit}, دائن={total_credit}")
            )
        return True

    def __str__(self):
        side = f"مدين {self.debit}" if self.debit > 0 else f"دائن {self.credit}"
        return f"{self.reference} | {self.account.name} | {side}"


# =====================================================================
# 🏦 المطابقة البنكية (Bank Reconciliation)
# =====================================================================
class BankStatement(models.Model):
    """كشف بنكي مستورد من البنك — لمطابقته مع حركات الخزينة."""
    treasury = models.ForeignKey(
        'Treasury', on_delete=models.CASCADE, related_name='bank_statements',
        verbose_name=_("الخزينة / الحساب البنكي")
    )
    statement_date = models.DateField(verbose_name=_("تاريخ الكشف"))
    period_start = models.DateField(verbose_name=_("بداية الفترة"))
    period_end = models.DateField(verbose_name=_("نهاية الفترة"))
    opening_balance = models.DecimalField(max_digits=15, decimal_places=2, verbose_name=_("الرصيد الافتتاحي"))
    closing_balance = models.DecimalField(max_digits=15, decimal_places=2, verbose_name=_("الرصيد الختامي"))
    uploaded_file = models.FileField(upload_to='bank_statements/%Y/%m/', blank=True, null=True)
    is_reconciled = models.BooleanField(default=False, verbose_name=_("تمت المطابقة"))
    reconciled_at = models.DateTimeField(null=True, blank=True)
    reconciled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("كشف بنكي")
        verbose_name_plural = _("🏦 كشوف البنوك")
        ordering = ['-statement_date']

    def __str__(self):
        return f"كشف {self.treasury.name} — {self.statement_date}"


class BankStatementLine(models.Model):
    """سطر واحد من الكشف البنكي."""
    DIRECTION_CHOICES = (
        ('debit', _('سحب (مدين)')),
        ('credit', _('إيداع (دائن)')),
    )
    statement = models.ForeignKey(BankStatement, on_delete=models.CASCADE, related_name='lines')
    transaction_date = models.DateField()
    description = models.CharField(max_length=300)
    reference = models.CharField(max_length=100, blank=True, db_index=True, verbose_name=_("مرجع البنك"))
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)

    # Reconciliation linkage
    matched_transaction = models.ForeignKey(
        'FinancialTransaction', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='bank_lines', verbose_name=_("الحركة المالية المطابقة"),
    )
    match_confidence = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'),
        help_text=_("0-100 — ثقة المطابقة التلقائية")
    )
    is_matched = models.BooleanField(default=False, db_index=True)
    matched_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = _("سطر كشف بنكي")
        verbose_name_plural = _("سطور كشوف البنوك")
        ordering = ['transaction_date', 'pk']
        indexes = [
            models.Index(fields=['statement', 'is_matched']),
            models.Index(fields=['transaction_date', 'amount']),
        ]

    def __str__(self):
        sign = '+' if self.direction == 'credit' else '-'
        return f"{self.transaction_date} | {sign}{self.amount} | {self.description[:50]}"

    def auto_match(self):
        """
        🤖 محاولة مطابقة تلقائية مع حركة في FinancialTransaction.
        يبحث بنفس التاريخ ±3 أيام ونفس المبلغ.
        يرجع الـ confidence score (0-100).
        """
        from datetime import timedelta as _td
        if self.is_matched:
            return 100

        target_type = 'in' if self.direction == 'credit' else 'out'
        candidates = FinancialTransaction.objects.filter(
            treasury=self.statement.treasury,
            transaction_type=target_type,
            amount=self.amount,
            date__date__gte=self.transaction_date - _td(days=3),
            date__date__lte=self.transaction_date + _td(days=3),
        ).exclude(bank_lines__is_matched=True)

        # Best match: same date + amount = 100% confidence (only if unique)
        exact_qs = candidates.filter(date__date=self.transaction_date)
        exact_count = exact_qs.count()
        if exact_count == 1:
            self.matched_transaction = exact_qs.first()
            self.match_confidence = Decimal('100.00')
            self.is_matched = True
            self.matched_at = timezone.now()
            self.save(update_fields=['matched_transaction', 'match_confidence', 'is_matched', 'matched_at'])
            return 100
        elif exact_count > 1:
            # Multiple candidates — flag for manual review at 50% confidence
            self.matched_transaction = exact_qs.first()
            self.match_confidence = Decimal('50.00')
            self.is_matched = False
            self.matched_at = timezone.now()
            self.save(update_fields=['matched_transaction', 'match_confidence', 'is_matched', 'matched_at'])
            return 50

        # Near match: same amount within ±3 days = 80%
        near = candidates.first()
        if near:
            self.matched_transaction = near
            self.match_confidence = Decimal('80.00')
            self.is_matched = True
            self.matched_at = timezone.now()
            self.save(update_fields=['matched_transaction', 'match_confidence', 'is_matched', 'matched_at'])
            return 80

        return 0


# =====================================================================
# 📦 سجل حركات المخزون (Inventory Movement Tracker)
# =====================================================================
