"""Store device tokens as SHA-256 hashes instead of plaintext.

Existing robots keep working: their firmware still sends the same plaintext
token, and `security.authenticate_device` now hashes what it receives before
looking it up. Irreversible by design — the plaintext can't be recovered, so
the reverse step is a no-op (rotate a token from the dashboard if needed).
"""

import hashlib

from django.db import migrations


def _is_hash(value):
    return len(value or "") == 64 and all(c in "0123456789abcdef" for c in value)


def hash_tokens(apps, schema_editor):
    RobotDevice = apps.get_model("robot", "RobotDevice")
    for device in RobotDevice.objects.all():
        if device.api_token and not _is_hash(device.api_token):
            device.api_token = hashlib.sha256(device.api_token.encode("utf-8")).hexdigest()
            device.save(update_fields=["api_token"])


class Migration(migrations.Migration):

    dependencies = [
        ("robot", "0006_face_enrollment_shelf_map_motor_health"),
    ]

    operations = [
        migrations.RunPython(hash_tokens, migrations.RunPython.noop),
    ]
