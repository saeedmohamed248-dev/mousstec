"""
Coverage check: both OBD drivers (Bluetooth & Wi-Fi) must implement every
ELM327 protocol AND every SAE J1979 diagnostic service mode the diagnostics
room relies on. The drivers are pure JS, so we parse the source files and
assert on their content — no browser needed.

If you add or remove a protocol/mode, update REQUIRED_* below.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

STATIC_JS = Path(__file__).resolve().parent.parent / 'static' / 'smart_diagnostics' / 'js'
BT_PATH = STATIC_JS / 'obd_bluetooth.js'
WIFI_PATH = STATIC_JS / 'obd_wifi.js'
UDS_PATH = STATIC_JS / 'uds_did_catalog.js'

# All 11 ELM327 protocol codes. 1-9 = standard OBD-II buses,
# A = auto-search, B = SAE J1939 (heavy-duty CAN).
REQUIRED_PROTOCOL_CODES = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'A', 'B']

# SAE J1979 service modes the diagnostics room actually wires up.
# Method name → minimum-required ELM commands the method must issue.
REQUIRED_SERVICE_METHODS = {
    'streamLiveData':           ['01'],          # Mode 01 live data
    'readFreezeFrame':          ['02'],          # Mode 02 freeze frame
    'readDTCs':                 ['03', '07', '0A'],  # stored + pending + permanent
    'clearDTCs':                ['04'],          # Mode 04
    'readO2MonitoringResults':  ['05'],          # Mode 05 (legacy non-CAN)
    'readMisfireCounts':        ['06'],          # Mode 06 on-board monitoring
    'requestComponentTest':     ['08'],          # Mode 08 bidirectional
    'readDataByIdentifier':     ['22'],          # Mode 22 UDS DID read
    'readModuleStandardInfo':   [],              # uses readDataByIdentifier
    'readSupportedPIDs':        ['0100'],        # Mode 01 PID 00 bitmask
    'writeDataByIdentifier':    ['2E'],          # UDS Mode 0x2E write
    'setDiagnosticSession':     ['10'],          # UDS Mode 0x10 session ctrl
    'runBatteryChargingTest':   ['0142'],        # PID 42 control_voltage
    'sampleBatteryVoltage':     ['0142'],
    'runAdaptationStep':        [],              # generic step runner
    'readVehicleInfo':          ['09'],          # Mode 09 CalID/CVN/ECU name
    'readVIN':                  ['0902'],        # Mode 09 PID 02
    'readReadinessMonitors':    ['0101'],        # Mode 01 PID 01 readiness
    'probeCapabilities':        [],              # capability sniff
}

# PID parser definitions both drivers must include — adding a new code below
# without adding the matching `'<HEX>': { label: ...` entry to the PIDs table
# in BOTH JS files will fail this test.
REQUIRED_PID_LABELS = {
    '0C': 'rpm',
    '0D': 'speed_kph',
    '05': 'coolant_temp_c',
    '11': 'throttle_pct',
    '42': 'control_voltage',
    # Status / accumulators
    '03': 'fuel_system_status',
    '1F': 'run_time_s',
    '21': 'dist_with_mil_km',
    '31': 'dist_since_clear_km',
    '4D': 'mil_on_min',
    # Pedal
    '49': 'accel_pedal_d_pct',
    '4C': 'cmd_throttle_pct',
    # Fuel type / hybrid
    '51': 'fuel_type',
    '52': 'ethanol_fuel_pct',
    '5B': 'hybrid_battery_pct',
    # Diesel
    '78': 'egt_bank1_c',
    '7C': 'dpf_temp_c',
}


def _atsp_codes(src: str) -> set[str]:
    """Pull every ATSP<code> appearance — that's how the driver selects a bus."""
    return {m.group(1).upper() for m in re.finditer(r"ATSP\s*'?\s*\+?\s*[\"']?([0-9A-Ba-b])", src)}


def _protocol_table_codes(src: str) -> set[str]:
    """Codes declared in the protocols=[{code:'X',...}] table."""
    return {m.group(1).upper() for m in re.finditer(r"code:\s*['\"]([0-9A-Ba-b])['\"]", src)}


class OBDProtocolCoverageTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bt = BT_PATH.read_text(encoding='utf-8')
        cls.wifi = WIFI_PATH.read_text(encoding='utf-8')

    def test_bluetooth_driver_exists(self):
        self.assertTrue(BT_PATH.exists(), f'Missing {BT_PATH}')
        self.assertIn('class OBDBluetooth', self.bt)

    def test_wifi_driver_exists(self):
        self.assertTrue(WIFI_PATH.exists(), f'Missing {WIFI_PATH}')
        self.assertIn('class OBDWiFi', self.wifi)

    def test_bluetooth_covers_every_protocol_code(self):
        codes = _protocol_table_codes(self.bt)
        missing = set(REQUIRED_PROTOCOL_CODES) - codes
        self.assertFalse(
            missing,
            f'obd_bluetooth.js is missing protocol codes: {sorted(missing)}. '
            f'Found: {sorted(codes)}',
        )

    def test_wifi_covers_every_protocol_code(self):
        codes = _protocol_table_codes(self.wifi)
        missing = set(REQUIRED_PROTOCOL_CODES) - codes
        self.assertFalse(
            missing,
            f'obd_wifi.js is missing protocol codes: {sorted(missing)}. '
            f'Found: {sorted(codes)}',
        )

    def test_both_drivers_have_same_protocol_set(self):
        self.assertEqual(
            _protocol_table_codes(self.bt),
            _protocol_table_codes(self.wifi),
            'Bluetooth and Wi-Fi drivers must support the same protocol set so '
            'mechanics get identical coverage regardless of transport.',
        )

    def test_bluetooth_exposes_every_service_mode_method(self):
        for method, cmds in REQUIRED_SERVICE_METHODS.items():
            self.assertIn(
                f'{method}(', self.bt,
                f'obd_bluetooth.js missing method `{method}` — '
                'it should be at parity with obd_wifi.js.',
            )
            for cmd in cmds:
                self.assertRegex(
                    self.bt, re.escape(cmd),
                    f'obd_bluetooth.js method `{method}` must issue ELM command `{cmd}`.',
                )

    def test_wifi_exposes_every_service_mode_method(self):
        for method, cmds in REQUIRED_SERVICE_METHODS.items():
            self.assertIn(
                f'{method}(', self.wifi,
                f'obd_wifi.js missing method `{method}`.',
            )
            for cmd in cmds:
                self.assertRegex(
                    self.wifi, re.escape(cmd),
                    f'obd_wifi.js method `{method}` must issue ELM command `{cmd}`.',
                )

    def test_bluetooth_atsp_uses_real_codes(self):
        atsp = _atsp_codes(self.bt)
        unknown = atsp - set(REQUIRED_PROTOCOL_CODES) - {'0'}
        self.assertFalse(
            unknown,
            f'obd_bluetooth.js references unknown ATSP codes: {sorted(unknown)}',
        )

    def test_bluetooth_has_required_pids(self):
        for pid, label in REQUIRED_PID_LABELS.items():
            self.assertRegex(
                self.bt, rf"['\"]?{pid}['\"]?\s*:\s*\{{[^}}]*label:\s*['\"]{label}['\"]",
                f'obd_bluetooth.js missing PID {pid} ({label}).',
            )

    def test_wifi_has_required_pids(self):
        for pid, label in REQUIRED_PID_LABELS.items():
            self.assertRegex(
                self.wifi, rf"['\"]?{pid}['\"]?\s*:\s*\{{[^}}]*label:\s*['\"]{label}['\"]",
                f'obd_wifi.js missing PID {pid} ({label}).',
            )

    def test_uds_did_catalog_exists(self):
        self.assertTrue(UDS_PATH.exists(), f'Missing {UDS_PATH}')
        src = UDS_PATH.read_text(encoding='utf-8')
        # ISO 14229 reserved DIDs every compliant ECU should expose.
        for did in ('F186', 'F18C', 'F190', 'F195'):
            self.assertIn(f"'{did}'", src, f'UDS standard DID {did} missing from catalog')
        # Module address table — at least engine + non-engine modules.
        for mod in ('engine', 'abs', 'airbag', 'bcm'):
            self.assertIn(f'{mod}:', src, f'UDS module "{mod}" missing from catalog')
        # Manufacturer-specific catalogs we ship with. Every make that's
        # common in the Egyptian/MENA market should be present.
        for name in ('TOYOTA_DIDS', 'HYUNDAI_DIDS', 'VAG_DIDS', 'BMW_DIDS',
                     'MERCEDES_DIDS', 'NISSAN_DIDS', 'HONDA_DIDS', 'FORD_DIDS'):
            self.assertIn(name, src, f'Manufacturer catalog {name} missing')
        # Make→catalog lookup must cover at least these makes + their sister
        # brands (lexus→toyota, kia→hyundai, mini→bmw, infiniti→nissan, etc).
        for make in ('toyota:', 'hyundai:', 'bmw:', 'mercedes:', 'nissan:',
                     'honda:', 'ford:', 'lexus:', 'kia:', 'mini:'):
            self.assertIn(
                make, src,
                f'getDIDsForMake() must recognize "{make.rstrip(":")}"',
            )

    def test_uds_catalog_loaded_in_template(self):
        tpl = Path(__file__).resolve().parent.parent / 'templates' / \
              'smart_diagnostics' / 'diagnostics_room.html'
        self.assertIn(
            'uds_did_catalog.js', tpl.read_text(encoding='utf-8'),
            'diagnostics_room.html must include uds_did_catalog.js so the '
            'UDS_MODULES/UDS_STANDARD_DIDS globals are available to the drivers.',
        )

    def test_new_diagnostic_buttons_wired_in_template(self):
        """Mode 22 / Mode 08 / supported-PIDs buttons must exist AND have
        click handlers calling the driver methods. Without both halves the
        feature is invisible to the mechanic."""
        tpl = (Path(__file__).resolve().parent.parent / 'templates' /
               'smart_diagnostics' / 'diagnostics_room.html').read_text(encoding='utf-8')
        for btn_id, method in (
            ('btnSupportedPIDs', 'readSupportedPIDs'),
            ('btnModuleInfo',    'readModuleStandardInfo'),
            ('btnEvapLeakTest',  'requestComponentTest'),
        ):
            self.assertIn(
                f'id="{btn_id}"', tpl,
                f'diagnostics_room.html missing button {btn_id}',
            )
            self.assertIn(
                f'obd.{method}', tpl,
                f'Button {btn_id} click handler must call obd.{method}',
            )

    def test_protocol_memory_client_loaded_in_template(self):
        tpl = Path(__file__).resolve().parent.parent / 'templates' / \
              'smart_diagnostics' / 'diagnostics_room.html'
        self.assertIn(
            'protocol_memory_client.js', tpl.read_text(encoding='utf-8'),
            'diagnostics_room.html must include protocol_memory_client.js '
            'so the drivers can cache the successful protocol per vehicle.',
        )

    def test_mode22_is_gated_to_can_protocols(self):
        """Mode 22 + ATSH/ATCRA only work on CAN. The drivers must throw a
        helpful error on K-Line/J1850 rather than firing a doomed request."""
        for label, src in (('bluetooth', self.bt), ('wifi', self.wifi)):
            self.assertIn(
                'CAN_PROTOCOLS', src,
                f'obd_{label}.js must define a CAN protocol set to gate Mode 22.',
            )
            # Must reference the protocol code the driver remembers after init.
            self.assertIn(
                '_lastProtocolCode', src,
                f'obd_{label}.js Mode 22 gate must use _lastProtocolCode.',
            )

    def test_drivers_use_protocol_memory(self):
        """Both drivers must call lookup() before sweep and save() after success.
        Without this, the model + endpoints we built never actually help."""
        for label, src in (('bluetooth', self.bt), ('wifi', self.wifi)):
            self.assertIn(
                'ProtocolMemoryClient.lookup', src,
                f'obd_{label}.js must call ProtocolMemoryClient.lookup before sweep.',
            )
            self.assertIn(
                'ProtocolMemoryClient.save', src,
                f'obd_{label}.js must call ProtocolMemoryClient.save after success.',
            )
            self.assertIn(
                'ProtocolMemoryClient.reorder', src,
                f'obd_{label}.js must use reorder() to try the cached protocol first.',
            )

    def test_wifi_atsp_uses_real_codes(self):
        atsp = _atsp_codes(self.wifi)
        unknown = atsp - set(REQUIRED_PROTOCOL_CODES) - {'0'}
        self.assertFalse(
            unknown,
            f'obd_wifi.js references unknown ATSP codes: {sorted(unknown)}',
        )
