# 🛡️ حارس المرتجعات بالصور + بصمة القطعة بالذكاء الاصطناعي
# ============================================================
# الفكرة: أول ما القطعة تتصرف (تتباع/تتسلّم) بنصوّرها والذكاء الاصطناعي
# يستخرج "بصمة" مرئية لها (علامات مميزة، أرقام تسلسلية ظاهرة، حالة السطح).
# لما ترجع بنصوّرها تاني والذكاء الاصطناعي يقارن الراجع بالمصروف ويقول
# تنفع ترجع ولا لأ — ولو مش هتنفع يقول السبب بالتفصيل.
#
# نفس الحارس بيخدم موقع FixIt: العميل يصوّر القطعة قبل الشرا (baseline)
# وبعد الشرا (وقت الإرجاع) والموقع يبعتهم هنا فيرجع له الحكم والسبب.
import uuid

from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _

from .catalog import Product
from .customers import Customer
from .invoices import SaleInvoice, SaleInvoiceItem


def _return_photo_upload_path(instance, filename):
    """تجميع الصور حسب الحارس + السنة/الشهر لتفادي مجلد واحد ضخم."""
    now = timezone.now()
    guard_id = getattr(instance, 'guard_id', None) or 'new'
    return f'return_guard/{now:%Y/%m}/guard_{guard_id}/{instance.stage}_{filename}'


class PartReturnGuard(models.Model):
    """سجل حماية إرجاع لقطعة مباعة بعينها (سطر فاتورة أو طلب موقع).

    يحمل بصمة القطعة وقت الصرف (dispatch_fingerprint) والحكم على المرتجع
    (verdict). مصدر الحقيقة للبصمة هو صورة المحل وقت الصرف؛ صور العميل
    من الموقع تُحفظ كـ baseline احتياطي لو مفيش بصمة محل.
    """

    SOURCE_CHOICES = (
        ('internal', _('المحل (داخلي)')),
        ('website', _('موقع FixIt')),
    )
    STATUS_CHOICES = (
        ('awaiting_dispatch', _('بانتظار تصوير الصرف')),
        ('fingerprinted', _('تم تثبيت بصمة الصرف')),
        ('return_requested', _('طلب إرجاع قيد الفحص')),
        ('return_approved', _('الإرجاع مقبول')),
        ('return_rejected', _('الإرجاع مرفوض')),
        ('needs_human', _('يحتاج مراجعة بشرية')),
    )

    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name='return_guards',
        verbose_name=_("القطعة"),
    )
    part_number = models.CharField(
        max_length=100, db_index=True,
        verbose_name=_("Part Number (مطابقة الموقع = SKU)"),
    )
    invoice_item = models.ForeignKey(
        SaleInvoiceItem, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='return_guards', verbose_name=_("سطر الفاتورة"),
    )
    original_invoice = models.ForeignKey(
        SaleInvoice, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='return_guards', verbose_name=_("الفاتورة الأصلية"),
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='return_guards', verbose_name=_("العميل"),
    )

    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default='internal',
        verbose_name=_("مصدر الحارس"),
    )
    external_ref = models.CharField(
        max_length=120, blank=True, db_index=True,
        verbose_name=_("مرجع خارجي (رقم طلب الموقع)"),
    )
    public_token = models.UUIDField(
        default=uuid.uuid4, unique=True, editable=False,
        verbose_name=_("رمز عام آمن للموقع"),
    )

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='awaiting_dispatch',
        db_index=True, verbose_name=_("الحالة"),
    )

    # 🧬 بصمة الصرف — مصدر الحقيقة (JSON من الذكاء الاصطناعي)
    dispatch_fingerprint = models.JSONField(
        null=True, blank=True, verbose_name=_("بصمة القطعة وقت الصرف"),
    )
    dispatch_analyzed_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("وقت تحليل بصمة الصرف"),
    )

    # ⚖️ حكم المرتجع
    return_fingerprint = models.JSONField(
        null=True, blank=True, verbose_name=_("بصمة القطعة وقت الإرجاع"),
    )
    verdict = models.JSONField(
        null=True, blank=True,
        verbose_name=_("الحكم (returnable / match_score / reasons)"),
    )
    verdict_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("وقت إصدار الحكم"),
    )

    notes = models.TextField(blank=True, verbose_name=_("ملاحظات"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("حارس مرتجعات القطعة")
        verbose_name_plural = _("حرّاس مرتجعات القطع")
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=['part_number', 'external_ref'], name='inventory_p_part_nu_idx'),
        ]

    def __str__(self):
        return f"Guard #{self.pk} — {self.part_number} ({self.get_status_display()})"

    # ----- helpers -----
    @property
    def is_returnable(self):
        """None = لسه متحكمش عليها / يحتاج بشري، True/False = الحكم."""
        if not isinstance(self.verdict, dict):
            return None
        return self.verdict.get('returnable')

    @property
    def match_score(self):
        if isinstance(self.verdict, dict):
            return self.verdict.get('match_score')
        return None

    @property
    def rejection_reasons(self):
        if isinstance(self.verdict, dict):
            return self.verdict.get('reasons') or []
        return []

    @property
    def reasons_text(self):
        return " • ".join(str(r) for r in self.rejection_reasons)


class PartReturnPhoto(models.Model):
    """صورة مرفقة بحارس المرتجعات (صرف / إرجاع / صور العميل من الموقع)."""

    STAGE_CHOICES = (
        ('dispatch', _('صورة الصرف (المحل)')),
        ('return', _('صورة الإرجاع (المحل)')),
        ('customer_pre', _('صورة العميل قبل الشراء')),
        ('customer_post', _('صورة العميل عند الإرجاع')),
    )
    SOURCE_CHOICES = (
        ('internal', _('المحل')),
        ('website', _('موقع FixIt')),
    )

    guard = models.ForeignKey(
        PartReturnGuard, on_delete=models.CASCADE, related_name='photos',
        verbose_name=_("الحارس"),
    )
    stage = models.CharField(
        max_length=20, choices=STAGE_CHOICES, verbose_name=_("المرحلة"),
    )
    image = models.ImageField(
        upload_to=_return_photo_upload_path, blank=True, null=True,
        verbose_name=_("الصورة"),
    )
    image_url_external = models.URLField(
        blank=True, verbose_name=_("رابط صورة مستضافة على الموقع"),
    )
    sha256 = models.CharField(
        max_length=64, blank=True, db_index=True,
        verbose_name=_("بصمة النزاهة (SHA-256)"),
    )
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default='internal',
        verbose_name=_("المصدر"),
    )
    uploaded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name=_("رفعها"),
    )
    uploaded_at = models.DateTimeField(
        default=timezone.now, db_index=True, verbose_name=_("وقت الرفع"),
    )

    class Meta:
        verbose_name = _("صورة مرتجع")
        verbose_name_plural = _("صور المرتجعات")
        ordering = ('uploaded_at',)

    def __str__(self):
        return f"{self.get_stage_display()} — guard #{self.guard_id}"
