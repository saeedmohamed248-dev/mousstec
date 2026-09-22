"""
create_robot_device — register a robot and print its API token in one step.

Saves clicking through the admin: it creates a RobotDevice for a branch, mints a
secure token, and prints it ONCE so it can be copied into the ESP32 firmware
(ROBOT_TOKEN in esp32_bridge.ino / esp32_cam.ino).

Multi-tenant note: RobotDevice is a per-workshop (tenant) model, so run this
inside the tenant's schema with django_tenants' tenant_command, e.g.:

    python manage.py tenant_command create_robot_device \
        --name "روبوت الفرع الرئيسي" --branch "الفرع الرئيسي" --schema=<tenant_schema>

Without a branch it lists the available branches and exits, so you can see the
exact names/ids first.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils.crypto import get_random_string


class Command(BaseCommand):
    help = "Register a robot device for a branch and print its API token."

    def add_arguments(self, parser):
        parser.add_argument("--name", help="Human-readable robot name.")
        parser.add_argument("--branch", help="Branch name or id the robot belongs to.")
        parser.add_argument("--uid", help="Stable hardware id (e.g. ESP32 MAC). Optional.")

    def handle(self, *args, **opts):
        from inventory.models import Branch
        from robot.models import RobotDevice

        branch_arg = opts.get("branch")
        if not branch_arg:
            self.stdout.write("الفروع المتاحة (استخدم الاسم أو الـ id مع --branch):")
            for b in Branch.objects.all():
                self.stdout.write(f"  [{b.id}] {b.name}")
            raise CommandError("مرّر --branch باسم الفرع أو رقمه.")

        # Resolve the branch by id or name.
        branch = None
        if str(branch_arg).isdigit():
            branch = Branch.objects.filter(pk=int(branch_arg)).first()
        if branch is None:
            branch = Branch.objects.filter(name=branch_arg).first()
        if branch is None:
            raise CommandError(f"لم أجد فرعاً باسم/رقم «{branch_arg}».")

        name = opts.get("name") or f"روبوت {branch.name}"
        uid = opts.get("uid") or f"ROBOT-{get_random_string(10).upper()}"
        token = get_random_string(48)

        device = RobotDevice.objects.create(
            name=name, branch=branch, device_uid=uid, api_token=token,
        )

        self.stdout.write(self.style.SUCCESS("✅ تم تسجيل الروبوت:"))
        self.stdout.write(f"   id        : {device.id}")
        self.stdout.write(f"   name      : {device.name}")
        self.stdout.write(f"   branch    : {branch.name}")
        self.stdout.write(f"   device_uid: {device.device_uid}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("🔑 التوكن (انسخه في الفيرموير ROBOT_TOKEN — مش هيتعرض تاني):"))
        self.stdout.write(f"   {token}")
