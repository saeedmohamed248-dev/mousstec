"""
📡 LiveTelemetryConsumer — WebSocket for live OBD data stream
================================================================
Path: ws/diagnostics/live/<vin>/

Flow:
  1. scope['tenant'] populated by TenantAuthMiddleware (asgi.py)
  2. validate subscription + diagnostics_live_data entitlement
  3. resolve Vehicle by VIN within tenant schema
  4. accept; client streams JSON frames {rpm, engine_load_pct, coolant_temp_c, ...}
  5. each frame → persist LiveTelemetryFrame + group-broadcast to dashboard viewers
  6. tenant isolation enforced by django-tenants schema switch

Two participants:
  - DEVICE (OBD2 device) — POSTs frames via send_json
  - VIEWER (dashboard browser) — receives frames via group broadcast
Role is selected via ?role=device|viewer query param.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.db import connection

logger = logging.getLogger('mouss_tec_core')

GROUP_PREFIX = 'diag_live'


class LiveTelemetryConsumer(AsyncJsonWebsocketConsumer):

    async def connect(self):
        self.tenant = self.scope.get('tenant')
        self.schema = self.scope.get('schema_name', 'public')
        self.vin = self.scope['url_route']['kwargs'].get('vin', '').upper()
        qs = self._query_params()
        self.role = (qs.get('role') or 'viewer').lower()
        self.device_token = self.scope.get('device_token') or qs.get('token')
        self.trace_id = self.scope.get('trace_id', '?')
        self.device_id = None

        if not self.tenant:
            await self._deny(4001, 'tenant_unresolved')
            return

        # 🔐 Device role MUST present a valid token (no session cookie path).
        # Viewers authenticate via the standard Django session (handled by AuthMiddlewareStack).
        if self.role == 'device':
            if not self.device_token:
                await self._deny(4401, 'device_token_required')
                return
            device_ok = await database_sync_to_async(self._authenticate_device)()
            if not device_ok:
                await self._deny(4403, 'device_token_invalid')
                return

        # Subscription + entitlement check (run inside the tenant schema)
        gate = await database_sync_to_async(self._check_access)()
        if not gate['allowed']:
            await self._deny(4003, gate['reason'])
            return

        self.vehicle_id = gate['vehicle_id']
        self.group = f"{GROUP_PREFIX}.{self.schema}.{self.vehicle_id}"

        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self.send_json({'type': 'connected', 'vin': self.vin, 'role': self.role})

    async def disconnect(self, code):
        if hasattr(self, 'group'):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if self.role != 'device':
            await self.send_json({'type': 'error', 'message': 'viewers cannot send frames'})
            return

        frame = self._sanitize_frame(content)
        if frame is None:
            await self.send_json({'type': 'error', 'message': 'invalid frame'})
            return

        scan_id = await database_sync_to_async(self._ensure_active_scan)()
        await database_sync_to_async(self._persist_frame)(scan_id, frame)

        await self.channel_layer.group_send(
            self.group,
            {'type': 'broadcast.frame', 'payload': frame},
        )

    async def broadcast_frame(self, event):
        await self.send_json({'type': 'frame', 'data': event['payload']})

    # ── helpers ─────────────────────────────────────────────────
    def _query_params(self) -> dict:
        from urllib.parse import parse_qs
        qs = self.scope.get('query_string', b'').decode('utf-8')
        return {k: v[0] for k, v in parse_qs(qs).items()}

    async def _deny(self, code: int, reason: str):
        logger.warning(f"[LiveTelemetry] deny {reason} trace={self.trace_id}")
        await self.accept()
        await self.send_json({'type': 'denied', 'reason': reason})
        await self.close(code=code)

    def _authenticate_device(self) -> bool:
        """Token-based device authentication. Confirms the device:
          1. exists & is active in the tenant schema
          2. is bound to the VIN claimed in the URL
        Also stamps last_seen_at and caches device_id for scan binding.
        """
        from django_tenants.utils import schema_context
        from smart_diagnostics.models import DiagnosticDevice
        from django.utils import timezone as _tz
        with schema_context(self.schema):
            dev = (
                DiagnosticDevice.objects
                .filter(device_token=self.device_token, is_active=True)
                .select_related('vehicle').first()
            )
            if not dev:
                return False
            if dev.vehicle.chassis_number.upper() != self.vin:
                return False
            self.device_id = dev.id
            DiagnosticDevice.objects.filter(pk=dev.pk).update(last_seen_at=_tz.now())
            return True

    def _check_access(self) -> dict:
        """Runs in tenant schema (we switch here)."""
        from django_tenants.utils import schema_context
        from inventory.models import Vehicle
        from smart_diagnostics.services.quota import (
            DiagnosticsQuotaService, FEATURE_LIVE_DATA,
        )
        with schema_context(self.schema):
            gate = DiagnosticsQuotaService.check_feature(self.tenant, FEATURE_LIVE_DATA)
            if not gate.allowed:
                return {'allowed': False, 'reason': gate.reason}
            v = Vehicle.objects.filter(chassis_number=self.vin).first()
            if not v:
                return {'allowed': False, 'reason': 'vehicle_not_found'}
            return {'allowed': True, 'vehicle_id': v.id}

    def _sanitize_frame(self, content) -> dict | None:
        if not isinstance(content, dict):
            return None
        allowed = {
            'rpm', 'engine_load_pct', 'coolant_temp_c', 'intake_temp_c',
            'vehicle_speed_kph', 'throttle_pct', 'battery_v',
        }
        frame = {k: content.get(k) for k in allowed if k in content}
        frame['ts'] = datetime.utcnow().isoformat()
        return frame

    def _ensure_active_scan(self) -> int:
        from django_tenants.utils import schema_context
        from smart_diagnostics.models import DiagnosticScan
        with schema_context(self.schema):
            scan = (
                DiagnosticScan.objects
                .filter(vehicle_id=self.vehicle_id, status='in_progress', source='live_obd')
                .order_by('-started_at').first()
            )
            if scan:
                return scan.id
            return DiagnosticScan.objects.create(
                vehicle_id=self.vehicle_id,
                device_id=self.device_id,
                source='live_obd',
                status='in_progress',
            ).id

    def _persist_frame(self, scan_id: int, frame: dict):
        from django_tenants.utils import schema_context
        from smart_diagnostics.models import LiveTelemetryFrame
        with schema_context(self.schema):
            LiveTelemetryFrame.objects.create(
                scan_id=scan_id,
                rpm=frame.get('rpm'),
                engine_load_pct=frame.get('engine_load_pct'),
                coolant_temp_c=frame.get('coolant_temp_c'),
                intake_temp_c=frame.get('intake_temp_c'),
                vehicle_speed_kph=frame.get('vehicle_speed_kph'),
                throttle_pct=frame.get('throttle_pct'),
                battery_v=frame.get('battery_v'),
                raw=frame,
            )
