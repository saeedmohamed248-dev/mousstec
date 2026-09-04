"""Smart Diagnostics serializers — الأجهزة، الفحوصات، الأعطال، التيليمتري."""
from rest_framework import serializers

from smart_diagnostics.models import (
    DiagnosticDevice,
    DiagnosticScan,
    FaultLog,
    LiveTelemetryFrame,
)


class DiagnosticDeviceSerializer(serializers.ModelSerializer):
    vehicle_plate = serializers.CharField(source='vehicle.car_plate', read_only=True, default=None)

    class Meta:
        model = DiagnosticDevice
        fields = ('id', 'vehicle', 'vehicle_plate', 'hardware_id', 'is_active', 'last_seen_at')


class DiagnosticScanSerializer(serializers.ModelSerializer):
    vehicle_plate = serializers.CharField(source='vehicle.car_plate', read_only=True, default=None)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = DiagnosticScan
        fields = (
            'id', 'vehicle', 'vehicle_plate', 'device', 'technician',
            'source', 'status', 'status_display', 'started_at', 'finished_at', 'summary',
        )


class FaultLogSerializer(serializers.ModelSerializer):
    vehicle_plate = serializers.CharField(source='vehicle.car_plate', read_only=True, default=None)

    class Meta:
        model = FaultLog
        fields = (
            'id', 'vehicle', 'vehicle_plate', 'scan', 'dtc_code', 'detected_at',
            'mileage_at_detection', 'severity',
            'resolved_at', 'resolution_note',
        )


class LiveTelemetrySerializer(serializers.ModelSerializer):
    class Meta:
        model = LiveTelemetryFrame
        fields = (
            'id', 'scan', 'timestamp', 'rpm', 'engine_load_pct', 'coolant_temp_c',
            'intake_temp_c', 'vehicle_speed_kph', 'throttle_pct', 'battery_v',
        )
