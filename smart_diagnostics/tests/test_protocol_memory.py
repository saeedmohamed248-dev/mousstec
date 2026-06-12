"""
Protocol memory — stores the last OBD protocol that successfully connected
to a given vehicle so the next session can skip the ~30s sweep.

These tests cover the model invariants only (no HTTP — that needs a tenant
fixture). The model lives in the public schema, so we run inside a
TransactionTestCase that does NOT swap schemas.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from diagnostics_catalog.models import VehicleProtocolMemory


class VehicleProtocolMemoryTests(TestCase):
    def setUp(self):
        VehicleProtocolMemory.objects.all().delete()

    def test_save_with_vin(self):
        m = VehicleProtocolMemory.objects.create(
            vin='WBA12345678901234',
            protocol_code='6',
            protocol_label='CAN 11-bit / 500 kbps',
        )
        self.assertEqual(m.hit_count, 1)
        self.assertTrue(m.first_seen)

    def test_save_with_dongle_only(self):
        m = VehicleProtocolMemory.objects.create(
            dongle_id='AA:BB:CC:DD:EE:FF',
            protocol_code='3',
            protocol_label='ISO 9141-2 (K-Line)',
        )
        self.assertEqual(m.protocol_code, '3')

    def test_must_have_at_least_one_identifier(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VehicleProtocolMemory.objects.create(
                    vin='', dongle_id='', protocol_code='6',
                )

    def test_invalid_protocol_code_rejected_at_choices(self):
        # choices is enforced at form/serializer layer — the DB stores any 1-char,
        # so we test that the choices tuple covers what we accept in the API.
        codes = {c for c, _ in VehicleProtocolMemory.PROTOCOL_CHOICES}
        self.assertEqual(
            codes,
            {'1', '2', '3', '4', '5', '6', '7', '8', '9', 'A', 'B'},
            'PROTOCOL_CHOICES must match the 11 ELM327 codes.',
        )

    def test_str_repr_uses_vin_then_dongle(self):
        m = VehicleProtocolMemory.objects.create(
            vin='WBA12345678901234', protocol_code='6',
            protocol_label='CAN 500',
        )
        self.assertIn('WBA12345678901234', str(m))
        self.assertIn('CAN 500', str(m))

        m2 = VehicleProtocolMemory.objects.create(
            dongle_id='AA:BB:CC', protocol_code='3',
        )
        self.assertIn('AA:BB:CC', str(m2))

    def test_ordering_by_last_used_desc(self):
        old = VehicleProtocolMemory.objects.create(
            vin='OLDVIN12345678901', protocol_code='6',
        )
        # bump created_at backwards by saving a new one later
        new = VehicleProtocolMemory.objects.create(
            vin='NEWVIN12345678901', protocol_code='6',
        )
        # last_used defaults to now() — newer record was just created later.
        old.last_used = timezone.now().replace(year=2020)
        old.save(update_fields=['last_used'])

        first = VehicleProtocolMemory.objects.all().first()
        self.assertEqual(first.vin, new.vin)
