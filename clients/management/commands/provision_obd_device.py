"""
🔐 Provision a new OBD device for a tenant.

Prints the plaintext device secret EXACTLY ONCE. The operator must copy it
into the device firmware / mobile app right away — we only store ciphertext
and the last-4 hint, so a lost secret means a forced rotation.

Usage:
    python manage.py provision_obd_device --tenant <schema_name> \\
        [--branch <branch_id>] [--label "Cairo Bay 2"] [--device-id obd_xyz]

    # Rotate the secret on an existing device:
    python manage.py provision_obd_device --rotate <device_id>

    # Revoke / suspend / reactivate:
    python manage.py provision_obd_device --set-status <device_id> \\
        --status revoked
"""
from django.core.management.base import BaseCommand, CommandError

from clients.models import Client
from clients.obd_device_models import OBDDevice


class Command(BaseCommand):
    help = "Provision, rotate, or change the status of an OBD scanner device."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Client.schema_name to bind this device to")
        parser.add_argument("--branch", type=int, default=None,
                            help="Branch PK within the tenant schema (optional)")
        parser.add_argument("--label", default="", help="Human label for ops UI")
        parser.add_argument("--device-id", dest="device_id", default=None,
                            help="Custom device_id (default: auto-generated)")

        parser.add_argument("--rotate", dest="rotate_id", default=None,
                            help="device_id of an existing device to rotate")

        parser.add_argument("--set-status", dest="status_id", default=None,
                            help="device_id whose status should change")
        parser.add_argument("--status", choices=[c[0] for c in OBDDevice.STATUS_CHOICES],
                            default=None, help="New status value")

    def handle(self, *args, **opts):
        if opts["rotate_id"]:
            self._rotate(opts["rotate_id"])
            return
        if opts["status_id"]:
            if not opts["status"]:
                raise CommandError("--set-status requires --status")
            self._set_status(opts["status_id"], opts["status"])
            return

        if not opts["tenant"]:
            raise CommandError(
                "Provide --tenant <schema_name> (or use --rotate / --set-status)"
            )
        self._provision(
            tenant_schema=opts["tenant"],
            branch_id=opts["branch"],
            label=opts["label"],
            device_id=opts["device_id"],
        )

    # ── actions ────────────────────────────────────────────────────────
    def _provision(self, *, tenant_schema, branch_id, label, device_id):
        tenant = Client.objects.filter(schema_name=tenant_schema).first()
        if tenant is None:
            raise CommandError(f"Tenant with schema_name='{tenant_schema}' not found")

        device, plaintext = OBDDevice.provision(
            tenant=tenant, branch_id=branch_id, label=label, device_id=device_id,
        )
        self._print_secret_banner(device, plaintext, action="PROVISIONED")

    def _rotate(self, device_id):
        device = OBDDevice.objects.filter(device_id=device_id).first()
        if device is None:
            raise CommandError(f"Device '{device_id}' not found")
        plaintext = device.rotate_secret()
        self._print_secret_banner(device, plaintext, action="ROTATED")

    def _set_status(self, device_id, status):
        device = OBDDevice.objects.filter(device_id=device_id).first()
        if device is None:
            raise CommandError(f"Device '{device_id}' not found")
        prev = device.status
        device.status = status
        device.save(update_fields=["status"])
        self.stdout.write(self.style.SUCCESS(
            f"✓ Device '{device_id}': {prev} → {status}"
        ))

    # ── output ─────────────────────────────────────────────────────────
    def _print_secret_banner(self, device, plaintext, *, action):
        bar = "═" * 74
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(bar))
        self.stdout.write(self.style.WARNING(
            f"  🔐 OBD DEVICE {action} — COPY THE SECRET NOW (shown only once)"
        ))
        self.stdout.write(self.style.WARNING(bar))
        self.stdout.write(f"  device_id   : {device.device_id}")
        self.stdout.write(f"  tenant      : {device.tenant.schema_name}")
        self.stdout.write(f"  branch_id   : {device.branch_id}")
        self.stdout.write(f"  label       : {device.label or '(none)'}")
        self.stdout.write(f"  generation  : {device.rotation_generation}")
        self.stdout.write(f"  last4 hint  : ...{device.secret_last4}")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"  SECRET: {plaintext}"))
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(bar))
        self.stdout.write(self.style.WARNING(
            "  ⚠  Store this in the device firmware now. We persist only "
            "ciphertext + last4."
        ))
        self.stdout.write(self.style.WARNING(bar))
