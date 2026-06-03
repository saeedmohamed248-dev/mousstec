"""Token-based device auth for the live WebSocket consumer."""
import secrets

from django.db import connection

from smart_diagnostics.tests.base import DiagnosticsTenantTestCase
from smart_diagnostics.tests.factories import (
    get_or_create_premium_plan,
    attach_subscription,
    make_customer_and_vehicle,
)


class DeviceAuthTest(DiagnosticsTenantTestCase):
    """Drives the consumer's _authenticate_device + _check_access methods
    directly (no full ASGI roundtrip needed for the logic under test)."""

    def _make_consumer(self, vin, token, role='device'):
        from smart_diagnostics.consumers import LiveTelemetryConsumer
        c = LiveTelemetryConsumer()
        c.scope = {
            'tenant': self.tenant,
            'schema_name': self.tenant.schema_name,
            'device_token': token,
            'trace_id': 'TST',
        }
        c.tenant = self.tenant
        c.schema = self.tenant.schema_name
        c.vin = vin.upper()
        c.role = role
        c.device_token = token
        c.device_id = None
        return c

    def _bootstrap(self):
        connection.set_schema_to_public()
        plan = get_or_create_premium_plan()
        attach_subscription(self.tenant, plan, quota_remaining=10)
        connection.set_tenant(self.tenant)
        return make_customer_and_vehicle(vin='AUTHTEST111111111', plate='AUT-1')

    def test_valid_token_authenticates(self):
        from smart_diagnostics.models import DiagnosticDevice
        vehicle = self._bootstrap()
        token = secrets.token_urlsafe(32)
        DiagnosticDevice.objects.create(vehicle=vehicle, device_token=token, is_active=True)

        c = self._make_consumer(vehicle.chassis_number, token)
        self.assertTrue(c._authenticate_device())
        self.assertIsNotNone(c.device_id)

    def test_invalid_token_rejected(self):
        self._bootstrap()
        c = self._make_consumer('AUTHTEST111111111', 'not-a-real-token')
        self.assertFalse(c._authenticate_device())

    def test_inactive_device_rejected(self):
        from smart_diagnostics.models import DiagnosticDevice
        vehicle = self._bootstrap()
        token = secrets.token_urlsafe(32)
        DiagnosticDevice.objects.create(vehicle=vehicle, device_token=token, is_active=False)
        c = self._make_consumer(vehicle.chassis_number, token)
        self.assertFalse(c._authenticate_device())

    def test_token_bound_to_different_vehicle_rejected(self):
        from smart_diagnostics.models import DiagnosticDevice
        vehicle = self._bootstrap()
        other = make_customer_and_vehicle(vin='OTHER222222222222', plate='OT-2')
        token = secrets.token_urlsafe(32)
        DiagnosticDevice.objects.create(vehicle=vehicle, device_token=token, is_active=True)

        # Try to claim the OTHER VIN with vehicle's token
        c = self._make_consumer(other.chassis_number, token)
        self.assertFalse(c._authenticate_device())

    def test_portable_device_accepts_any_vehicle(self):
        """Workshop scanners (vehicle is NULL) authenticate for any tenant VIN."""
        from smart_diagnostics.models import DiagnosticDevice
        vehicle_a = self._bootstrap()
        vehicle_b = make_customer_and_vehicle(vin='PORTABLE000000001', plate='P-1')
        token = secrets.token_urlsafe(32)
        # Portable device — NO vehicle binding
        DiagnosticDevice.objects.create(vehicle=None, device_token=token, is_active=True)

        # Same token authenticates for BOTH vehicles
        c1 = self._make_consumer(vehicle_a.chassis_number, token)
        self.assertTrue(c1._authenticate_device())

        c2 = self._make_consumer(vehicle_b.chassis_number, token)
        self.assertTrue(c2._authenticate_device())

    def test_last_seen_at_stamped(self):
        from smart_diagnostics.models import DiagnosticDevice
        vehicle = self._bootstrap()
        token = secrets.token_urlsafe(32)
        d = DiagnosticDevice.objects.create(vehicle=vehicle, device_token=token, is_active=True)
        self.assertIsNone(d.last_seen_at)

        c = self._make_consumer(vehicle.chassis_number, token)
        c._authenticate_device()
        d.refresh_from_db()
        self.assertIsNotNone(d.last_seen_at)
