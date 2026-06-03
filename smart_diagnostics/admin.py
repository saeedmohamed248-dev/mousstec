"""Admin for tenant-scoped diagnostics models."""
from django.contrib import admin, messages
from django.db.models import Sum, Count, Q
from django.utils.translation import gettext_lazy as _

from .models import (
    DiagnosticDevice,
    DiagnosticScan,
    FaultLog,
    LiveTelemetryFrame,
    TestPlanExecution,
    TestStepResult,
    APICallLog,
)


@admin.register(DiagnosticDevice)
class DiagnosticDeviceAdmin(admin.ModelAdmin):
    list_display = ('vehicle', 'masked_token', 'hardware_id', 'is_active', 'last_seen_at')
    list_filter = ('is_active',)
    search_fields = ('device_token', 'hardware_id', 'vehicle__chassis_number', 'vehicle__car_plate')
    readonly_fields = ('created_at', 'device_token')
    raw_id_fields = ('vehicle',)
    actions = ['rotate_token', 'deactivate_devices', 'activate_devices']

    def masked_token(self, obj):
        if not obj.device_token:
            return '—'
        return f"{obj.device_token[:6]}…{obj.device_token[-4:]}"
    masked_token.short_description = _("التوكن")

    @admin.action(description=_("🔁 تدوير التوكن (rotate)"))
    def rotate_token(self, request, queryset):
        import secrets
        n = 0
        for d in queryset:
            d.device_token = secrets.token_urlsafe(32)
            d.save(update_fields=['device_token'])
            n += 1
        self.message_user(request, f"تم تدوير التوكن لـ {n} جهاز.", messages.SUCCESS)

    @admin.action(description=_("🛑 إيقاف الأجهزة المحددة"))
    def deactivate_devices(self, request, queryset):
        n = queryset.update(is_active=False)
        self.message_user(request, f"تم إيقاف {n} جهاز.", messages.SUCCESS)

    @admin.action(description=_("✅ تفعيل الأجهزة المحددة"))
    def activate_devices(self, request, queryset):
        n = queryset.update(is_active=True)
        self.message_user(request, f"تم تفعيل {n} جهاز.", messages.SUCCESS)

    def save_model(self, request, obj, form, change):
        # auto-generate token on create
        if not obj.device_token:
            import secrets
            obj.device_token = secrets.token_urlsafe(32)
        super().save_model(request, obj, form, change)


@admin.register(DiagnosticScan)
class DiagnosticScanAdmin(admin.ModelAdmin):
    list_display = ('id', 'vehicle', 'source', 'status', 'technician', 'started_at', 'finished_at')
    list_filter = ('status', 'source', 'started_at')
    search_fields = ('vehicle__chassis_number', 'vehicle__car_plate', 'summary')
    date_hierarchy = 'started_at'
    raw_id_fields = ('vehicle', 'device', 'technician')


@admin.register(FaultLog)
class FaultLogAdmin(admin.ModelAdmin):
    list_display = ('dtc_code', 'vehicle', 'severity', 'detected_at', 'resolved_at', 'resolved_by')
    list_filter = ('severity', 'detected_at', 'resolved_at')
    search_fields = ('dtc_code', 'vehicle__chassis_number', 'vehicle__car_plate', 'resolution_note')
    date_hierarchy = 'detected_at'
    raw_id_fields = ('vehicle', 'scan', 'resolved_by')
    actions = ['mark_resolved']

    @admin.action(description=_("✅ تأشير كـ تم الحل (الآن)"))
    def mark_resolved(self, request, queryset):
        from django.utils import timezone
        n = queryset.filter(resolved_at__isnull=True).update(
            resolved_at=timezone.now(),
            resolved_by=request.user,
        )
        self.message_user(request, f"تم تأشير {n} سجل عطل كـ محلول.", messages.SUCCESS)


@admin.register(LiveTelemetryFrame)
class LiveTelemetryFrameAdmin(admin.ModelAdmin):
    list_display = ('scan', 'timestamp', 'rpm', 'engine_load_pct', 'coolant_temp_c', 'vehicle_speed_kph')
    list_filter = ('timestamp',)
    raw_id_fields = ('scan',)
    date_hierarchy = 'timestamp'


class TestStepResultInline(admin.TabularInline):
    model = TestStepResult
    extra = 0
    readonly_fields = ('recorded_at',)


@admin.register(TestPlanExecution)
class TestPlanExecutionAdmin(admin.ModelAdmin):
    list_display = ('id', 'scan', 'dtc_code', 'status', 'started_at', 'finished_at')
    list_filter = ('status', 'dtc_code')
    search_fields = ('dtc_code', 'final_conclusion')
    raw_id_fields = ('scan',)
    inlines = [TestStepResultInline]


@admin.register(APICallLog)
class APICallLogAdmin(admin.ModelAdmin):
    """💰 Cost tracker — كم بتـ كلف كل tenant على الـ external APIs."""
    list_display = (
        'timestamp', 'provider', 'endpoint', 'dtc_code', 'vin',
        'cache_hit', 'cost_usd', 'http_status', 'triggered_by',
    )
    list_filter = ('provider', 'endpoint', 'cache_hit', 'timestamp')
    search_fields = ('dtc_code', 'vin', 'error')
    date_hierarchy = 'timestamp'
    readonly_fields = (
        'timestamp', 'provider', 'endpoint', 'dtc_code', 'vin',
        'cache_hit', 'cost_usd', 'http_status', 'error', 'triggered_by',
    )

    def has_add_permission(self, request):
        return False

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        qs = self.get_queryset(request)
        summary = qs.aggregate(
            total_cost=Sum('cost_usd'),
            total_calls=Count('id'),
            cache_hits=Count('id', filter=Q(cache_hit=True)),
        )
        extra_context['cost_summary'] = summary
        return super().changelist_view(request, extra_context=extra_context)
