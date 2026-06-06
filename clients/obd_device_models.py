"""
OBD Device Identity & Secrets — public-schema models.

Lives in `clients` (public schema) so we can authenticate a device BEFORE
switching to its tenant schema. The Vehicle / SaleInvoice / Report tables
live in tenant schemas and are only touched after auth succeeds.

Secrets at rest:
    The plaintext device secret is shown to the operator exactly once at
    provision / rotation time. We persist only Fernet ciphertext using a
    server-side KEK loaded from settings.OBD_DEVICE_SECRET_KEK.

Replay protection:
    `OBDDeviceNonce` is a short-lived (default 10min) record of
    (device, nonce) pairs already accepted. Reuse → 401. A periodic purge
    keeps the table small.
"""
from __future__ import annotations

import hmac
import hashlib
import secrets
import base64
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from cryptography.fernet import Fernet, InvalidToken

from clients.models import Client


def _fernet() -> Fernet:
    kek = getattr(settings, "OBD_DEVICE_SECRET_KEK", None)
    if not kek:
        raise RuntimeError(
            "OBD_DEVICE_SECRET_KEK not configured. Generate with "
            "`Fernet.generate_key()` and set in the environment."
        )
    return Fernet(kek.encode() if isinstance(kek, str) else kek)


class OBDDevice(models.Model):
    """One physical OBD scanner. Maps an opaque device_id → tenant + branch."""

    STATUS_ACTIVE = "active"
    STATUS_SUSPENDED = "suspended"
    STATUS_REVOKED = "revoked"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_SUSPENDED, "Suspended"),
        (STATUS_REVOKED, "Revoked"),
    ]

    device_id = models.CharField(max_length=64, unique=True, db_index=True)
    label = models.CharField(max_length=120, blank=True,
                             help_text="Human label e.g. 'Branch-Cairo Bay 2'")

    tenant = models.ForeignKey(Client, on_delete=models.CASCADE,
                               related_name="obd_devices")
    # Branch lives in the tenant schema — cross-schema FK is impossible,
    # so we store the PK as a soft reference.
    branch_id = models.PositiveIntegerField(null=True, blank=True,
                                            help_text="Branch PK within tenant schema")

    secret_ciphertext = models.BinaryField()
    secret_last4 = models.CharField(max_length=4, help_text="Ops-UI hint only")
    rotation_generation = models.PositiveSmallIntegerField(default=1)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES,
                              default=STATUS_ACTIVE)
    replay_window_seconds = models.PositiveSmallIntegerField(default=300)

    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_ip = models.GenericIPAddressField(null=True, blank=True)
    last_seen_generation = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "status"]),
        ]

    def __str__(self) -> str:
        return f"OBDDevice<{self.device_id} → {self.tenant.schema_name}>"

    # ── secret lifecycle ──────────────────────────────────────────────
    @classmethod
    def provision(cls, *, tenant: Client, branch_id: int | None = None,
                  label: str = "", device_id: str | None = None) -> tuple["OBDDevice", str]:
        """Create a new device. Returns (instance, plaintext_secret).
        The plaintext is shown to the operator once and never persisted."""
        plaintext = secrets.token_urlsafe(32)
        device = cls(
            device_id=device_id or f"obd_{secrets.token_hex(8)}",
            label=label,
            tenant=tenant,
            branch_id=branch_id,
            secret_ciphertext=_fernet().encrypt(plaintext.encode()),
            secret_last4=plaintext[-4:],
        )
        device.save()
        return device, plaintext

    def rotate_secret(self) -> str:
        """Bump generation, replace ciphertext. Returns new plaintext once."""
        plaintext = secrets.token_urlsafe(32)
        self.secret_ciphertext = _fernet().encrypt(plaintext.encode())
        self.secret_last4 = plaintext[-4:]
        self.rotation_generation += 1
        self.save(update_fields=[
            "secret_ciphertext", "secret_last4", "rotation_generation",
        ])
        return plaintext

    def _decrypt_secret(self) -> bytes:
        try:
            return _fernet().decrypt(bytes(self.secret_ciphertext))
        except InvalidToken as exc:
            raise RuntimeError(
                f"OBDDevice {self.device_id}: secret decryption failed. "
                f"KEK mismatch?"
            ) from exc

    # ── verification ──────────────────────────────────────────────────
    def verify_signature(self, *, body: bytes, timestamp: str,
                         nonce: str, signature_hex: str) -> bool:
        """Constant-time HMAC compare. Caller is responsible for
        timestamp-window and nonce-replay checks (those belong in the
        view so we can reject before doing the decrypt)."""
        if self.status != self.STATUS_ACTIVE:
            return False
        body_digest = hashlib.sha256(body).hexdigest()
        message = f"{timestamp}.{nonce}.{body_digest}".encode()
        expected = hmac.new(self._decrypt_secret(), message,
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_hex)


class OBDDeviceNonce(models.Model):
    """Short-lived record of (device, nonce) pairs already accepted.
    Pruned by a periodic task; rows older than ~replay_window can go."""

    device = models.ForeignKey(OBDDevice, on_delete=models.CASCADE,
                               related_name="nonces")
    nonce = models.CharField(max_length=64)
    seen_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["device", "nonce"],
                                    name="uniq_obd_device_nonce"),
        ]
        indexes = [
            models.Index(fields=["seen_at"]),
        ]

    @classmethod
    def purge_older_than(cls, *, seconds: int = 900) -> int:
        cutoff = timezone.now() - timedelta(seconds=seconds)
        deleted, _ = cls.objects.filter(seen_at__lt=cutoff).delete()
        return deleted
