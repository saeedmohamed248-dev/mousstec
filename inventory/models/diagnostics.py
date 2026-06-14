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

# Vehicle diagnostics, repair log, RFQ flow, service reminders, feedback.

from .organization import *  # noqa: F401, F403
from .customers import *  # noqa: F401, F403
from .invoices import *  # noqa: F401, F403
from .catalog import *  # noqa: F401, F403

class VehicleDiagnosticReport(models.Model):
    """OBD scan attached to a Job Card (SaleInvoice with invoice_type='maintenance').
    Mobile app POSTs JSON to /api/obd/ingest/ with VIN + fault_codes + live PIDs."""
    SCAN_CHOICES = (
        ('pre_repair',  _('فحص قبل الإصلاح')),
        ('post_repair', _('فحص بعد الإصلاح / تأكيد جودة')),
        ('ad_hoc',      _('فحص خارجي')),
    )

    job_card = models.ForeignKey(
        'SaleInvoice', on_delete=models.CASCADE, null=True, blank=True,
        related_name='diagnostic_reports',
        verbose_name=_("بطاقة الإصلاح المرتبطة"),
    )
    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.PROTECT, related_name='diagnostic_reports',
        verbose_name=_("المركبة"),
    )
    engineer = models.ForeignKey(
        EmployeeProfile, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role__in': ['engineer', 'tech']},
        related_name='diagnostic_reports',
        verbose_name=_("المهندس / الفني"),
    )

    scan_type = models.CharField(max_length=20, choices=SCAN_CHOICES, verbose_name=_("نوع الفحص"))
    scanned_at = models.DateTimeField(default=timezone.now, db_index=True, verbose_name=_("وقت الفحص"))

    fault_codes = models.JSONField(default=list, blank=True, verbose_name=_("أكواد الأعطال (DTCs)"),
        help_text=_("مصفوفة مثل: ['P0171', 'P0300']"))
    live_data = models.JSONField(default=dict, blank=True, verbose_name=_("القراءات الحية (PIDs)"),
        help_text=_("RPM, coolant_temp_c, maf_gs, ..."))

    device_id = models.CharField(max_length=80, blank=True, verbose_name=_("معرّف جهاز المسح"))
    raw_payload = models.JSONField(default=dict, blank=True, editable=False)

    severity_score = models.IntegerField(default=0, verbose_name=_("درجة الخطورة (0-100)"))

    # ── Source provenance + AI analysis (Diagnostics Room save flow) ──
    SOURCE_MOBILE_INGEST = 'mobile_ingest'
    SOURCE_DIAG_ROOM = 'diag_room'
    SOURCE_CHOICES = (
        (SOURCE_MOBILE_INGEST, _('OBD مباشر من الموبايل')),
        (SOURCE_DIAG_ROOM,     _('غرفة تشخيص الأعطال (Web Bluetooth)')),
    )
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default=SOURCE_MOBILE_INGEST,
        verbose_name=_("مصدر التقرير"),
    )
    vin_snapshot = models.CharField(
        max_length=17, blank=True, verbose_name=_("VIN المقروء من السيارة"),
        help_text=_("VIN كما قرأه الـ ELM327 من السيارة — للتدقيق."),
    )
    ai_summary = models.TextField(
        blank=True, verbose_name=_("ملخص تحليل الذكاء الاصطناعي"),
        help_text=_("التحليل النهائي اللي هيشوفه مستشار الخدمة والعميل."),
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='diagnostic_reports_created',
        verbose_name=_("أنشأه"),
    )

    class Meta:
        verbose_name = _("تقرير OBD")
        verbose_name_plural = _("تقارير OBD التشخيصية")
        indexes = [
            models.Index(fields=['vehicle', '-scanned_at']),
            models.Index(fields=['job_card', 'source']),
        ]

    def __str__(self):
        return f"OBD #{self.id} — {self.vehicle} ({self.get_scan_type_display()})"


def _diag_photo_upload_path(instance, filename):
    """Group photos by report id + year/month so an advisor browsing a job
    card doesn't hit a single 100k-file flat folder."""
    from django.utils import timezone as _tz
    now = _tz.now()
    return (
        f'diagnostics/{now:%Y/%m}/report_{instance.report_id or "new"}/'
        f'{filename}'
    )


