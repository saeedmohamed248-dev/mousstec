from django.contrib import admin

from .models import EcuBackupRef, EcuSession, EcuStateChange


@admin.register(EcuSession)
class EcuSessionAdmin(admin.ModelAdmin):
    list_display = ("vin", "chassis", "technician", "started_at", "ended_at")
    list_filter = ("chassis", "transport_kind")
    search_fields = ("vin", "technician")
    date_hierarchy = "started_at"


@admin.register(EcuStateChange)
class EcuStateChangeAdmin(admin.ModelAdmin):
    list_display = ("at", "session", "kind", "ecu_name", "success")
    list_filter = ("kind", "success", "ecu_name")
    search_fields = ("session__vin", "backup_sha256")
    date_hierarchy = "at"


@admin.register(EcuBackupRef)
class EcuBackupRefAdmin(admin.ModelAdmin):
    list_display = ("captured_at", "vin", "ecu_name", "memory_region", "size",
                    "uploaded_to_s3")
    search_fields = ("vin", "sha256")
    list_filter = ("ecu_name", "memory_region", "uploaded_to_s3")
