"""
📒 حسابات المنصة نفسها (Mouss Tec ككل) — مش حسابات الشركات.

قبل كده لوحة المالك كانت بتعرض الإيراد بس (فواتير الاشتراكات)، ومفيش أي مكان يتسجّل فيه
اللي بيتصرف على السيستم (سيرفرات، إعلانات، واتساب، ذكاء اصطناعي، مرتبات…) — فمفيش ربح
وخسارة حقيقية. هنا:
  * PlatformExpense: كل مصروف بعملته الأصلية + سعر الصرف وقت الدفع ⇒ قيمته بالجنيه ثابتة.
    المصروف الشهري المتكرر (سيرفر/مرتب) بيتنسخ لوحده أول كل شهر.
  * PlatformIncome: إيراد يدوي من برّه السيستم (استشارة، تركيب، منحة…). الاشتراكات
    وعمولات الأسواق بتتحسب أوتوماتيك من جداولها (clients/services/platform_finance.py).
"""
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

CURRENCY_CHOICES = (
    ('EGP', _('جنيه مصري')),
    ('USD', _('دولار')),
    ('EUR', _('يورو')),
    ('SAR', _('ريال سعودي')),
    ('AED', _('درهم إماراتي')),
    ('GBP', _('جنيه إسترليني')),
)

# بنود المصروفات — مجمّعة زي قايمة الدخل في شركات SaaS العالمية:
# تكلفة تشغيل الخدمة (COGS) ⇒ مجمل الربح، وبعدها المصروفات التشغيلية (OpEx).
EXPENSE_GROUPS = (
    ('cogs', _('تكلفة تشغيل الخدمة')),
    ('marketing', _('التسويق والمبيعات')),
    ('rnd', _('التطوير والمنتج')),
    ('ga', _('مصروفات إدارية وعمومية')),
)
EXPENSE_CATEGORIES = (
    # تكلفة تشغيل الخدمة (COGS)
    ('hosting', _('سيرفرات واستضافة'), 'cogs'),
    ('storage_backup', _('تخزين ونسخ احتياطي'), 'cogs'),
    ('ai_api', _('ذكاء اصطناعي (API)'), 'cogs'),
    ('whatsapp_sms', _('واتساب ورسائل SMS'), 'cogs'),
    ('email_service', _('خدمة الإيميل'), 'cogs'),
    ('payment_fees', _('رسوم بوابات الدفع'), 'cogs'),
    ('support_staff', _('فريق الدعم الفني'), 'cogs'),
    # التسويق والمبيعات
    ('ads', _('إعلانات (فيسبوك/جوجل/تيك توك)'), 'marketing'),
    ('content', _('محتوى وتصميم وفيديو'), 'marketing'),
    ('sales_commission', _('عمولات مبيعات ووسطاء'), 'marketing'),
    ('events', _('معارض وفعاليات'), 'marketing'),
    # التطوير والمنتج
    ('dev_salaries', _('مرتبات المطورين'), 'rnd'),
    ('dev_tools', _('أدوات واشتراكات برمجية'), 'rnd'),
    ('freelance', _('مستقلين (Freelance)'), 'rnd'),
    ('devices', _('أجهزة ومعدات'), 'rnd'),
    # إدارية وعمومية
    ('salaries', _('مرتبات إدارية'), 'ga'),
    ('office', _('مكتب وإيجار ومرافق'), 'ga'),
    ('domains', _('دومينات وشهادات'), 'ga'),
    ('legal_accounting', _('قانوني ومحاسبة'), 'ga'),
    ('taxes', _('ضرائب ورسوم حكومية'), 'ga'),
    ('bank_fees', _('مصاريف بنكية'), 'ga'),
    ('transport', _('مواصلات وسفر'), 'ga'),
    ('other', _('أخرى'), 'ga'),
)
CATEGORY_GROUP = {k: g for k, _l, g in EXPENSE_CATEGORIES}