class VehicleDiagnosticPhoto(models.Model):
    """Photo evidence attached to a VehicleDiagnosticReport.

    Typical use: technician snaps a melted connector / cut harness in the
    AI Diagnostics Room. We persist the bytes so the service advisor can
    show the customer exactly what justified the labour line.
    """
    report = models.ForeignKey(
        VehicleDiagnosticReport, on_delete=models.CASCADE,
        related_name='photos', verbose_name=_("التقرير"),
    )
    image = models.ImageField(
        upload_to=_diag_photo_upload_path, verbose_name=_("الصورة"),
    )
    caption = models.CharField(
        max_length=240, blank=True, verbose_name=_("وصف مختصر"),
    )
    uploaded_at = models.DateTimeField(
        default=timezone.now, db_index=True, verbose_name=_("وقت الرفع"),
    )

    class Meta:
        verbose_name = _("صورة تشخيص")
        verbose_name_plural = _("صور التشخيص")
        ordering = ['uploaded_at']

    def __str__(self):
        return f"Photo #{self.id} — report #{self.report_id}"


class RepairLog(models.Model):
    """Technician's per-task work log on a Job Card — drives the Tech Workspace timer & flags."""
    STATUS_CHOICES = (
        ('open',    _('قيد التنفيذ')),
        ('paused',  _('متوقف مؤقتاً')),
        ('done',    _('مكتمل')),
        ('blocked', _('بانتظار قطع إضافية')),
    )

    job_card = models.ForeignKey(
        'SaleInvoice', on_delete=models.CASCADE, related_name='repair_logs',
        verbose_name=_("بطاقة الإصلاح"),
    )
    technician = models.ForeignKey(
        EmployeeProfile, on_delete=models.PROTECT, related_name='repair_logs',
        limit_choices_to={'role__in': ['tech', 'engineer']},
        verbose_name=_("الفني"),
    )

    task_title = models.CharField(max_length=160, verbose_name=_("عنوان المهمة"))
    tech_notes = models.TextField(blank=True, verbose_name=_("ملاحظات فنية"))

    started_at = models.DateTimeField(default=timezone.now, verbose_name=_("وقت البدء"))
    ended_at = models.DateTimeField(null=True, blank=True, verbose_name=_("وقت الانتهاء"))
    paused_seconds = models.IntegerField(default=0, verbose_name=_("ثوانٍ توقف"))
    last_paused_at = models.DateTimeField(null=True, blank=True, editable=False)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='open', verbose_name=_("الحالة"))
    needs_extra_parts = models.BooleanField(default=False, verbose_name=_("يحتاج قطع إضافية؟"))
    extra_parts_note = models.TextField(blank=True, verbose_name=_("تفاصيل القطع المطلوبة"))

    class Meta:
        verbose_name = _("سجل إصلاح")
        verbose_name_plural = _("سجلات الإصلاح (Tech)")
        indexes = [models.Index(fields=['job_card', '-started_at']),
                   models.Index(fields=['technician', 'status'])]

    @property
    def duration_minutes(self) -> int:
        end = self.ended_at or timezone.now()
        total = (end - self.started_at).total_seconds() - (self.paused_seconds or 0)
        return max(int(total // 60), 0)

    def __str__(self):
        return f"RepairLog #{self.id} — {self.task_title} ({self.get_status_display()})"


class RepairLogMedia(models.Model):
    MEDIA_KIND = (
        ('before', _('قبل الإصلاح')),
        ('after',  _('بعد الإصلاح')),
        ('issue',  _('مشكلة/تحذير')),
    )
    log = models.ForeignKey(RepairLog, on_delete=models.CASCADE, related_name='media')
    kind = models.CharField(max_length=10, choices=MEDIA_KIND, default='before', verbose_name=_("النوع"))
    image = models.ImageField(upload_to='repair_logs/%Y/%m/', verbose_name=_("الصورة"))
    caption = models.CharField(max_length=200, blank=True, verbose_name=_("تعليق"))
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("مرفق سجل إصلاح")
        verbose_name_plural = _("مرفقات سجلات الإصلاح")


class RFQ(models.Model):
    """Request-for-Quotation — fans out a part request to N vendors and
    rolls up to a single PurchaseInvoice (draft) once a quote is accepted.

    Why a dedicated model (not PurchaseInvoice draft):
        - PurchaseInvoice is single-vendor (FK PROTECT) → can't represent
          the "asking three suppliers in parallel" stage.
        - We need to track which vendor was asked, when, what they quoted,
          and ETA per vendor so the inventory manager can compare apples
          to apples before placing the actual PO.
    """
    STATUS_OPEN     = 'open'
    STATUS_QUOTED   = 'quoted'      # at least one vendor replied
    STATUS_ORDERED  = 'ordered'     # a quote was accepted → PO created
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = (
        (STATUS_OPEN,      _('مفتوح — في انتظار الردود')),
        (STATUS_QUOTED,    _('تم استلام عروض')),
        (STATUS_ORDERED,   _('تم تحويلها إلى أمر شراء')),
        (STATUS_CANCELLED, _('ملغاة')),
    )

    job_card = models.ForeignKey(
        'SaleInvoice', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='rfqs',
        verbose_name=_("بطاقة الإصلاح المرتبطة"),
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='rfqs',
        verbose_name=_("فرع الطلب"),
    )
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='rfqs',
        verbose_name=_("القطعة (إن وُجدت في الكاتالوج)"),
        help_text=_("قد تكون فارغة لو الـ AI اقترح P/N مش موجود لدينا بعد."),
    )

    # When the AI suggests a P/N that's NOT in our catalogue, we still want
    # to RFQ it — store the raw P/N + name from the suggestion.
    part_number_requested = models.CharField(
        max_length=120, verbose_name=_("رقم القطعة المطلوبة"),
    )
    part_name_requested = models.CharField(
        max_length=200, blank=True, verbose_name=_("اسم القطعة المطلوبة"),
    )
    quantity = models.PositiveIntegerField(default=1, verbose_name=_("الكمية"))
    notes = models.TextField(blank=True, verbose_name=_("ملاحظات للموردين"))

    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_OPEN,
    )

    requested_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='rfqs_requested',
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    accepted_quote = models.ForeignKey(
        'RFQQuote', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='accepted_for',
        verbose_name=_("العرض المُعتمد"),
    )
    purchase_invoice = models.ForeignKey(
        PurchaseInvoice, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='source_rfqs',
        verbose_name=_("أمر الشراء الناتج"),
    )

    class Meta:
        verbose_name = _("طلب تسعير (RFQ)")
        verbose_name_plural = _("طلبات التسعير")
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['job_card', 'status']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"RFQ #{self.id} — {self.part_number_requested} ({self.quantity}x)"


