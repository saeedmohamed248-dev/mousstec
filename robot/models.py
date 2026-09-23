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
    # SHA-256 (hex) of the bearer token the firmware sends in `X-Robot-Token`.
    # The plaintext is shown once when minted (see `issue_token`) and never
    # stored, so a DB leak or a backup doesn't hand out working robot keys.
    api_token = models.CharField(
        max_length=64, unique=True, db_index=True,
        verbose_name=_("توكن الجهاز"),
    )
    firmware_version = models.CharField(max_length=20, blank=True, default="")
    is_active = models.BooleanField(default=True, verbose_name=_("مفعّل؟"))
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name=_("آخر اتصال"))
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # --- Camera / guard ---
    # The camera runs 24/7 and is never fully turned off; this flag only controls
    # whether the backend keeps the latest live frame (privacy toggle), it does
    # NOT stop after-hours motion guarding.
    camera_always_on = models.BooleanField(default=True, verbose_name=_("الكاميرا تعمل ٢٤ ساعة"))
    # After-hours guard window (local time). Motion detected between guard_from
    # and guard_to raises an alert. Null/null = guard whenever the shop is
    # "closed" per HRSettings, else always-armed fallback in services.
    guard_from = models.TimeField(null=True, blank=True, verbose_name=_("بداية الحراسة"))
    guard_to = models.TimeField(null=True, blank=True, verbose_name=_("نهاية الحراسة"))
    # Latest live frame for the dashboard viewer + when it arrived.
    last_frame = models.ImageField(upload_to="robot/live/%Y/%m/%d/", null=True, blank=True)
    last_frame_at = models.DateTimeField(null=True, blank=True)
    last_motion_at = models.DateTimeField(null=True, blank=True, verbose_name=_("آخر حركة"))

    # --- Health telemetry (reported by the ESP32 for a full "at-a-glance" view) ---
    battery_percent = models.IntegerField(null=True, blank=True, verbose_name=_("البطارية %"))
    cpu_temp = models.FloatField(null=True, blank=True, verbose_name=_("حرارة المعالج"))
    free_disk_mb = models.IntegerField(null=True, blank=True, verbose_name=_("مساحة SD المتاحة (م.ب)"))
    wifi_rssi = models.IntegerField(null=True, blank=True, verbose_name=_("قوة الواي فاي (dBm)"))
    telemetry_at = models.DateTimeField(null=True, blank=True)
    # While a supervisor is watching the live view we ask the camera to push
    # frames fast (smooth MJPEG); it drops back to the slow idle rate afterwards.
    # `camera_frame` reads this and returns the target push interval to the cam.
    stream_until = models.DateTimeField(null=True, blank=True)

    # Shelf map: where each shelf is relative to where the robot stands, so it
    # can turn its head toward the part it's talking about. Keys are shelf-
    # location prefixes as written in Inventory.shelf_location, longest wins:
    #   {"A": {"direction": "left", "ms": 600}, "B3": {"direction": "right", "ms": 300}}
    shelf_map = models.JSONField(default=dict, blank=True, verbose_name=_("خريطة الرفوف"))
    # Motor health for predictive maintenance: latest current per actuator
    # (amps) and cumulative run time, updated from /telemetry/.
    motor_health = models.JSONField(default=dict, blank=True, verbose_name=_("صحة المواتير"))

    def desired_push_interval_ms(self, *, fast=200, idle=1500) -> int:
        """Frame-push cadence the camera should use right now."""
        if self.stream_until and self.stream_until > timezone.now():
            return fast
        return idle

    class Meta:
        verbose_name = _("جهاز روبوت")
        verbose_name_plural = _("🤖 أجهزة الروبوت")
        ordering = ["-last_seen_at"]

    def __str__(self) -> str:
        return f"{self.name} @ {self.branch.name}"

    @staticmethod
    def hash_token(token: str) -> str:
        import hashlib
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()

    def issue_token(self) -> str:
        """Mint a new token, store only its hash, return the plaintext ONCE."""
        from django.utils.crypto import get_random_string
        token = get_random_string(48)
        self.api_token = self.hash_token(token)
        return token

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
        ("intake", _("إدخال بضاعة (تصوير + تسجيل)")),
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
    # Intake flow: how many units were added and whether a brand-new Product row
    # was created (vs. incrementing an existing one).
    quantity_added = models.IntegerField(default=0, verbose_name=_("الكمية المضافة"))
    created_new_product = models.BooleanField(default=False, verbose_name=_("منتج جديد؟"))
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
    # Milliseconds the relay stays energized. Every move is timed (see
    # services.validate_motor_command); 0 is only used by `stop`.
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


