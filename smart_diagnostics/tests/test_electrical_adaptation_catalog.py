"""
Coverage tests for the Battery Health thresholds + Adaptation/Relearn
procedure catalog. Both ship as a single JS file (electrical_adaptation_
catalog.js) — these tests parse the source to catch shape regressions
and ensure every supported make has at least one procedure.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

CATALOG = (Path(__file__).resolve().parent.parent / 'static' /
           'smart_diagnostics' / 'js' / 'electrical_adaptation_catalog.js')

# Every make we ship DIDs for in uds_did_catalog.js should also have at
# least one adaptation procedure (or be aliased to one). 'generic' covers
# the universal clear-adaptive flow.
REQUIRED_PROCEDURE_MAKES = {
    'generic', 'bmw', 'vag', 'toyota', 'hyundai', 'honda', 'ford', 'nissan',
}

REQUIRED_BATTERY_PHASES = {'rest', 'crank', 'idle_charging', 'rev_charging'}

# Severity levels the UI knows how to render. Anything else is a typo.
ALLOWED_SEVERITIES = {'low', 'medium', 'high'}

# Allowed adaptation step types. Adding a new one without teaching the
# driver runAdaptationStep() about it would silently no-op.
ALLOWED_STEP_TYPES = {'manual', 'wait', 'clear', 'session', 'write'}


class ElectricalAdaptationCatalogTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.src = CATALOG.read_text(encoding='utf-8')

    def test_file_exists(self):
        self.assertTrue(CATALOG.exists(), f'Missing {CATALOG}')

    def test_battery_thresholds_present(self):
        self.assertIn('BATTERY_HEALTH_THRESHOLDS', self.src)
        for phase in REQUIRED_BATTERY_PHASES:
            self.assertIn(
                f'{phase}:', self.src,
                f'Phase "{phase}" missing from BATTERY_HEALTH_THRESHOLDS',
            )

    def test_verdict_function_present(self):
        self.assertIn('verdictForPhase', self.src)
        self.assertIn("window.verdictForPhase", self.src)

    def test_every_required_make_has_a_procedure(self):
        # Extract `make: '<value>'` occurrences from procedure objects.
        makes = set(re.findall(r"make:\s*['\"]([a-z\-]+)['\"]", self.src))
        missing = REQUIRED_PROCEDURE_MAKES - makes
        self.assertFalse(
            missing,
            f'Adaptation catalog missing procedures for: {sorted(missing)}',
        )

    def test_severities_are_valid(self):
        severities = re.findall(r"severity:\s*['\"]([a-z]+)['\"]", self.src)
        self.assertTrue(severities, 'No severity declarations found')
        bad = set(severities) - ALLOWED_SEVERITIES
        self.assertFalse(bad, f'Unknown severity levels: {bad}')

    def test_step_types_are_valid(self):
        types = re.findall(r"type:\s*['\"]([a-z]+)['\"]", self.src)
        self.assertTrue(types, 'No step types found')
        bad = set(types) - ALLOWED_STEP_TYPES
        self.assertFalse(
            bad,
            f'Unknown step types: {bad}. Add to runAdaptationStep() switch '
            f'or fix the typo.',
        )

    def test_bmw_battery_registration_exists(self):
        """BMW battery registration is the canonical destructive UDS-write
        procedure — without it BMW owners can't replace a battery properly."""
        self.assertIn('bmw_battery_registration', self.src)
        # Must include a 2E write step with a DID and a data_input.
        self.assertRegex(self.src, r"type:\s*['\"]write['\"]")
        self.assertIn("data_input", self.src)

    def test_getAdaptationProceduresForMake_exported(self):
        self.assertIn('window.getAdaptationProceduresForMake', self.src)

    def test_catalog_loaded_in_template(self):
        tpl = (Path(__file__).resolve().parent.parent / 'templates' /
               'smart_diagnostics' / 'diagnostics_room.html')
        self.assertIn(
            'electrical_adaptation_catalog.js',
            tpl.read_text(encoding='utf-8'),
            'diagnostics_room.html must include electrical_adaptation_catalog.js',
        )

    def test_new_buttons_wired_in_template(self):
        tpl = (Path(__file__).resolve().parent.parent / 'templates' /
               'smart_diagnostics' / 'diagnostics_room.html').read_text(encoding='utf-8')
        for btn_id, method in (
            ('btnBatteryHealth',   'runBatteryChargingTest'),
            ('btnAdaptationMenu',  'runAdaptationStep'),
        ):
            self.assertIn(
                f'id="{btn_id}"', tpl,
                f'diagnostics_room.html missing button {btn_id}',
            )
            self.assertIn(
                f'obd.{method}', tpl,
                f'Button {btn_id} click handler must call obd.{method}',
            )