class RFQQuote(models.Model):
    """Per-vendor row inside an RFQ. Created at fan-out time with `sent_at`,
    the price + ETA fields filled in by the inventory manager when the
    vendor replies."""
    rfq = models.ForeignKey(
        RFQ, on_delete=models.CASCADE, related_name='quotes',
        verbose_name=_("الطلب الأصلي"),
    )
    vendor = models.ForeignKey(
        Vendor, on_delete=models.PROTECT, related_name='rfq_quotes',
        verbose_name=_("المورد"),
    )
    sent_at = models.DateTimeField(
        default=timezone.now, verbose_name=_("وقت إرسال الطلب"),
    )
    # Filled in when the vendor replies (via WhatsApp, the manager pastes it)
    quoted_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        verbose_name=_("السعر المعروض"),
    )
    quoted_eta_days = models.PositiveSmallIntegerField(
        null=True, blank=True, verbose_name=_("مدة التوريد (أيام)"),
    )
    quoted_at = models.DateTimeField(null=True, blank=True)
    notes = models.CharField(
        max_length=240, blank=True, verbose_name=_("ملاحظة المورد"),
    )

    class Meta:
        verbose_name = _("عرض تسعير")
        verbose_name_plural = _("عروض الموردين")
        constraints = [
            models.UniqueConstraint(
                fields=['rfq', 'vendor'], name='uniq_rfq_vendor',
            ),
        ]
        ordering = ['sent_at']

    def __str__(self):
        return f"Quote — {self.vendor.name} on RFQ #{self.rfq_id}"

    @property
    def has_response(self) -> bool:
        return self.quoted_price is not None


