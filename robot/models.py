"""
robot/models.py — persistence for the Mouss Tec physical edge-agent.

The robot is built from recycled BMW / MINI parts: an Arduino Mega 2560 drives
8 relays (head pan, two arms with self-locking, two track motors) and talks over
serial to an ESP32, which handles Wi-Fi, audio (INMP441 mic + MAX98357A amp) and
the API. An ESP32-CAM handles vision (part recognition, scrap-condition, faces).

These models are the audit trail and the bridge between that hardware and the
existing ERP apps (`inventory`, `hr`, `bmw_ecu`, `diagnostics_catalog`). They do
NOT duplicate ERP data — a scan links to `inventory.Product`, an access log links
to `hr.Employee`, a sale links to `inventory.SaleInvoice`.

Tenant-scoped (per-workshop schema), same as `inventory`/`hr`.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class RobotDevice(models.Model):
    """One physical robot (or one head unit) registered to a branch.

    The ESP32 authenticates with a per-device API token and sends a heartbeat;
    `last_seen_at` + `is_online` let the dashboard show which branches have a
    live robot.
    """

    name = models.CharField(max_length=80, verbose_name=_("اسم الروبوت"))
    # Stable hardware id burned into the ESP32 (e.g. its MAC). Used to route
    # scans/commands and to scope the device token.
    device_uid = models.CharField(
        max_length=64, unique=True, db_index=True,
        verbose_name=_("معرف الجهاز (ESP32 UID)"),
    )
    branch = models.ForeignKey(
        "inventory.Branch", on_delete=models.CASCADE,
        related_name="robots", verbose_name=_("الفرع"),
    )
    # Opaque bearer token the firmware sends in `X-Robot-Token`. Hashed at rest
    # would be stronger; kept simple here and rotated from the admin.
    api_token = models.CharField(
        max_length=64, unique=True, db_index=True,
        verbose_name=_("توكن الجهاز"),
    )
    firmware_version = models.CharField(max_length=20, blank=True, default="")
    is_active = models.BooleanField(default=True, verbose_name=_("مفعّل؟"))
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name=_("آخر اتصال"))
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("جهاز روبوت")
        verbose_name_plural = _("🤖 أجهزة الروبوت")
        ordering = ["-last_seen_at"]

    def __str__(self) -> str:
        return f"{self.name} @ {self.branch.name}"

    @property
    def is_online(self) -> bool:
        """Online if we've heard from it in the last 90 seconds."""
        if not self.last_seen_at:
            return False
        return (timezone.now() - self.last_seen_at).total_seconds() < 90


class RobotScanEvent(models.Model):
    """A vision event: the ESP32-CAM captured a part and the backend classified it.

    Covers both the POS flow (new part for sale) and the used/scrap flow
    (condition assessment → dynamic price suggestion). The recognized product is
    linked when confident; otherwise the raw guess/label is kept for review.
    """

    PURPOSE = [
        ("pos", _("بيع (POS)")),
        ("scrap", _("تقييم مستعمل/خردة")),
        ("lookup", _("استعلام مخزون")),
    ]

    device = models.ForeignKey(
        RobotDevice, on_delete=models.CASCADE, related_name="scans",
        verbose_name=_("الجهاز"),
    )
    purpose = models.CharField(max_length=10, choices=PURPOSE, default="pos")
    image = models.ImageField(
        upload_to="robot/scans/%Y/%m/", blank=True, null=True,
        verbose_name=_("صورة القطعة"),
    )
    # What the vision model thinks it is (free text label + normalized code).
    recognized_label = models.CharField(max_length=200, blank=True, default="")
    recognized_part_number = models.CharField(max_length=100, blank=True, default="")
    confidence = models.FloatField(default=0.0, verbose_name=_("درجة الثقة"))
    product = models.ForeignKey(
        "inventory.Product", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="robot_scans", verbose_name=_("المنتج المطابق"),
    )
    # Scrap flow: 0.0 (destroyed) .. 1.0 (like new); drives dynamic pricing.
    condition_score = models.FloatField(null=True, blank=True, verbose_name=_("درجة الحالة"))
    condition_notes = models.JSONField(default=dict, blank=True, verbose_name=_("ملاحظات الحالة"))
    # Retail price the robot verbally suggested (NEVER a wholesale figure).
    suggested_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        verbose_name=_("السعر المقترح (قطاعي)"),
    )
    # Set once a sale is generated from this scan.
    sale_invoice = models.ForeignKey(
        "inventory.SaleInvoice", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="robot_scans", verbose_name=_("فاتورة البيع"),
    )
    created_by = models.ForeignKey(
        "hr.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="robot_scans", verbose_name=_("الموظف المصرّح"),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("مسح بصري")
        verbose_name_plural = _("📷 المسح البصري")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["-created_at"])]

    def __str__(self) -> str:
        return f"Scan {self.recognized_label or '?'} ({self.confidence:.0%})"


