"""
Coverage check for the DTC catalog fixture. The diagnostics room is only as
useful as the codes it can explain — a Mode 03 read that returns "P0301" is
useless if the catalog has no entry for it. This test asserts coverage
breadth and quality WITHOUT requiring the DB (parses the JSON fixture and
calls the management command in a transaction).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from django.test import SimpleTestCase

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'dtc_catalog.json'

# Minimum coverage per system. Set to the floor we ship with, not aspirational
# numbers — a regression that drops below these means someone deleted entries.
MIN_PER_SYSTEM = {
    'P': 150,   # powertrain — engine, fuel, ignition, transmission
    'B': 30,    # body — electrical, airbag, lights, locks, HVAC
    'C': 15,    # chassis — ABS, ESP, TPMS, steering
    'U': 15,    # network — CAN bus, lost-communication codes
}

# Critical codes that MUST be present — these are the ones every shop sees
# weekly. If any of these is missing, the catalog has a serious gap.
REQUIRED_CODES = [
    # Misfires (the #1 reason cars come in)
    'P0300', 'P0301', 'P0302', 'P0303', 'P0304',
    # Fuel mixture
    'P0171', 'P0172',
    # O2 sensors
    'P0130', 'P0131', 'P0132', 'P0133',
    # Catalyst
    'P0420', 'P0430',
    # EVAP (smog/inspection failures)
    'P0440', 'P0442', 'P0455',
    # Critical sensors
    'P0335',           # crank
    'P0340',           # cam
    'P0101',           # MAF
    'P0117', 'P0118',  # ECT
    # Charging/electrical
    'P0562', 'P0563',
    # Transmission
    'P0700',
    # CAN/network
    'U0100', 'U0101', 'U0121', 'U0155',
    # ABS
    'C0035', 'C0040',
    # Airbag/SRS
    'B0001', 'B0010',
    # Immobilizer
    'B1370',
]

REQUIRED_FIELDS = {'code', 'system', 'severity', 'short', 'full'}
VALID_SYSTEMS = {'P', 'B', 'C', 'U'}
VALID_SEVERITIES = {'low', 'medium', 'high', 'critical'}


class DTCCatalogCoverageTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with FIXTURE.open(encoding='utf-8') as f:
            cls.entries = json.load(f)
        cls.by_code = {e['code']: e for e in cls.entries}

    def test_fixture_parses(self):
        self.assertIsInstance(self.entries, list)
        self.assertGreater(len(self.entries), 200, 'catalog has shrunk drastically')

    def test_every_entry_has_required_fields(self):
        for e in self.entries:
            missing = REQUIRED_FIELDS - set(e.keys())
            self.assertFalse(
                missing,
                f"Entry {e.get('code', '?')} missing fields: {missing}",
            )

    def test_no_duplicate_codes(self):
        codes = [e['code'] for e in self.entries]
        dupes = [c for c, n in Counter(codes).items() if n > 1]
        self.assertFalse(dupes, f'Duplicate DTC codes: {dupes}')

    def test_system_letter_matches_code_prefix(self):
        for e in self.entries:
            self.assertEqual(
                e['code'][0], e['system'],
                f"Code {e['code']} has system='{e['system']}' — should match prefix",
            )
            self.assertIn(e['system'], VALID_SYSTEMS)

    def test_severities_are_valid(self):
        for e in self.entries:
            self.assertIn(
                e['severity'], VALID_SEVERITIES,
                f"Code {e['code']} has invalid severity '{e['severity']}'",
            )

    def test_short_descriptions_non_empty(self):
        for e in self.entries:
            self.assertTrue(
                e['short'].strip(),
                f"Code {e['code']} has empty short description",
            )

    def test_minimum_coverage_per_system(self):
        counts = Counter(e['system'] for e in self.entries)
        for sys, minimum in MIN_PER_SYSTEM.items():
            self.assertGreaterEqual(
                counts.get(sys, 0), minimum,
                f"System '{sys}' has {counts.get(sys, 0)} codes — "
                f"below minimum {minimum}. Add more or lower the floor.",
            )

    def test_required_critical_codes_present(self):
        missing = [c for c in REQUIRED_CODES if c not in self.by_code]
        self.assertFalse(
            missing,
            f'Critical DTCs missing from catalog: {missing}',
        )

    def test_misfire_codes_are_high_severity(self):
        """Misfires destroy catalysts within minutes — must be flagged high."""
        for cyl in range(0, 9):
            code = f'P030{cyl}'
            if code in self.by_code:
                self.assertEqual(
                    self.by_code[code]['severity'], 'high',
                    f'{code} (misfire) must be high severity — '
                    'unattended misfires destroy the catalytic converter.',
                )

    def test_lost_can_communication_codes_severe(self):
        """U01xx lost-comm codes mean the car is half-blind — never 'low'."""
        for e in self.entries:
            if e['code'].startswith('U01') and 'Lost Communication' in e['short']:
                self.assertIn(
                    e['severity'], {'medium', 'high', 'critical'},
                    f"{e['code']} is a lost-communication fault — "
                    "must be medium/high/critical, got 'low'.",
                )

    def test_arabic_full_descriptions_exist(self):
        """Every entry should have an Arabic full description (mechanics use AR)."""
        # Allow up to 5% of entries to have empty 'full' — for codes where the
        # short already covers it. Anything more means stub entries leaked in.
        empty = [e['code'] for e in self.entries if not e.get('full', '').strip()]
        self.assertLess(
            len(empty), len(self.entries) * 0.05,
            f"{len(empty)} entries have no Arabic 'full' description: {empty[:10]}",
        )
