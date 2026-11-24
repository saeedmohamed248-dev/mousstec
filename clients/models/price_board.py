"""📈 بورصة أسعار المطابع — كل مطبعة بتسعّر بنود ثابتة المواصفات كل يوم.

* ``PriceSpec``: بند بمواصفة ثابتة (١٠٠٠ كارت كوشيه ٣٥٠ جم وش وضهر…) أو خامة (ورق/فليكس/حبر)
  — عشان الأسعار تتقارن ببعض. بيتحط من مهاجرة ويتعدّل من الأدمن.
* ``PriceQuote``: سعر مطبعة لبند في يوم — يا بائع سوق (seller) يا شركة على السيستم (merchant).
  سعر واحد لكل بند لكل مطبعة في اليوم (التحديث في نفس اليوم بيعدّل نفس الصف) ⇒ عندنا تاريخ يومي.

العميل بيشوف نطاق (أقل–أعلى ومتوسط) من غير أسماء، وبس لو فيه ٣ مطابع على الأقل.
"""
from django.db import models
from django.utils.translation import get_language, gettext_lazy as _

__all__ = ['PriceSpec', 'PriceQuote']


class PriceSpec(models.Model):
    KIND_CHOICES = (
        ('product', _('منتج')),
        ('material', _('خامة')),
    )
    code = models.SlugField(max_length=40, unique=True)
    sector = models.CharField(max_length=20, default='printing', db_index=True)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default='product', db_index=True)
    name = models.CharField(max_length=120, verbose_name=_("البند"))
    name_en = models.CharField(max_length=120, blank=True)
    spec = models.CharField(max_length=200, blank=True, verbose_name=_("المواصفة الثابتة"))
    spec_en = models.CharField(max_length=200, blank=True)
    unit = models.CharField(max_length=60, verbose_name=_("الوحدة"))
    unit_en = models.CharField(max_length=60, blank=True)
    icon = models.CharField(max_length=40, default='fa-print')
    is_active = models.BooleanField(default=True, db_index=True)
    sort = models.IntegerField(default=0)

    class Meta:
        verbose_name = _("بند في بورصة الأسعار")
        verbose_name_plural = _("📈 بنود بورصة أسعار المطابع")
        ordering = ['kind', 'sort', 'id']

    def __str__(self):
        return f'{self.name} — {self.unit}'

    def _l(self, field):
        en = (get_language() or 'ar').startswith('en') and getattr(self, field + '_en')
        return en or getattr(self, field)

    @property
    def l_name(self):
        return self._l('name')

    @property
    def l_spec(self):
        return self._l('spec')

    @property
    def l_unit(self):
        return self._l('unit')


class PriceQuote(models.Model):
    spec = models.ForeignKey(PriceSpec, on_delete=models.CASCADE, related_name='quotes')
    seller = models.ForeignKey('clients.MarketSeller', on_delete=models.CASCADE, null=True, blank=True,
                               related_name='price_quotes')
    merchant = models.ForeignKey('clients.Client', on_delete=models.CASCADE, null=True, blank=True,
                                 related_name='price_quotes')
    governorate = models.CharField(max_length=40, db_index=True, verbose_name=_("المحافظة"))
    price = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("السعر"))
    quoted_on = models.DateField(db_index=True)
    # 🛡️ سعر بعيد جداً عن متوسط السوق ⇒ مابيدخلش في الحساب لحد ما يتراجع
    is_outlier = models.BooleanField(default=False, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("سعر مطبعة")
        verbose_name_plural = _("أسعار المطابع اليومية")
        ordering = ['-quoted_on', 'spec']
        constraints = [
            models.UniqueConstraint(fields=['spec', 'seller', 'quoted_on'], name='pricequote_one_per_seller_day',
                                    condition=models.Q(seller__isnull=False)),
            models.UniqueConstraint(fields=['spec', 'merchant', 'quoted_on'], name='pricequote_one_per_merchant_day',
                                    condition=models.Q(merchant__isnull=False)),
            models.CheckConstraint(condition=models.Q(seller__isnull=False) ^ models.Q(merchant__isnull=False),
                                   name='pricequote_seller_xor_merchant'),
            models.CheckConstraint(condition=models.Q(price__gt=0), name='pricequote_price_positive'),
        ]
        indexes = [models.Index(fields=['spec', 'quoted_on', 'governorate'])]

    def __str__(self):
        return f'{self.spec.name}: {self.price} ({self.quoted_on})'