class RobotVoiceInteraction(models.Model):
    """A hands-free voice turn: mic → speech-to-text → intent → spoken reply.

    The workshop assistant ("Do we have a water pump for a BMW E90?") and the POS
    prompts ("customer name?", "cash or credit?") both land here for audit and for
    improving the intent classifier over time.
    """

    INTENT = [
        ("inventory_query", _("استعلام مخزون")),
        ("pos_answer", _("رد على سؤال بيع")),
        ("diagnostic", _("سؤال تشخيصي/عطل")),
        ("command", _("أمر حركة")),
        ("unknown", _("غير معروف")),
    ]

    device = models.ForeignKey(
        RobotDevice, on_delete=models.CASCADE, related_name="voice_turns",
        verbose_name=_("الجهاز"),
    )
    transcript = models.TextField(blank=True, default="", verbose_name=_("النص المسموع"))
    intent = models.CharField(max_length=20, choices=INTENT, default="unknown")
    reply_text = models.TextField(blank=True, default="", verbose_name=_("الرد المنطوق"))
    # Optional link to who asked (mechanic under the car), when face-known.
    employee = models.ForeignKey(
        "hr.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="robot_voice_turns",
    )
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("تفاعل صوتي")
        verbose_name_plural = _("🎙️ التفاعلات الصوتية")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_intent_display()}: {self.transcript[:40]}"


class RobotAccessLog(models.Model):
    """Facial-recognition access-control + attendance event.

    Every face the ESP32-CAM resolves is logged here. When it matches an
    authorized `hr.Employee` above the configured threshold we (a) clock them
    in/out via `hr.AttendanceRecord` and (b) grant the session the right to
    authorize a sale. Unknown faces are logged and denied.
    """

    RESULT = [
        ("granted", _("مصرّح")),
        ("denied", _("مرفوض")),
        ("unknown", _("وجه غير معروف")),
    ]
    ACTION = [
        ("clock_in", _("تسجيل حضور")),
        ("clock_out", _("تسجيل انصراف")),
        ("authorize", _("تفويض عملية")),
        ("scan", _("مسح فقط")),
    ]

    device = models.ForeignKey(
        RobotDevice, on_delete=models.CASCADE, related_name="access_logs",
        verbose_name=_("الجهاز"),
    )
    employee = models.ForeignKey(
        "hr.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="robot_access_logs", verbose_name=_("الموظف"),
    )
    result = models.CharField(max_length=10, choices=RESULT, default="unknown")
    action = models.CharField(max_length=10, choices=ACTION, default="scan")
    match_score = models.FloatField(default=0.0, verbose_name=_("درجة التطابق"))
    image = models.ImageField(upload_to="robot/faces/%Y/%m/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("سجل وصول (وجه)")
        verbose_name_plural = _("👤 سجلات الوصول (تعرّف الوجه)")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["-created_at"])]

    def __str__(self) -> str:
        who = self.employee.name if self.employee else _("مجهول")
        return f"{who} — {self.get_result_display()}"


