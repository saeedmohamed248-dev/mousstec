"""robot/admin.py — dashboards for devices, scans, access and procurement signals."""

from django.contrib import admin

from .models import (
    MotorCommandLog, ProcurementSignal, RobotAccessLog, RobotDevice,
    RobotScanEvent, RobotVoiceInteraction,
)


@admin.register(RobotDevice)
class RobotDeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "branch", "is_online", "firmware_version", "last_seen_at", "is_active")
    list_filter = ("branch", "is_active")
    search_fields = ("name", "device_uid")
    readonly_fields = ("last_seen_at", "last_ip", "created_at")


@admin.register(RobotScanEvent)
class RobotScanEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "device", "purpose", "recognized_label",
                    "confidence", "product", "suggested_price", "sale_invoice")
    list_filter = ("purpose", "device")
    search_fields = ("recognized_label", "recognized_part_number")
    readonly_fields = ("created_at",)


@admin.register(RobotVoiceInteraction)
class RobotVoiceInteractionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "device", "intent", "transcript", "employee")
    list_filter = ("intent", "device")
    search_fields = ("transcript", "reply_text")
    readonly_fields = ("created_at",)


@admin.register(RobotAccessLog)
class RobotAccessLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "device", "employee", "result", "action", "match_score")
    list_filter = ("result", "action", "device")
    readonly_fields = ("created_at",)


@admin.register(ProcurementSignal)
class ProcurementSignalAdmin(admin.ModelAdmin):
    list_display = ("created_at", "product", "branch", "quantity_on_hand",
                    "min_stock_level", "suggested_reorder_qty", "status")
    list_filter = ("status", "branch")
    search_fields = ("product__name", "product__part_number")
    readonly_fields = ("created_at", "resolved_at")


@admin.register(MotorCommandLog)
class MotorCommandLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "device", "actuator", "direction",
                    "duration_ms", "acknowledged")
    list_filter = ("actuator", "acknowledged", "device")
    readonly_fields = ("created_at", "acknowledged_at")
