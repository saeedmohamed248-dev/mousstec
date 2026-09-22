from django.apps import AppConfig


class RobotConfig(AppConfig):
    """🤖 Mouss Tec physical edge-agent (Arduino Mega + ESP32 + ESP32-CAM).

    Tenant-scoped app: every device, scan, invoice trigger and access log
    belongs to a single workshop schema, exactly like `inventory` and `hr`.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "robot"
    verbose_name = "🤖 روبوت الورشة (Mouss Tec Robot)"