class ServiceReminderRule(models.Model):
    """Workshop-tunable maintenance interval rule.

    Each rule says: 'service X is due every N km OR M months'. The
    predictive engine evaluates these against every vehicle's last-done
    timestamps + mileage to decide who to nudge.

    Defaults are seeded in a data migration so a fresh workshop has
    working rules out of the box; the admin can then prune or tweak.
    """
    CATEGORY_OIL = 'engine_oil'
    CATEGORY_BRAKE_PADS = 'brake_pads'
    CATEGORY_SPARK = 'spark_plugs'
    CATEGORY_COOLANT = 'coolant'
    CATEGORY_TRANSMISSION = 'transmission_oil'
    CATEGORY_TIMING = 'timing_belt'
    CATEGORY_AIR_FILTER = 'air_filter'
    CATEGORY_CABIN_FILTER = 'cabin_filter'
    CATEGORY_BATTERY = 'battery'
    CATEGORY_WIPERS = 'wipers'
    CATEGORY_GENERAL = 'general'
    CATEGORY_CHOICES = (
        (CATEGORY_OIL,          _('زيت محرك')),
        (CATEGORY_BRAKE_PADS,   _('فحمات فرامل')),
        (CATEGORY_SPARK,        _('بوجيهات')),
        (CATEGORY_COOLANT,      _('مياه تبريد')),
        (CATEGORY_TRANSMISSION, _('زيت فتيس')),
        (CATEGORY_TIMING,       _('سير الكاتينة / التيمنج')),
        (CATEGORY_AIR_FILTER,   _('فلتر هواء')),
        (CATEGORY_CABIN_FILTER, _('فلتر مكيف')),
        (CATEGORY_BATTERY,      _('بطارية')),
        (CATEGORY_WIPERS,       _('مساحات')),
        (CATEGORY_GENERAL,      _('عام / صيانة دورية')),
    )

    SEVERITY_LOW = 'low'
    SEVERITY_MEDIUM = 'medium'
    SEVERITY_HIGH = 'high'
    SEVERITY_CHOICES = (
        (SEVERITY_LOW,    _('منخفضة')),
        (SEVERITY_MEDIUM, _('متوسطة')),
        (SEVERITY_HIGH,   _('عالية — تجنّب التأخير')),
    )

    name = models.CharField(max_length=120, verbose_name=_("اسم القاعدة"))
    category = models.CharField(
        max_length=24, choices=CATEGORY_CHOICES, default=CATEGORY_GENERAL,
        verbose_name=_("نوع الصيانة"),
    )
    interval_km = models.PositiveIntegerField(
        null=True, blank=True, verbose_name=_("الفاصل الزمني (كم)"),
        help_text=_("مثال: 10000 — اتركه فارغاً للقواعد الزمنية فقط."),
    )
    interval_months = models.PositiveSmallIntegerField(
        null=True, blank=True, verbose_name=_("الفاصل الزمني (أشهر)"),
        help_text=_("مثال: 6 — اتركه فارغاً للقواعد المعتمدة على الكيلومترات فقط."),
    )
    severity = models.CharField(
        max_length=8, choices=SEVERITY_CHOICES, default=SEVERITY_MEDIUM,
    )
    applies_to_brands = models.JSONField(
        default=list, blank=True, verbose_name=_("الماركات المنطبق عليها"),
        help_text=_("فارغة = تنطبق على كل الماركات. مثال: ['BMW','MINI']"),
    )
    is_active = models.BooleanField(default=True)
    whatsapp_template = models.TextField(
        blank=True, verbose_name=_("نص رسالة WhatsApp"),
        help_text=_("بمتغيرات: {customer}, {vehicle}, {rule}, {workshop}"),
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        verbose_name = _("قاعدة تذكير صيانة")
        verbose_name_plural = _("قواعد التذكير الدوري")
        constraints = [
            models.CheckConstraint(
                check=models.Q(interval_km__isnull=False) |
                      models.Q(interval_months__isnull=False),
                name='svc_rule_has_some_interval',
            ),
        ]
        ordering = ['category', 'name']

    def __str__(self):
        return f"{self.name} ({self.get_category_display()})"


class ServiceNudge(models.Model):
    """A specific (vehicle, rule) pair the predictive engine flagged as
    overdue/due/upcoming. Persisted so the CRM can manage outreach state
    (sent / dismissed / done) without re-deriving from scratch every load.
    """
    URGENCY_OVERDUE  = 'overdue'    # past the interval already
    URGENCY_DUE      = 'due'        # at or within 14 days of the interval
    URGENCY_UPCOMING = 'upcoming'   # 14-45 days away
    URGENCY_CHOICES = (
        (URGENCY_OVERDUE,  _('متأخر')),
        (URGENCY_DUE,      _('مستحق الآن')),
        (URGENCY_UPCOMING, _('قريباً')),
    )

    STATUS_PENDING   = 'pending'
    STATUS_SENT      = 'sent'
    STATUS_DISMISSED = 'dismissed'
    STATUS_DONE      = 'done'       # service was performed → close the loop
    STATUS_CHOICES = (
        (STATUS_PENDING,   _('في الانتظار')),
        (STATUS_SENT,      _('تم إرسال تذكير')),
        (STATUS_DISMISSED, _('تم التجاهل')),
        (STATUS_DONE,      _('تمت الصيانة')),
    )

    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.CASCADE, related_name='service_nudges',
        verbose_name=_("المركبة"),
    )
    rule = models.ForeignKey(
        ServiceReminderRule, on_delete=models.CASCADE,
        related_name='nudges', verbose_name=_("القاعدة"),
    )

    last_done_at = models.DateTimeField(null=True, blank=True)
    last_done_mileage = models.PositiveIntegerField(null=True, blank=True)
    due_at = models.DateField(null=True, blank=True)
    due_at_mileage = models.PositiveIntegerField(null=True, blank=True)
    reason = models.CharField(max_length=240, blank=True)

    urgency = models.CharField(
        max_length=10, choices=URGENCY_CHOICES, default=URGENCY_UPCOMING,
        db_index=True,
    )
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING,
        db_index=True,
    )

    sent_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='service_nudges_sent',
    )

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    refreshed_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        verbose_name = _("تذكير صيانة")
        verbose_name_plural = _("تذكيرات الصيانة (CRM)")
        constraints = [
            models.UniqueConstraint(
                fields=['vehicle', 'rule'],
                name='uniq_vehicle_rule_nudge',
            ),
        ]
        indexes = [
            models.Index(fields=['urgency', 'status']),
            models.Index(fields=['status', '-created_at']),
        ]
        ordering = ['urgency', 'due_at']

    def __str__(self):
        return f"Nudge: {self.vehicle} → {self.rule.name} ({self.urgency})"


class CustomerFeedback(models.Model):
    """Public UUID link sent to customer after invoice 'posted' for rating + signature."""
    sale_invoice = models.OneToOneField(
        'SaleInvoice', on_delete=models.CASCADE, related_name='feedback',
        verbose_name=_("الفاتورة"),
    )
    public_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False,
        verbose_name=_("رمز الرابط العام"))

    rating = models.PositiveSmallIntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        verbose_name=_("التقييم (1-5)"),
    )
    comment = models.TextField(blank=True, verbose_name=_("ملاحظات العميل"))

    received_in_good_condition = models.BooleanField(default=False,
        verbose_name=_("استلمت السيارة بحالة جيدة"))
    signature_image = models.ImageField(upload_to='signatures/%Y/%m/', null=True, blank=True,
        verbose_name=_("توقيع رقمي"))

    sent_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        verbose_name = _("تقييم وتوقيع عميل")
        verbose_name_plural = _("تقييمات وتوقيعات العملاء")

    @property
    def public_url(self) -> str:
        return f"/feedback/{self.public_token}/"

    def __str__(self):
        return f"Feedback INV#{self.sale_invoice_id} ({self.rating or '—'})"