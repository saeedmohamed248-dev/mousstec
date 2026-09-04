"""Smart Diagnostics views — الأجهزة، الفحوصات، الأعطال."""
from smart_diagnostics.models import DiagnosticDevice, DiagnosticScan, FaultLog

from ..serializers import (
    DiagnosticDeviceSerializer,
    DiagnosticScanSerializer,
    FaultLogSerializer,
)
from .base import ReadOnlyViewSet, ListOnlyViewSet


class DiagnosticDeviceViewSet(ReadOnlyViewSet):
    serializer_class = DiagnosticDeviceSerializer
    queryset = DiagnosticDevice.objects.select_related('vehicle').order_by('-last_seen_at')


class DiagnosticScanViewSet(ReadOnlyViewSet):
    serializer_class = DiagnosticScanSerializer

    def get_queryset(self):
        qs = DiagnosticScan.objects.select_related('vehicle', 'device').order_by('-started_at')
        if self.request.query_params.get('vehicle'):
            qs = qs.filter(vehicle_id=self.request.query_params['vehicle'])
        return qs


class FaultLogViewSet(ListOnlyViewSet):
    serializer_class = FaultLogSerializer

    def get_queryset(self):
        qs = FaultLog.objects.select_related('vehicle', 'scan').order_by('-detected_at')
        params = self.request.query_params
        if params.get('vehicle'):
            qs = qs.filter(vehicle_id=params['vehicle'])
        if params.get('unresolved') == 'true':
            qs = qs.filter(resolved_at__isnull=True)
        return qs
