"""Admin for the shared Diagnostics Catalog (public schema only)."""
from django.contrib import admin
from django.db import connection

from .models import (
    DTCDefinition,
    VINDecodeCache,
    DTCExternalLookupCache,
    APICostRate,
)


class PublicOnlyMixin:
    """Hide these models from tenant admin (they live in public)."""
    def has_module_permission(self, request):
        return connection.schema_name == 'public' and super().has_module_permission(request)


@admin.register(DTCDefinition)
class DTCDefinitionAdmin(PublicOnlyMixin, admin.ModelAdmin):
    list_display = ('code', 'system', 'severity', 'short_description', 'source', 'is_generic', 'updated_at')
    list_filter = ('system', 'severity', 'source', 'is_generic')
    search_fields = ('code', 'short_description', 'full_description')
    ordering = ('code',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(VINDecodeCache)
class VINDecodeCacheAdmin(PublicOnlyMixin, admin.ModelAdmin):
    list_display = ('vin', 'make', 'model', 'model_year', 'provider', 'fetched_at')
    list_filter = ('provider', 'make', 'model_year')
    search_fields = ('vin', 'make', 'model')
    readonly_fields = ('fetched_at',)


@admin.register(DTCExternalLookupCache)
class DTCExternalLookupCacheAdmin(PublicOnlyMixin, admin.ModelAdmin):
    list_display = ('dtc_code', 'provider', 'vehicle_signature', 'fetched_at')
    list_filter = ('provider',)
    search_fields = ('dtc_code', 'vehicle_signature')
    readonly_fields = ('fetched_at',)


@admin.register(APICostRate)
class APICostRateAdmin(PublicOnlyMixin, admin.ModelAdmin):
    list_display = ('provider', 'endpoint', 'cost_usd', 'is_active', 'note')
    list_filter = ('provider', 'is_active')
    search_fields = ('provider', 'endpoint')