class RobotKnowledge(models.Model):
    """The robot's learning memory — it gets better at recognizing parts.

    Every time a human confirms "this photo/label is THIS product" (during a
    sale or an intake), we store the mapping keyed by whatever the robot saw: a
    read part-number/code, a spoken/typed label, or a vision fingerprint hash.
    Next time the same code/label/fingerprint shows up, `resolve()` returns the
    confirmed product instantly and with rising confidence (hit_count), so the
    robot improves with use instead of re-asking.
    """

    KEY_KIND = [
        ("code", _("رقم/كود مقروء")),
        ("label", _("اسم/وصف")),
        ("fingerprint", _("بصمة مرئية")),
    ]

    key_kind = models.CharField(max_length=12, choices=KEY_KIND, default="code")
    # Normalized lookup key (lowercased code, normalized label, or fp hash).
    key_value = models.CharField(max_length=255, db_index=True)
    product = models.ForeignKey(
        "inventory.Product", on_delete=models.CASCADE,
        related_name="robot_knowledge", verbose_name=_("المنتج المؤكَّد"),
    )
    hit_count = models.PositiveIntegerField(default=1, verbose_name=_("مرات التأكيد"))
    # Full vision fingerprint / notes kept for auditing and future re-training.
    details = models.JSONField(default=dict, blank=True)
    confirmed_by = models.ForeignKey(
        "hr.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="robot_confirmations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("معرفة الروبوت")
        verbose_name_plural = _("🧠 معرفة الروبوت (تعلّم)")
        ordering = ["-hit_count", "-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["key_kind", "key_value", "product"],
                name="robot_knowledge_unique_key_product",
            ),
        ]
        indexes = [models.Index(fields=["key_kind", "key_value"])]

    def __str__(self) -> str:
        return f"{self.get_key_kind_display()} '{self.key_value}' → {self.product} (×{self.hit_count})"


class RobotCustomerFace(models.Model):
    """The robot's memory of a customer — so it greets returning faces by name.

    Binds a walk-in customer's face embedding to their `inventory.Customer`
    record and tracks how often they visit. Recognition also works by name,
    phone, or invoice number (handled in services) — this row is specifically
    the FACE channel plus the visit counters, and it never stores any price or
    cost, only the customer link.
    """

    customer = models.OneToOneField(
        "inventory.Customer", on_delete=models.CASCADE,
        related_name="robot_face", verbose_name=_("العميل"),
    )
    # Face embedding (same extractor as staff faces — see robot/faces.py).
    face_encoding = models.JSONField(null=True, blank=True, verbose_name=_("بصمة الوجه"))
    visit_count = models.PositiveIntegerField(default=0, verbose_name=_("عدد الزيارات"))
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name=_("آخر زيارة"))
    # Free preferences/notes the robot picks up ("يفضّل قطع أصلية", …).
    notes = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("ذاكرة عميل")
        verbose_name_plural = _("🙋 ذاكرة العملاء (تعرّف الوجه)")
        ordering = ["-last_seen_at"]

    def __str__(self) -> str:
        return f"{self.customer.name} (×{self.visit_count})"


