"""
🧬 Diagnostics Catalog — Shared Knowledge Base (public schema)
=================================================================
Models هنا بتعيش في الـ public schema. كل الـ tenants بـ يقرأوا
من نفس الـ catalog عشان نـ amortize الـ external API cost.

- DTCDefinition: مرجع أكواد الأعطال (OBD2). seed من open-source.
- VINDecodeCache: cache دائم لردود NHTSA VIN decoder.
- DTCExternalLookupCache: cache دائم لردود CarMD/pay-per-call providers.
- APICostRate: تسعيرة المزودين الخارجيين (للـ admin cost tracker).
"""
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


SEVERITY_CHOICES = (
    ('info', _('معلوماتي')),
    ('low', _('منخفض')),
    ('medium', _('متوسط')),
    ('high', _('حرج')),
    ('critical', _('فوري — أوقف السيارة')),
)


class DTCDefinition(models.Model):
    """مرجع أكواد الأعطال (OBD2 / manufacturer-specific).
    Seed من open-source community databases."""

    SYSTEM_CHOICES = (
        ('P', _('Powertrain (P)')),
        ('C', _('Chassis (C)')),
        ('B', _('Body (B)')),
        ('U', _('Network/Comms (U)')),
    )

    code = models.CharField(
        max_length=8, unique=True, db_index=True,
        verbose_name=_("DTC Code"),
        help_text=_("e.g. P0301, U0100"),
    )
    system = models.CharField(max_length=1, choices=SYSTEM_CHOICES, default='P')
    short_description = models.CharField(max_length=200, verbose_name=_("الوصف المختصر"))
    full_description = models.TextField(blank=True, verbose_name=_("الوصف التفصيلي"))
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default='medium')

    # خطوات التشخيص الإرشادية (Interactive Test Plan)
    # Shape: [{"step": 1, "title": "...", "action": "...", "expected": "..."}]
    guided_steps = models.JSONField(default=list, blank=True, verbose_name=_("خطوات الفحص الموجَّه"))

    # OEM parts الاحتمالية المسببة للعطل (cross-reference مع inventory.Product.oem_cross_reference)
    likely_oem_parts = models.JSONField(
        default=list, blank=True,
        verbose_name=_("OEM parts المرجحة"),
        help_text=_("قائمة OEM part numbers محتمل ارتباطها بالعطل"),
    )

    source = models.CharField(
        max_length=30, default='community',
        help_text=_("community | carmd | manual"),
    )
    is_generic = models.BooleanField(
        default=True,
        help_text=_("True = OBD2 generic؛ False = manufacturer-specific"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("تعريف كود عطل (DTC)")
        verbose_name_plural = _("📚 DTC Catalog — مرجع الأكواد")
        ordering = ['code']
        indexes = [models.Index(fields=['system', 'severity'])]

    def __str__(self):
        return f"{self.code} — {self.short_description[:50]}"


class VINDecodeCache(models.Model):
    """Cache دائم لردود NHTSA vPIC. أي VIN يتـ decode مرة واحدة فقط
    على مستوى السيستم كله."""

    vin = models.CharField(max_length=17, unique=True, db_index=True)
    decoded_data = models.JSONField(verbose_name=_("بيانات الـ VIN المفكوكة"))
    make = models.CharField(max_length=80, blank=True)
    model = models.CharField(max_length=80, blank=True)
    model_year = models.IntegerField(null=True, blank=True)
    engine = models.CharField(max_length=120, blank=True)
    provider = models.CharField(max_length=30, default='nhtsa')
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = _("VIN decode cache")
        verbose_name_plural = _("🔐 VIN Decode Cache")
        ordering = ['-fetched_at']

    def __str__(self):
        return f"{self.vin} ({self.make} {self.model} {self.model_year or ''})"


class DTCExternalLookupCache(models.Model):
    """Cache دائم لردود الـ pay-per-call providers (CarMD وأمثاله).
    الـ unique constraint بـ يضمن إن مفيش duplicate API call ممكن يحصل."""

    dtc_code = models.CharField(max_length=8, db_index=True)
    vehicle_signature = models.CharField(
        max_length=120, db_index=True, blank=True,
        help_text=_("e.g. 'BMW|F30|2015|N20' — to differentiate same DTC across vehicles"),
    )
    provider = models.CharField(max_length=30, default='carmd')
    payload = models.JSONField()
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = _("DTC external lookup cache")
        verbose_name_plural = _("🔐 DTC External Cache")
        unique_together = [('dtc_code', 'vehicle_signature', 'provider')]
        ordering = ['-fetched_at']

    def __str__(self):
        sig = self.vehicle_signature or 'generic'
        return f"{self.provider}:{self.dtc_code}@{sig}"


class APICostRate(models.Model):
    """تسعيرة المزودين الخارجيين بالـ USD. يستخدمها الـ DTCResolver
    لتسجيل التكلفة في APICallLog."""

    provider = models.CharField(max_length=30)
    endpoint = models.CharField(max_length=80, help_text=_("e.g. 'dtc_lookup', 'vin_decode'"))
    cost_usd = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        verbose_name = _("سعر مكالمة API")
        verbose_name_plural = _("💵 API Cost Rates")
        unique_together = [('provider', 'endpoint')]
        ordering = ['provider', 'endpoint']

    def __str__(self):
        return f"{self.provider}.{self.endpoint} = ${self.cost_usd}"
