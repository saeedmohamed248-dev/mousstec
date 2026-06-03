"""
🔧 Smart Diagnostics — Tenant-scoped models (per-tenant schema)
=================================================================
كل الـ models هنا بـ تعيش في الـ tenant schema. الـ isolation
مضمون بالـ django-tenants schema routing — مفيش حاجة محتاجة tenant FK.

Models:
  - DiagnosticDevice: جهاز OBD2 مربوط بمركبة (token-authenticated WS)
  - DiagnosticScan: جلسة فحص (live أو manual)
  - FaultLog: Digital Health Passport — كل DTC + resolution history
  - LiveTelemetryFrame: snapshots من الـ live stream (rolling window)
  - TestPlanExecution / TestStepResult: تنفيذ خطوات الـ ISTA injector
  - APICallLog: audit trail للـ external API calls (cost tracker)
"""
from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


User = get_user_model()


class DiagnosticDevice(models.Model):
    """جهاز OBD2 hardware مربوط بمركبة. الـ token هو المفتاح
    اللي بـ يـ authenticate الـ WebSocket لما الجهاز يبعت live data."""

    vehicle = models.ForeignKey(
        'inventory.Vehicle', on_delete=models.CASCADE,
        related_name='diagnostic_devices',
        verbose_name=_("المركبة"),
    )
    device_token = models.CharField(
        max_length=64, unique=True, db_index=True,
        verbose_name=_("توكن الجهاز"),
    )
    hardware_id = models.CharField(max_length=80, blank=True)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("جهاز فحص OBD")
        verbose_name_plural = _("🔌 أجهزة الفحص الميدانية")

    def __str__(self):
        return f"{self.vehicle.chassis_number[-6:]} → {self.device_token[:8]}…"


class DiagnosticScan(models.Model):
    """جلسة فحص واحدة. ممكن تكون live (من جهاز OBD) أو manual
    (الفني أدخل الكود)."""

    STATUS_CHOICES = (
        ('in_progress', _('قيد التنفيذ')),
        ('completed', _('مكتمل')),
        ('blocked_quota', _('تم الحجب — نفدت الحصة')),
        ('blocked_subscription', _('تم الحجب — اشتراك غير مفعّل')),
        ('error', _('خطأ')),
    )
    SOURCE_CHOICES = (
        ('live_obd', _('Live OBD device')),
        ('manual', _('إدخال يدوي')),
        ('api', _('REST API')),
    )

    vehicle = models.ForeignKey(
        'inventory.Vehicle', on_delete=models.CASCADE,
        related_name='diagnostic_scans',
    )
    device = models.ForeignKey(
        DiagnosticDevice, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='scans',
    )
    technician = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='diagnostic_scans',
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='manual')
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default='in_progress')

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    summary = models.TextField(blank=True)

    class Meta:
        verbose_name = _("جلسة فحص تشخيصي")
        verbose_name_plural = _("🩺 جلسات الفحص التشخيصي")
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['vehicle', '-started_at']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"Scan #{self.pk} {self.vehicle.car_plate or self.vehicle.chassis_number[-6:]}"


class FaultLog(models.Model):
    """Digital Health Passport — كل عطل اتكشف على المركبة + تاريخ الحل.
    دي الـ historical record اللي بـ يـ stay مع المركبة طول عمرها."""

    vehicle = models.ForeignKey(
        'inventory.Vehicle', on_delete=models.CASCADE,
        related_name='fault_history',
    )
    scan = models.ForeignKey(
        DiagnosticScan, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='faults',
    )
    dtc_code = models.CharField(max_length=8, db_index=True, verbose_name=_("كود العطل"))
    detected_at = models.DateTimeField(default=timezone.now)
    mileage_at_detection = models.IntegerField(null=True, blank=True)
    severity = models.CharField(max_length=10, default='medium')

    resolved_at = models.DateTimeField(null=True, blank=True, verbose_name=_("تاريخ الحل"))
    resolution_note = models.TextField(blank=True, verbose_name=_("ملاحظات الحل"))
    resolved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='resolved_faults',
    )

    class Meta:
        verbose_name = _("سجل عطل")
        verbose_name_plural = _("📖 الجواز الصحي للمركبة (Fault History)")
        ordering = ['-detected_at']
        indexes = [
            models.Index(fields=['vehicle', 'dtc_code', '-detected_at']),
            models.Index(fields=['resolved_at']),
        ]

    def __str__(self):
        status = '✅' if self.resolved_at else '🔴'
        return f"{status} {self.vehicle.chassis_number[-6:]} → {self.dtc_code}"


