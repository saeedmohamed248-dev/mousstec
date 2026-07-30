"""💱 Multi-currency foundation — global exchange-rate table.

الأرقام المحاسبية كلها بتفضل بالجنيه المصري (base currency) — الجدول ده
**عرض** فقط: بيحوّل الأسعار للعميل بعملته المفضلة عند الطلب، وبيستقبل
تحديثات الصرف من regional_tax_forex_sync_webhook أو مهمة يومية.

قرار معماري متعمّد: مانلمسش الـ ledger (FinancialTransaction / EscrowLedger /
PlatformInvoice) عشان تحويل دفاتر الأستاذ لـ multi-currency تغيير جذري
وخطير على نظام مالي شغّال — بيتعمل في مرحلة منفصلة بـ migration محاسبية
كاملة. دلوقتي: تحويل عرض آمن + بنية جاهزة.

جدول مشترك (public schema) — كل الـ tenants بيقروا من نفس الأسعار.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# العملة الأساسية — كل الأرصدة والقيود بيها. مايتغيّرش من غير migration محاسبية.
BASE_CURRENCY = 'EGP'

# العملات المدعومة للعرض. الرمز + عدد الخانات العشرية للعرض.
SUPPORTED_CURRENCIES = {
    'EGP': {'symbol': 'ج.م', 'name': 'جنيه مصري', 'decimals': 2},
    'USD': {'symbol': '$', 'name': 'دولار أمريكي', 'decimals': 2},
    'EUR': {'symbol': '€', 'name': 'يورو', 'decimals': 2},
    'SAR': {'symbol': 'ر.س', 'name': 'ريال سعودي', 'decimals': 2},
    'AED': {'symbol': 'د.إ', 'name': 'درهم إماراتي', 'decimals': 2},
    'KWD': {'symbol': 'د.ك', 'name': 'دينار كويتي', 'decimals': 3},
}


class ExchangeRate(models.Model):
    """سعر صرف من BASE_CURRENCY لعملة أخرى.

    rate = كام وحدة من الـ target تساوي وحدة واحدة EGP.
    مثال: 1 EGP = 0.02 USD → target='USD', rate=0.02.
    آخر صف (أحدث fetched_at) لكل عملة هو المعتمد.
    """
    target_currency = models.CharField(
        max_length=3, db_index=True, verbose_name=_("العملة"),
        help_text=_("رمز ISO 4217 — USD, EUR, SAR…"))
    rate = models.DecimalField(
        max_digits=18, decimal_places=8, verbose_name=_("سعر الصرف (مقابل 1 EGP)"))
    source = models.CharField(max_length=50, blank=True, verbose_name=_("المصدر"),
                              help_text=_("webhook / manual / api provider name"))
    fetched_at = models.DateTimeField(default=timezone.now, db_index=True,
                                      verbose_name=_("وقت التحديث"))

    class Meta:
        verbose_name = _("سعر صرف")
        verbose_name_plural = _("💱 أسعار الصرف")
        ordering = ['target_currency', '-fetched_at']
        indexes = [
            models.Index(fields=['target_currency', '-fetched_at']),
        ]

    def __str__(self):
        return f"1 {BASE_CURRENCY} = {self.rate} {self.target_currency} @ {self.fetched_at:%Y-%m-%d %H:%M}"
