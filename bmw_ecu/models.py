"""Cloud-sync models: persist every ECU session + state change to Mousstec DB.

Why: a tech's laptop dies mid-coding → next day the senior tech opens the
case file, sees the last successful state, and knows exactly where to resume.

These are tenant-scoped (default tenant context — `django_tenants` routes
the schema). Models are intentionally minimal — heavy detail lives in the
on-disk backup files referenced by `backup_sha256`.
"""
from __future__ import annotations

from django.db import models


class EcuSession(models.Model):
    """One technician's session against one car. Owns many state changes."""

    vin = models.CharField(max_length=17, db_index=True)
    chassis = models.CharField(max_length=8, blank=True, help_text="F30, G20, ...")
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    technician = models.CharField(max_length=64, blank=True)
    transport_kind = models.CharField(max_length=16, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "ECU Session"

    def __str__(self) -> str:
        return f"{self.vin} @ {self.started_at:%Y-%m-%d %H:%M}"


class EcuStateChange(models.Model):
    """An atomic, append-only event in an ECU session."""

    KIND_CHOICES = [
        ("connect", "Connect"),
        ("session", "Diagnostic session"),
        ("security", "Security access"),
        ("read_isn", "ISN read"),
        ("write_isn", "ISN write"),
        ("ews_sync", "EWS sync"),
        ("code", "Coding"),
        ("flash_start", "Flash start"),
        ("flash_done", "Flash done"),
        ("rollback", "Rollback"),
        ("error", "Error"),
    ]

    session = models.ForeignKey(EcuSession, on_delete=models.CASCADE,
                                related_name="state_changes")
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    kind = models.CharField(max_length=24, choices=KIND_CHOICES, db_index=True)
    ecu_name = models.CharField(max_length=32, blank=True)
    success = models.BooleanField(default=True)
    backup_sha256 = models.CharField(max_length=64, blank=True, db_index=True,
                                     help_text="Content-addressed reference to the on-disk dump")
    payload_summary = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["at"]
        verbose_name = "ECU State Change"

    def __str__(self) -> str:
        return f"{self.session.vin} · {self.kind} · {self.ecu_name}"


class ExecutionAttempt(models.Model):
    """Audit row for every Manager-orchestrated run (whichever strategy won)."""

    STRATEGY_CHOICES = [
        ("software_only", "Software Only"),
        ("hardware_automation", "Hardware Automation"),
        ("interactive_guided", "Interactive Guided"),
    ]
    OUTCOME_CHOICES = [
        ("success", "Success"),
        ("partial", "Partial"),
        ("suspended", "Suspended"),
        ("failed_rolled_back", "Failed (rolled back)"),
        ("failed_unrecoverable", "Failed (unrecoverable)"),
    ]

    session = models.ForeignKey(EcuSession, on_delete=models.CASCADE,
                                related_name="execution_attempts")
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    profile_name = models.CharField(max_length=32, db_index=True)
    strategy = models.CharField(max_length=24, choices=STRATEGY_CHOICES, db_index=True)
    outcome = models.CharField(max_length=24, choices=OUTCOME_CHOICES, db_index=True)
    exploit_used = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    diagnostics = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Execution Attempt"


class WizardSession(models.Model):
    """Persisted state of an InteractiveGuidedStrategy run between requests."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    state = models.CharField(max_length=32, db_index=True)
    vin = models.CharField(max_length=17, db_index=True)
    ecu_name = models.CharField(max_length=32)
    captured_isn_hex = models.CharField(max_length=128, blank=True)
    technician_id = models.CharField(max_length=64, blank=True)
    notes = models.TextField(blank=True)
    error_code = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Wizard Session"


class EcuPinoutDiagram(models.Model):
    """One pinout image + callouts per ECU. Editable from Django admin."""

    ecu_name = models.CharField(max_length=32, unique=True)
    image_url = models.CharField(max_length=512)
    callouts = models.JSONField(default=list, blank=True,
                                help_text='[{"pin": 18, "label": "BOOT", "color": "red"}]')
    description = models.TextField(blank=True)

    class Meta:
        verbose_name = "ECU Pinout Diagram"

    def __str__(self) -> str:
        return self.ecu_name


class DiagnosticFeeCharge(models.Model):
    """Pay-Per-Success ledger: 450 EGP per unlocked VIN.

    Lifecycle:
        authorized → captured  (charge finalised; SUCCESS)
        authorized → released  (charge cancelled; rolled back / failed)
        authorized → expired   (stale; reaper task should release)

    Idempotency: only one row per (vin, status='authorized') at any time.
    Re-calling authorize() returns the existing open row.
    """

    STATUS_CHOICES = [
        ("authorized", "Authorized"),
        ("captured", "Captured"),
        ("released", "Released"),
        ("expired", "Expired"),
        ("declined", "Declined"),
    ]

    vin = models.CharField(max_length=17, db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="EGP")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES,
                              default="authorized", db_index=True)
    authorization_ref = models.CharField(max_length=64, unique=True,
                                         help_text="Idempotency key")
    session = models.ForeignKey("EcuSession", on_delete=models.SET_NULL,
                                null=True, blank=True,
                                related_name="fee_charges")
    authorized_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finalised_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-authorized_at"]
        verbose_name = "Diagnostic Fee Charge"
        constraints = [
            models.UniqueConstraint(
                fields=["vin"], condition=models.Q(status="authorized"),
                name="bmw_ecu_one_open_auth_per_vin",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vin} · {self.amount} {self.currency} · {self.status}"


class BmwEcuSettlement(models.Model):
    """Audit row for every settlement attempt against a captured fee.

    Created by LocalBillingGate.on_captured. Decouples the diagnostic
    capture (which is final once it lands) from the money movement
    (which can succeed, fail, or end up as a Paymob iframe waiting on
    the workshop to pay).
    """

    MODE_CHOICES = [
        ("wallet", "Wallet Deduct"),
        ("paymob", "Paymob Iframe"),
        ("failed", "Settlement Failed"),
    ]

    charge = models.OneToOneField(
        "DiagnosticFeeCharge", on_delete=models.CASCADE,
        related_name="settlement",
    )
    mode = models.CharField(max_length=16, choices=MODE_CHOICES, db_index=True)
    succeeded = models.BooleanField(default=False, db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="EGP")
    wallet_before = models.DecimalField(max_digits=15, decimal_places=2,
                                        null=True, blank=True)
    wallet_after = models.DecimalField(max_digits=15, decimal_places=2,
                                       null=True, blank=True)
    paymob_iframe_url = models.URLField(max_length=1024, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "BMW ECU Settlement"

    def __str__(self) -> str:
        return f"{self.charge.vin} · {self.mode} · {self.amount} {self.currency}"


class EcuBackupRef(models.Model):
    """Index of on-disk backups so the cloud knows what's safely stored."""

    vin = models.CharField(max_length=17, db_index=True)
    ecu_name = models.CharField(max_length=32)
    memory_region = models.CharField(max_length=16)
    sha256 = models.CharField(max_length=64, unique=True)
    size = models.PositiveIntegerField()
    path = models.CharField(max_length=512)
    captured_at = models.DateTimeField()
    uploaded_to_s3 = models.BooleanField(default=False)

    class Meta:
        ordering = ["-captured_at"]
        verbose_name = "ECU Backup Reference"

    def __str__(self) -> str:
        return f"{self.vin}/{self.ecu_name}/{self.sha256[:12]}"