class RobotStockTakeSession(models.Model):
    """A physical inventory-count (جرد) the robot runs on command.

    Started by a spoken/typed instruction ("اجرد الكنترول والفلاتر"). Each counted
    item is a line reconciled against `inventory.Inventory`; the session tracks
    matched/mismatched counts so a supervisor can approve adjustments.
    """

    STATUS = [
        ("open", _("جاري")),
        ("completed", _("مكتمل")),
        ("applied", _("تم ترحيل التسويات")),
        ("cancelled", _("ملغي")),
    ]

    device = models.ForeignKey(
        RobotDevice, on_delete=models.CASCADE, related_name="stock_takes",
        verbose_name=_("الجهاز"),
    )
    branch = models.ForeignKey(
        "inventory.Branch", on_delete=models.CASCADE, verbose_name=_("الفرع"),
    )
    instruction = models.CharField(max_length=255, blank=True, default="",
                                   verbose_name=_("الأمر المنطوق"))
    status = models.CharField(max_length=12, choices=STATUS, default="open", db_index=True)
    started_by = models.ForeignKey(
        "hr.Employee", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="robot_stock_takes",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("جلسة جرد")
        verbose_name_plural = _("📋 جلسات الجرد")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"جرد #{self.id} @ {self.branch} ({self.get_status_display()})"


class RobotStockTakeLine(models.Model):
    """One counted item within a stock-take session (expected vs. counted)."""

    session = models.ForeignKey(
        RobotStockTakeSession, on_delete=models.CASCADE, related_name="lines",
    )
    product = models.ForeignKey(
        "inventory.Product", on_delete=models.CASCADE,
        related_name="robot_stock_take_lines",
    )
    expected_qty = models.IntegerField(default=0, verbose_name=_("الكمية بالنظام"))
    counted_qty = models.IntegerField(default=0, verbose_name=_("الكمية المعدودة"))

    class Meta:
        verbose_name = _("سطر جرد")
        verbose_name_plural = _("أسطر الجرد")
        ordering = ["id"]

    @property
    def variance(self) -> int:
        return self.counted_qty - self.expected_qty

    @property
    def matched(self) -> bool:
        return self.variance == 0

    def __str__(self) -> str:
        return f"{self.product} — نظام {self.expected_qty} / معدود {self.counted_qty}"


class RobotCommand(models.Model):
    """A command the dashboard/backend queues for the robot to execute.

    The device polls `/commands/pending/`, runs each, and acks it. Covers remote
    control from the dashboard (take a snapshot, turn the head, move, speak, start
    the live stream) and owner paging (announce for an employee). This is the
    generic control channel; MotorCommandLog stays the low-level relay log.
    """

    KIND = [
        ("snapshot", _("التقط صورة")),
        ("look_at", _("لف الرأس تجاه")),
        ("move", _("تحرّك")),
        ("say", _("انطق نص")),
        ("page", _("نادِ على موظف")),
        ("stream_start", _("ابدأ البث")),
        ("stream_stop", _("أوقف البث")),
        ("scan", _("امسح القطعة قدام الكاميرا")),
    ]
    # Kinds the ESP32-CAM executes (delivered in its /camera/frame/ reply);
    # everything else goes to the bridge ESP32 via /commands/pending/.
    CAMERA_KINDS = ("snapshot", "scan")
    STATUS = [
        ("pending", _("بانتظار")),
        ("sent", _("أُرسل للجهاز")),
        ("done", _("تم")),
        ("failed", _("فشل")),
    ]

    device = models.ForeignKey(
        RobotDevice, on_delete=models.CASCADE, related_name="commands",
        verbose_name=_("الجهاز"),
    )
    kind = models.CharField(max_length=16, choices=KIND)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default="pending", db_index=True)
    result = models.JSONField(default=dict, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("أمر روبوت")
        verbose_name_plural = _("🎛️ أوامر الروبوت")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["device", "status"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} → {self.device.name} ({self.status})"


class RobotSnapshot(models.Model):
    """A still image the robot captured — manually, on motion, or with a page."""

    REASON = [
        ("manual", _("يدوي (من اللوحة)")),
        ("motion", _("حركة")),
        ("after_hours", _("حركة بعد الغلق")),
        ("page", _("مع نداء")),
        ("scan", _("مسح")),
        ("enroll", _("تسجيل بصمة وجه")),
    ]

    device = models.ForeignKey(
        RobotDevice, on_delete=models.CASCADE, related_name="snapshots",
        verbose_name=_("الجهاز"),
    )
    image = models.ImageField(upload_to="robot/snapshots/%Y/%m/%d/")
    reason = models.CharField(max_length=12, choices=REASON, default="manual")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("لقطة")
        verbose_name_plural = _("📸 اللقطات")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_reason_display()} @ {self.created_at:%Y-%m-%d %H:%M}"


