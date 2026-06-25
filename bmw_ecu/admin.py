from django.contrib import admin

from .models import (
    BmwEcuSettlement, DiagnosticFeeCharge, EcuBackupRef, EcuPinoutDiagram,
    EcuSession, EcuStateChange, ExecutionAttempt, WizardSession,
)


@admin.register(BmwEcuSettlement)
class BmwEcuSettlementAdmin(admin.ModelAdmin):
    list_display = ("created_at", "charge", "mode", "succeeded", "amount",
                    "wallet_after")
    list_filter = ("mode", "succeeded", "currency")
    search_fields = ("charge__vin", "charge__authorization_ref")
    date_hierarchy = "created_at"
    readonly_fields = ("charge", "mode", "succeeded", "amount", "currency",
                       "wallet_before", "wallet_after", "paymob_iframe_url",
                       "error_message", "created_at")


@admin.register(DiagnosticFeeCharge)
class DiagnosticFeeChargeAdmin(admin.ModelAdmin):
    list_display = ("authorized_at", "vin", "amount", "currency", "status",
                    "finalised_at")
    list_filter = ("status", "currency")
    search_fields = ("vin", "authorization_ref")
    date_hierarchy = "authorized_at"


@admin.register(ExecutionAttempt)
class ExecutionAttemptAdmin(admin.ModelAdmin):
    list_display = ("started_at", "profile_name", "strategy", "outcome", "exploit_used")
    list_filter = ("strategy", "outcome", "profile_name")
    search_fields = ("session__vin", "error_code", "exploit_used")
    date_hierarchy = "started_at"


@admin.register(WizardSession)
class WizardSessionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "vin", "ecu_name", "state", "technician_id")
    list_filter = ("state", "ecu_name")
    search_fields = ("vin", "technician_id")
    date_hierarchy = "created_at"


@admin.register(EcuPinoutDiagram)
class EcuPinoutDiagramAdmin(admin.ModelAdmin):
    list_display = ("ecu_name", "image_url")
    search_fields = ("ecu_name",)


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