class ProcurementSignal(models.Model):
    """Trigger-agent output: robot noticed critically-low stock while scanning.

    This is the robot acting as an agent in the multi-agent ecosystem — it does
    NOT place an order, it raises a signal the Procurement Agent consumes to add
    the part to the next overseas shipping-container manifest.
    """

    STATUS = [
        ("open", _("مفتوح")),
        ("acknowledged", _("مستلَم")),
        ("queued", _("مضاف للمنافيست")),
        ("dismissed", _("مُهمَل")),
    ]

    device = models.ForeignKey(
        RobotDevice, on_delete=models.CASCADE, related_name="procurement_signals",
        verbose_name=_("الجهاز"),
    )
    product = models.ForeignKey(
        "inventory.Product", on_delete=models.CASCADE,
        related_name="robot_procurement_signals", verbose_name=_("المنتج"),
    )
    branch = models.ForeignKey(
        "inventory.Branch", on_delete=models.CASCADE, verbose_name=_("الفرع"),
    )
    quantity_on_hand = models.IntegerField(default=0)
    min_stock_level = models.IntegerField(default=0)
    suggested_reorder_qty = models.IntegerField(default=0)
    status = models.CharField(max_length=15, choices=STATUS, default="open", db_index=True)
    # Free-form hand-off payload the Procurement Agent reads.
    manifest_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("إشارة توريد")
        verbose_name_plural = _("📦 إشارات التوريد (Procurement Agent)")
        ordering = ["-created_at"]
        constraints = [
            # At most one open signal per product+branch — avoid manifest spam.
            models.UniqueConstraint(
                fields=["product", "branch"],
                condition=models.Q(status="open"),
                name="robot_one_open_procurement_signal_per_product_branch",
            ),
        ]

    def __str__(self) -> str:
        return f"⚠️ {self.product} @ {self.branch} ({self.quantity_on_hand})"


class MotorCommandLog(models.Model):
    """Physical-articulation command dispatched to the Arduino Mega via ESP32.

    The backend never drives relays directly; it records intent here and the
    ESP32 pulls/receives it and forwards a serial frame to the Mega. `ack` is set
    when the Mega confirms. Kept for safety auditing (e.g. arm lift while a face
    is in frame).
    """

    ACTUATOR = [
        ("head", _("الرأس (لف يمين/شمال)")),
        ("arm_left", _("الذراع اليسرى")),
        ("arm_right", _("الذراع اليمنى")),
        ("track", _("الجنزير (حركة)")),
    ]
    DIRECTION = [
        ("left", "left"), ("right", "right"),
        ("up", "up"), ("down", "down"),
        ("forward", "forward"), ("backward", "backward"),
        ("stop", "stop"),
    ]

    device = models.ForeignKey(
        RobotDevice, on_delete=models.CASCADE, related_name="motor_commands",
        verbose_name=_("الجهاز"),
    )
    actuator = models.CharField(max_length=10, choices=ACTUATOR)
    direction = models.CharField(max_length=10, choices=DIRECTION)
    # Milliseconds the relay stays energized (0 = latch until an explicit stop).
    duration_ms = models.PositiveIntegerField(default=0)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )
    acknowledged = models.BooleanField(default=False, verbose_name=_("تم التنفيذ؟"))
    created_at = models.DateTimeField(auto_now_add=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("أمر حركة")
        verbose_name_plural = _("🦾 أوامر الحركة")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_actuator_display()} → {self.direction}"

    def serial_frame(self) -> str:
        """Compact frame the ESP32 forwards to the Arduino Mega over serial.

        Format: `<ACTUATOR:DIRECTION:DURATION_MS>` — see the Mega firmware
        parser in robot/firmware/arduino_mega_motor_control/.
        """
        return f"<{self.actuator}:{self.direction}:{self.duration_ms}>"