class RobotAlert(models.Model):
    """A notification the owner should see — chiefly after-hours motion."""

    KIND = [
        ("after_hours_motion", _("حركة بعد غلق المحل")),
        ("offline", _("الروبوت فقد الاتصال")),
        ("back_online", _("الروبوت رجع أونلاين")),
        ("low_battery", _("بطارية منخفضة")),
        ("motor_fault", _("مشكلة في موتور")),
        ("other", _("أخرى")),
    ]

    device = models.ForeignKey(
        RobotDevice, on_delete=models.CASCADE, related_name="alerts",
        verbose_name=_("الجهاز"),
    )
    kind = models.CharField(max_length=20, choices=KIND, default="other", db_index=True)
    message = models.CharField(max_length=255, blank=True, default="")
    snapshot = models.ForeignKey(
        RobotSnapshot, on_delete=models.SET_NULL, null=True, blank=True,
    )
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("تنبيه روبوت")
        verbose_name_plural = _("🚨 تنبيهات الروبوت")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["is_read", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} — {self.created_at:%Y-%m-%d %H:%M}"


class RobotPageCall(models.Model):
    """Owner pages an employee: the robot announces / calls them to the office.

    Only the owner/admin may create one (enforced in the view). The robot picks
    it up via a queued RobotCommand(kind='page') and announces it by voice, and
    may turn/roll toward the last place it saw them.
    """

    STATUS = [
        ("pending", _("بانتظار النداء")),
        ("announced", _("تم النداء")),
        ("acknowledged", _("ردّ الموظف")),
        ("cancelled", _("أُلغي")),
    ]

    device = models.ForeignKey(
        RobotDevice, on_delete=models.CASCADE, related_name="page_calls",
        verbose_name=_("الجهاز"),
    )
    target_employee = models.ForeignKey(
        "hr.Employee", on_delete=models.CASCADE, related_name="robot_pages",
        verbose_name=_("الموظف المطلوب"),
    )
    message = models.CharField(max_length=255, blank=True, default="",
                               verbose_name=_("رسالة النداء"))
    status = models.CharField(max_length=12, choices=STATUS, default="pending", db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name=_("طلب النداء"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    announced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("نداء موظف")
        verbose_name_plural = _("📢 نداءات الموظفين")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"نداء {self.target_employee} ({self.get_status_display()})"


class RobotSyncEvent(models.Model):
    """Idempotency ledger for offline events replayed when the net returns.

    While offline the robot queues everything it did (scans, counts, learned
    facts) with a client-generated `client_uid`; on reconnect it POSTs them to
    `/sync/push/`. This row makes replay idempotent — a repeated `client_uid` is
    ignored so nothing is applied twice.
    """

    device = models.ForeignKey(
        RobotDevice, on_delete=models.CASCADE, related_name="sync_events",
    )
    client_uid = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=32)
    payload = models.JSONField(default=dict, blank=True)
    applied = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("حدث مزامنة")
        verbose_name_plural = _("🔄 أحداث المزامنة (أوفلاين)")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["device", "client_uid"],
                name="robot_sync_unique_client_uid_per_device",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} · {self.client_uid} ({'applied' if self.applied else 'queued'})"


class RobotFaceEnrollment(models.Model):
    """Staff face-enrollment round: the robot calls employees one by one.

    The owner starts it from the dashboard right after installing the robot
    (or later for new hires). The robot announces each employee by name, the
    camera captures several good samples of that face, the averaged embedding
    is stored on `hr.Employee.face_encoding`, and it moves on to the next.
    Anyone absent is skipped and can be enrolled in a later round.

    `entries` is the ordered queue:
      [{"employee_id", "name", "status": pending|current|done|skipped,
        "samples": [[...], ...], "announced_at", "announce_count", "note"}]
    """

    STATUS = [
        ("active", _("جاري")),
        ("done", _("انتهى")),
        ("cancelled", _("أُلغي")),
    ]

    device = models.ForeignKey(
        RobotDevice, on_delete=models.CASCADE, related_name="face_enrollments",
        verbose_name=_("الجهاز"),
    )
    status = models.CharField(max_length=10, choices=STATUS, default="active", db_index=True)
    entries = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("جلسة تسجيل بصمات الوجه")
        verbose_name_plural = _("🧑‍💼 جلسات تسجيل بصمات الوجه")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        done = sum(1 for e in self.entries if e.get("status") == "done")
        return f"تسجيل وجوه #{self.pk} ({done}/{len(self.entries)})"

    def current(self):
        """(index, entry) of the employee being enrolled now, or (None, None)."""
        for i, e in enumerate(self.entries):
            if e.get("status") == "current":
                return i, e
        return None, None
