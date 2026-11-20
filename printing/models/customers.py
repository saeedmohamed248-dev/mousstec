"""🏢 المطابع — الفروع والعملاء (PrintBranch, PrintCustomer)."""
from decimal import Decimal
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator


# =====================================================================
# 🏢 1. الفروع والعملاء (Branch & Customer — مستقل عن inventory)
# =====================================================================

class PrintBranch(models.Model):
    """فرع المطبعة / الاستوديو"""
    name = models.CharField(max_length=100, verbose_name=_("اسم الفرع"))
    address = models.TextField(blank=True, verbose_name=_("العنوان"))
    phone = models.CharField(max_length=20, blank=True, verbose_name=_("الهاتف"))
    is_active = models.BooleanField(default=True, verbose_name=_("نشط"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("فرع")
        verbose_name_plural = _("الفروع")
        ordering = ['name']

    def __str__(self):
        return self.name


class PrintCustomer(models.Model):
    """عملاء المطبعة / الاستوديو"""
    name = models.CharField(max_length=150, verbose_name=_("اسم العميل"))
    phone = models.CharField(max_length=20, blank=True, verbose_name=_("الهاتف"))
    whatsapp = models.CharField(max_length=20, blank=True, verbose_name=_("واتساب"))
    email = models.EmailField(blank=True, verbose_name=_("البريد الإلكتروني"))
    company = models.CharField(max_length=150, blank=True, verbose_name=_("اسم الشركة"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    credit_limit = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, validators=[MinValueValidator(Decimal('0.00'))],
        verbose_name=_("حد الائتمان"),
        help_text=_("أقصى مديونية مسموحة على العميل — الطلب اللي يعدّيها بيفضل مسودة لحد ما يدفع. 0 = من غير حد."))
    # 📖 المديونية قبل السيستم — من غيرها كانت بتتسجّل طلب أو دفعة وهمية وتبوّظ الأرباح.
    opening_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, verbose_name=_("رصيد افتتاحي"),
        help_text=_("المديونية قبل ما تبدأ على السيستم: موجب = العميل عليه لينا، سالب = ليه رصيد عندنا."))
    opening_date = models.DateField(default=timezone.localdate, verbose_name=_("تاريخ الرصيد الافتتاحي"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("تاريخ الإضافة"))

    class Meta:
        verbose_name = _("عميل")
        verbose_name_plural = _("العملاء")
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.company})" if self.company else self.name

    def payments_qs(self):
        """كل فلوس العميل: دفعات على طلباته + دفعات على حسابه (من غير طلب)."""
        from .treasury import PrintTransaction  # استيراد محلي لتفادي الدائرية
        return PrintTransaction.objects.filter(
            models.Q(order__customer=self) | models.Q(order__isnull=True, customer=self))

    def account_summary(self):
        """
        رصيد العميل على مستوى الحساب كله (مش طلب طلب).

        * due         — المستحق على الطلبات الشغالة (مش مسودة ولا ملغية)
        * payments    — صافي فلوسه اللي دخلت الخزنة (دفعات − مردودات)
        * balance     — due − payments: موجب = عليه، سالب = ليه رصيد
        * outstanding — المتبقّي على طلباته بعد اللي اتسدّد عليها
        * credit      — رصيد مقدّم متاح (فلوس دافعها ومش متسدّدة على طلب)
        * opening_outstanding — الباقي من الرصيد الافتتاحي (بعد الإعدام/الخصم والدفعات على الحساب)

        الرصيد الافتتاحي المدين بيتسدّد من الدفعات على الحساب قبل أي حاجة؛ والدائن
        (فلوس ليه قبل السيستم) بيتحسب كأنه دفعة.
        """
        from .production import PrintOrder  # استيراد محلي لتفادي الدائرية
        zero = Decimal('0')
        due = applied = outstanding = zero
        for o in self.printorder_set.only('status', 'total_amount', 'discount', 'tax_amount', 'paid_amount',
                                          'credited_amount'):
            o_due = o.due_amount if o.status not in PrintOrder.NON_DEBT_STATUSES else zero
            due += o_due
            outstanding += max(zero, o_due - o.paid_amount)
            applied += max(zero, min(o.paid_amount, o_due))
        agg = self.payments_qs().aggregate(
            ins=models.Sum('amount', filter=models.Q(transaction_type='in')),
            outs=models.Sum('amount', filter=models.Q(transaction_type='out')))
        payments = (agg['ins'] or zero) - (agg['outs'] or zero)
        base = self.account_level_debt()
        opening_left = zero
        if base > 0:
            due += base
            used = min(base, max(zero, payments - applied))
            applied += used
            opening_left = base - used
            outstanding += opening_left
        elif base < 0:
            payments += -base
        return {
            'due': due, 'payments': payments, 'balance': due - payments,
            'outstanding': outstanding, 'credit': max(zero, payments - applied),
            'opening_outstanding': opening_left,
        }

    def account_level_debt(self):
        """الرصيد الافتتاحي − الإشعارات الدائنة على الحساب نفسه (إعدام/خصم من غير طلب)."""
        zero = Decimal('0')
        if not self.pk:
            return self.opening_balance or zero
        agg = self.credit_notes.filter(order__isnull=True).aggregate(
            a=models.Sum('amount'), t=models.Sum('tax_amount'))
        return (self.opening_balance or zero) - (agg['a'] or zero) - (agg['t'] or zero)

    @property
    def balance(self):
        return self.account_summary()['balance']

    @property
    def available_credit(self):
        return self.account_summary()['credit']

    def over_credit_limit(self):
        """رسالة لو مديونية العميل عدّت حد الائتمان، وإلا None (0 = من غير حد)."""
        if not self.credit_limit or self.credit_limit <= 0:
            return None
        balance = self.account_summary()['balance']
        if balance <= self.credit_limit:
            return None
        return (f"العميل «{self.name}» عدّى حد الائتمان: المديونية {balance:,.2f} "
                f"والحد {self.credit_limit:,.2f}. حصّل دفعة أو ارفع الحد الأول.")
