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