PAYMENT_METHODS = (
    ('card', _('بطاقة بنكية')),
    ('bank', _('تحويل بنكي')),
    ('cash', _('كاش')),
    ('wallet', _('محفظة إلكترونية / إنستاباي')),
    ('paypal', _('PayPal')),
    ('other', _('أخرى')),
)

INCOME_CATEGORIES = (
    ('services', _('خدمات (تركيب/تدريب/استشارة)')),
    ('custom_dev', _('تطوير خاص لعميل')),
    ('hardware', _('بيع أجهزة')),
    ('investment', _('تمويل/استثمار')),
    ('other', _('أخرى')),
)

_CENT = Decimal('0.01')


class _Money(models.Model):
    """مبلغ بعملته الأصلية + سعر الصرف للجنيه وقت الدفع ⇒ amount_egp ثابت (مايتغيّرش لو الدولار اتحرك)."""
    date = models.DateField(db_index=True, verbose_name=_("التاريخ"))
    description = models.CharField(max_length=200, verbose_name=_("البيان"))
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))],
                                 verbose_name=_("المبلغ"))
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='EGP', verbose_name=_("العملة"))
    exchange_rate = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal('1'),
                                        validators=[MinValueValidator(Decimal('0.0001'))],
                                        verbose_name=_("سعر الصرف (بالجنيه)"))
    amount_egp = models.DecimalField(max_digits=16, decimal_places=2, editable=False, default=Decimal('0'),
                                     verbose_name=_("المبلغ بالجنيه"))
    notes = models.TextField(blank=True, default='', verbose_name=_("ملاحظات"))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ['-date', '-id']

    def save(self, *args, **kwargs):
        if self.currency == 'EGP':
            self.exchange_rate = Decimal('1')
        self.amount_egp = (Decimal(self.amount) * Decimal(self.exchange_rate)).quantize(_CENT, ROUND_HALF_UP)
        super().save(*args, **kwargs)


class PlatformExpense(_Money):
    category = models.CharField(max_length=30, choices=[(k, l) for k, l, _g in EXPENSE_CATEGORIES],
                                db_index=True, verbose_name=_("البند"))
    vendor = models.CharField(max_length=120, blank=True, default='', verbose_name=_("المورّد/الجهة"))
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS, default='card',
                                      verbose_name=_("طريقة الدفع"))
    receipt = models.FileField(upload_to='platform_finance/', blank=True, null=True, verbose_name=_("الإيصال"))
    is_recurring = models.BooleanField(default=False, verbose_name=_("بيتكرر كل شهر"))
    # المصروف المتكرر بيتنسخ شهرياً — النسخة بتشاور على الأصل (ومابيتكررش تاني لنفس الشهر)
    recurring_source = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name='copies')

    class Meta(_Money.Meta):
        verbose_name = _("مصروف المنصة")
        verbose_name_plural = _("مصروفات المنصة")
        constraints = [models.UniqueConstraint(fields=['recurring_source', 'date'],
                                               condition=models.Q(recurring_source__isnull=False),
                                               name='platform_expense_one_copy_per_month')]

    @property
    def group(self):
        return CATEGORY_GROUP.get(self.category, 'ga')

    def __str__(self):
        return f"{self.date} — {self.get_category_display()} — {self.amount_egp} EGP"


class PlatformIncome(_Money):
    category = models.CharField(max_length=20, choices=INCOME_CATEGORIES, default='services',
                                verbose_name=_("النوع"))
    source = models.CharField(max_length=120, blank=True, default='', verbose_name=_("من (العميل/الجهة)"))

    class Meta(_Money.Meta):
        verbose_name = _("إيراد يدوي للمنصة")
        verbose_name_plural = _("إيرادات يدوية للمنصة")

    def __str__(self):
        return f"{self.date} — {self.get_category_display()} — {self.amount_egp} EGP"