class LiveTelemetryFrame(models.Model):
    """Snapshots من الـ live OBD stream. بنحتفظ فقط بـ rolling window
    (مثلاً آخر 24 ساعة) — الـ celery task بـ يـ purge القديم."""

    scan = models.ForeignKey(
        DiagnosticScan, on_delete=models.CASCADE,
        related_name='telemetry_frames',
    )
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    rpm = models.IntegerField(null=True, blank=True)
    engine_load_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    coolant_temp_c = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    intake_temp_c = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    vehicle_speed_kph = models.IntegerField(null=True, blank=True)
    throttle_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    battery_v = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("إطار قياس حي")
        verbose_name_plural = _("📊 Live Telemetry Frames")
        ordering = ['-timestamp']
        indexes = [models.Index(fields=['scan', '-timestamp'])]


class TestPlanExecution(models.Model):
    """تنفيذ خطة فحص (ISTA-style) على عطل معين. بـ تـ generate
    من DTCDefinition.guided_steps."""

    STATUS_CHOICES = (
        ('pending', _('في الانتظار')),
        ('in_progress', _('قيد التنفيذ')),
        ('passed', _('نجح')),
        ('failed', _('فشل')),
        ('aborted', _('ألغي')),
    )
    scan = models.ForeignKey(
        DiagnosticScan, on_delete=models.CASCADE,
        related_name='test_plans',
    )
    dtc_code = models.CharField(max_length=8, db_index=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='pending')
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    final_conclusion = models.TextField(blank=True)

    class Meta:
        verbose_name = _("تنفيذ خطة فحص")
        verbose_name_plural = _("🧪 Interactive Test Plans")
        ordering = ['-started_at']


class TestStepResult(models.Model):
    """نتيجة خطوة واحدة في الـ test plan."""

    execution = models.ForeignKey(
        TestPlanExecution, on_delete=models.CASCADE,
        related_name='step_results',
    )
    step_number = models.IntegerField()
    title = models.CharField(max_length=200)
    action = models.TextField(blank=True)
    expected = models.TextField(blank=True)
    technician_observation = models.TextField(blank=True)
    passed = models.BooleanField(null=True, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['execution', 'step_number']
        unique_together = [('execution', 'step_number')]


class APICallLog(models.Model):
    """Audit log لكل external API call بـ تكلفته بالـ USD.
    بـ يـ feed الـ admin cost tracker."""

    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    provider = models.CharField(max_length=30)
    endpoint = models.CharField(max_length=80)
    dtc_code = models.CharField(max_length=8, blank=True)
    vin = models.CharField(max_length=17, blank=True)
    cache_hit = models.BooleanField(default=False, help_text=_("True = served من cache، مفيش تكلفة"))
    cost_usd = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    http_status = models.IntegerField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True)
    triggered_by = models.ForeignKey(
        User, on_delete=models.SET_NULL,
        null=True, blank=True,
    )

    class Meta:
        verbose_name = _("سجل مكالمة API")
        verbose_name_plural = _("💰 API Call Cost Log")
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['provider', '-timestamp']),
            models.Index(fields=['cache_hit']),
        ]

    def __str__(self):
        tag = '🟢 cache' if self.cache_hit else f'💸 ${self.cost_usd}'
        return f"[{self.provider}.{self.endpoint}] {tag}"
