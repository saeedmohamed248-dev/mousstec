from django.contrib import admin

from .models import (
    BmwEcuSettlement, CodingEntitlementHold, DiagnosticFeeCharge,
    EcuBackupRef, EcuHardwareProfile, EcuPinoutDiagram, EcuSession,
    EcuStateChange, ExecutionAttempt, GiftCredit, GiftCreditUsage,
    WizardSession,
)


@admin.register(GiftCredit)
class GiftCreditAdmin(admin.ModelAdmin):
    list_display = ("granted_at", "tenant_schema", "grant_type",
                    "credits_remaining", "credits_total", "valid_until",
                    "status", "granted_by")
    list_filter = ("grant_type", "status")
    search_fields = ("tenant_schema", "note", "granted_by")
    date_hierarchy = "granted_at"
    actions = ["revoke_selected"]

    @admin.action(description="Revoke selected gift credits")
    def revoke_selected(self, request, queryset):
        n = queryset.update(status="revoked")
        self.message_user(request, f"Revoked {n} gift credit(s).")


@admin.register(GiftCreditUsage)
class GiftCreditUsageAdmin(admin.ModelAdmin):
    list_display = ("used_at", "gift", "vin", "operation_type", "reference")
    list_filter = ("operation_type",)
    search_fields = ("vin", "reference", "gift__tenant_schema")
    date_hierarchy = "used_at"
    readonly_fields = ("gift", "vin", "operation_type", "used_at", "reference")


@admin.register(CodingEntitlementHold)
class CodingEntitlementHoldAdmin(admin.ModelAdmin):
    list_display = ("created_at", "vin", "operation_type", "tenant_schema",
                    "status", "resolved_at")
    list_filter = ("status", "operation_type", "tenant_schema")
    search_fields = ("vin", "hold_ref", "tenant_schema")
    date_hierarchy = "created_at"


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


@admin.register(EcuHardwareProfile)
class EcuHardwareProfileAdmin(admin.ModelAdmin):
    list_display = ("hardware_id", "ecu_name", "board_revision", "boot_pin",
                    "verified")
    list_filter = ("family", "protocol", "verified")
    search_fields = ("hardware_id", "ecu_name", "board_revision")


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
