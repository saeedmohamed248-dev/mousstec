"""🔌 Wix integration — per-tenant connection config (public schema).

بيربط كل tenant في Mousstec بموقع Wix بتاعه. الـ super-admin بيدخل الـ
API key + Site ID مرة واحدة من صفحة الربط، وبعدها المزامنة بتشتغل:
  • المنتجات: Mousstec Product → Wix Stores (create/update by SKU).
  • المبيعات: Wix eCommerce orders → Mousstec SaleInvoice.

الموديل في public schema (زي VisitorLog) عشان الـ super-admin يقدر يدير كل
الروابط من مكان واحد. الـ api_key حسّاس — بيتعرض مقنّع في الأدمن.
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class WixConnection(models.Model):
    client = models.OneToOneField(
        'clients.Client', on_delete=models.CASCADE, related_name='wix_connection',
        verbose_name=_("الشركة"),
    )
    # 🔑 Wix credentials — API key من manage.wix.com + Site ID بتاع المتجر.
    api_key = models.TextField(verbose_name=_("Wix API Key"))
    site_id = models.CharField(max_length=100, verbose_name=_("Wix Site ID"))
    account_id = models.CharField(max_length=100, blank=True, verbose_name=_("Wix Account ID"))

    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_("نشط"))
    sync_products = models.BooleanField(default=True, verbose_name=_("مزامنة المنتجات (Mousstec→Wix)"))
    sync_orders = models.BooleanField(default=True, verbose_name=_("سحب المبيعات (Wix→Mousstec)"))

    # الفرع اللي مبيعات Wix بتتسجّل فيه كفواتير. null = أول فرع.
    default_branch_id = models.IntegerField(null=True, blank=True,
        verbose_name=_("فرع تسجيل مبيعات Wix"))

    # حالة آخر مزامنة
    last_product_sync_at = models.DateTimeField(null=True, blank=True)
    last_order_sync_at = models.DateTimeField(null=True, blank=True)
    products_pushed = models.PositiveIntegerField(default=0, verbose_name=_("منتجات تمت مزامنتها"))
    orders_imported = models.PositiveIntegerField(default=0, verbose_name=_("طلبات تم استيرادها"))
    last_error = models.TextField(blank=True, verbose_name=_("آخر خطأ"))
    last_test_ok = models.BooleanField(default=False, verbose_name=_("آخر اختبار اتصال ناجح"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("ربط Wix")
        verbose_name_plural = _("🔌 روابط Wix")

    def __str__(self):
        status = "✅" if (self.is_active and self.last_test_ok) else "⏸️"
        return f"{status} Wix — {self.client.name} ({self.site_id[:12]}…)"

    @property
    def masked_api_key(self) -> str:
        if not self.api_key:
            return ''
        k = self.api_key
        return f"{k[:6]}…{k[-4:]}" if len(k) > 12 else "••••"

    def mark_error(self, msg: str) -> None:
        self.last_error = (msg or '')[:2000]
        self.save(update_fields=['last_error', 'updated_at'])
