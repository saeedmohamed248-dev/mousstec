"""
View-level tests for the protocol-memory endpoints.

We bypass URL routing (django-tenants makes that brittle in unit tests —
the URLConf for /api/diagnostics/ is tenant-mounted) and exercise the view
callables directly through APIRequestFactory. This catches view-layer
regressions in request parsing, upsert semantics, and read-after-write
behavior — the things the JS drivers depend on.
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from diagnostics_catalog.models import VehicleProtocolMemory
from smart_diagnostics.api.views import (
    protocol_memory_lookup,
    protocol_memory_save,
)


class ProtocolMemoryAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user(
            username='diag_test',
            email='diag@test.local',
            password='unused-force-auth',
        )

    def setUp(self):
        prev = connection.schema_name
        if prev != 'public':
            connection.set_schema_to_public()
        VehicleProtocolMemory.objects.all().delete()
        self.factory = APIRequestFactory()

    def _lookup(self, **params):
        qs = '&'.join(f'{k}={v}' for k, v in params.items())
        req = self.factory.get(f'/api/diagnostics/protocol-memory/?{qs}')
        force_authenticate(req, user=self.user)
        return protocol_memory_lookup(req)

    def _save(self, **body):
        req = self.factory.post(
            '/api/diagnostics/protocol-memory/save/',
            data=json.dumps(body),
            content_type='application/json',
        )
        force_authenticate(req, user=self.user)
        return protocol_memory_save(req)

    # ── lookup ──────────────────────────────────────────────────────────
    def test_lookup_requires_an_identifier(self):
        req = self.factory.get('/api/diagnostics/protocol-memory/')
        force_authenticate(req, user=self.user)
        r = protocol_memory_lookup(req)
        self.assertEqual(r.status_code, 400)
        self.assertIn('error', r.data)

    def test_lookup_returns_not_found_for_unknown_vehicle(self):
        r = self._lookup(dongle_id='AA:BB')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, {'found': False})

    def test_lookup_by_dongle_id(self):
        VehicleProtocolMemory.objects.create(
            dongle_id='AA:BB', protocol_code='6', protocol_label='CAN 500',
        )
        r = self._lookup(dongle_id='AA:BB')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['found'])
        self.assertEqual(r.data['protocol_code'], '6')
        self.assertEqual(r.data['protocol_label'], 'CAN 500')

    def test_lookup_prefers_vin_over_dongle(self):
        VehicleProtocolMemory.objects.create(
            vin='WBA12345678901234', protocol_code='6', protocol_label='CAN',
        )
        VehicleProtocolMemory.objects.create(
            dongle_id='AA:BB', protocol_code='3', protocol_label='K-Line',
        )
        r = self._lookup(vin='WBA12345678901234', dongle_id='AA:BB')
        self.assertEqual(r.data['protocol_code'], '6')

    # ── save ────────────────────────────────────────────────────────────
    def test_save_creates_new_record(self):
        r = self._save(dongle_id='CC:DD', protocol_code='6',
                       protocol_label='CAN 11-bit 500', sweep_seconds_saved=12.5)
        self.assertEqual(r.status_code, 201, getattr(r, 'data', r))
        self.assertTrue(VehicleProtocolMemory.objects.filter(dongle_id='CC:DD').exists())

    def test_save_upserts_and_increments_hit_count(self):
        self._save(dongle_id='CC:DD', protocol_code='6')
        r = self._save(dongle_id='CC:DD', protocol_code='6')
        self.assertEqual(r.status_code, 200)  # 200 = updated, not created
        self.assertEqual(r.data['hit_count'], 2)

    def test_save_links_vin_to_existing_dongle_record(self):
        """Driver writes the dongle record at init, then writes again with
        the VIN once Mode 09 returns it. Same row must be linked, not duped."""
        self._save(dongle_id='EE:FF', protocol_code='6')
        self._save(dongle_id='EE:FF', vin='WBA99988877766655', protocol_code='6')
        rows = VehicleProtocolMemory.objects.filter(dongle_id='EE:FF')
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().vin, 'WBA99988877766655')

    def test_save_rejects_invalid_protocol_code(self):
        r = self._save(dongle_id='XX', protocol_code='Z')
        self.assertEqual(r.status_code, 400)

    def test_save_rejects_missing_identifiers(self):
        r = self._save(protocol_code='6')
        self.assertEqual(r.status_code, 400)

    def test_endpoints_require_authentication(self):
        # No force_authenticate — IsAuthenticated should reject.
        req = self.factory.get('/api/diagnostics/protocol-memory/?dongle_id=AA')
        r = protocol_memory_lookup(req)
        self.assertIn(r.status_code, (401, 403))

        req2 = self.factory.post(
            '/api/diagnostics/protocol-memory/save/',
            data=json.dumps({'dongle_id': 'AA', 'protocol_code': '6'}),
            content_type='application/json',
        )
        r2 = protocol_memory_save(req2)
        self.assertIn(r2.status_code, (401, 403))
