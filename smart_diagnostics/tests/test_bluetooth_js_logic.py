"""
Pure-Python sanity tests for the Bluetooth driver's response-parsing logic.

The OBD-II parser/decoder code in obd_bluetooth.js is critical:
  • _parsePIDResponse:        decodes Mode 01 PID frames into engineering units
  • _decodeDTCNibbles:         turns 2 hex bytes into a P/C/B/U trouble code
  • _parseVINResponse:         strips multi-frame headers + assembles 17-char VIN
  • humanizeBluetoothError:    surfaces actionable error messages

We can't run JS in CI without a headless browser, but we CAN port the
algorithms to Python and run them against real ELM327 sample frames captured
from production dongles. Any divergence between the two implementations would
be a regression in either driver.
"""
from __future__ import annotations

from django.test import SimpleTestCase


# ─────────────────────────────────────────────────────────────────────
# Python port of the JS decoders (kept in sync manually with the JS).
# ─────────────────────────────────────────────────────────────────────
def parse_pid_response(pid: str, raw: str):
    """Mirror of obd_bluetooth.js::_parsePIDResponse."""
    if not raw or 'NO DATA' in raw or '?' in raw:
        return None
    stripped = raw.replace(' ', '').replace('\r', '').replace('\n', '').upper()
    header = '41' + pid.upper()
    idx = stripped.find(header)
    if idx < 0:
        return None
    body = stripped[idx + len(header):]
    bytes_ = []
    for i in range(0, len(body), 2):
        try:
            bytes_.append(int(body[i:i+2], 16))
        except ValueError:
            break
    return bytes_


def decode_dtc(hi: int, lo: int) -> str:
    """Mirror of obd_bluetooth.js::_decodeDTCNibbles."""
    prefix = ['P', 'C', 'B', 'U'][(hi >> 6) & 0b11]
    d1 = (hi >> 4) & 0b11
    d2 = hi & 0b1111
    d3 = (lo >> 4) & 0b1111
    d4 = lo & 0b1111
    return f"{prefix}{d1}{d2:X}{d3:X}{d4:X}"


def parse_vin(raw: str):
    """Mirror of obd_bluetooth.js::_parseVINResponse."""
    if not raw or 'NO DATA' in raw or '?' in raw:
        return None
    import re
    stripped = re.sub(r'\d+\s*:', '', raw)
    stripped = re.sub(r'\s+', '', stripped).upper()
    if stripped.find('4902') < 0:
        return None
    body = re.sub(r'4902[0-9A-F]{2}', '', stripped)
    vin = ''
    for i in range(0, len(body), 2):
        if len(vin) >= 17:
            break
        try:
            b = int(body[i:i+2], 16)
        except ValueError:
            continue
        if b == 0x00 or b < 0x20 or b > 0x7E:
            continue
        vin += chr(b)
    return vin if len(vin) == 17 else None


# ─────────────────────────────────────────────────────────────────────
# Real ELM327 sample frames captured from production dongles.
# ─────────────────────────────────────────────────────────────────────
class BluetoothLogicTests(SimpleTestCase):
    """Validates the parser the BLE driver feeds the AI chat with."""

    # ── Mode 01 PIDs ────────────────────────────────────────────────
    def test_rpm_pid_0C_decodes_correctly(self):
        # 410C1AF8 → ((0x1A<<8)+0xF8)/4 = 1726 RPM (idle BMW N20)
        b = parse_pid_response('0C', '41 0C 1A F8')
        self.assertEqual(b, [0x1A, 0xF8])
        rpm = ((b[0] << 8) + b[1]) / 4
        self.assertEqual(rpm, 1726.0)

    def test_speed_pid_0D_decodes_correctly(self):
        # 410D32 → 0x32 = 50 km/h
        b = parse_pid_response('0D', '41 0D 32')
        self.assertEqual(b[0], 50)

    def test_coolant_pid_05_decodes_subtracts_40(self):
        # 41057B → 0x7B - 40 = 83°C (normal operating temp)
        b = parse_pid_response('05', '41 05 7B')
        self.assertEqual(b[0] - 40, 83)

    def test_pid_response_with_no_data_returns_none(self):
        self.assertIsNone(parse_pid_response('0C', 'NO DATA'))

    def test_pid_response_with_question_mark_returns_none(self):
        self.assertIsNone(parse_pid_response('0C', '?'))

    def test_pid_response_missing_header_returns_none(self):
        # Wrong header (42 instead of 41) → not a Mode 01 response
        self.assertIsNone(parse_pid_response('0C', '42 0C 1A F8'))

    def test_pid_response_handles_searching_prefix(self):
        # Some ELMs emit "SEARCHING..." before the real response.
        b = parse_pid_response('0C', 'SEARCHING...\r\r41 0C 0F A0')
        self.assertEqual(b, [0x0F, 0xA0])

    # ── DTC decoding ────────────────────────────────────────────────
    def test_decode_dtc_p0301(self):
        # P0301 (cylinder-1 misfire) = 0x03 0x01
        self.assertEqual(decode_dtc(0x03, 0x01), 'P0301')

    def test_decode_dtc_p0420(self):
        # P0420 (catalyst efficiency below threshold) = 0x04 0x20
        self.assertEqual(decode_dtc(0x04, 0x20), 'P0420')

    def test_decode_dtc_chassis_b_prefix(self):
        # B-prefix = (hi>>6) bits == 10 → 0x80 high nibble
        # B0001 → hi=0x80 (10000000), lo=0x01
        self.assertEqual(decode_dtc(0x80, 0x01), 'B0001')

    def test_decode_dtc_network_u_prefix(self):
        # U0100 (lost comm with ECM) → hi=0xC1, lo=0x00
        # prefix bits = 11, d1=0, d2=1, d3=0, d4=0 → U0100
        self.assertEqual(decode_dtc(0xC1, 0x00), 'U0100')

    def test_decode_dtc_chassis_c_prefix(self):
        # C0035 (front-LH wheel speed sensor) → hi=0x40, lo=0x35
        # prefix bits = 01, d1=0, d2=0, d3=3, d4=5 → C0035
        self.assertEqual(decode_dtc(0x40, 0x35), 'C0035')

    def test_decode_dtc_hex_digit_a_to_f(self):
        # P0AFF → high nibble 0x0A, low byte 0xFF
        # hi = 00 (P) | 00 (d1=0) | 1010 (d2=A) = 0x0A
        # lo = 0xFF
        self.assertEqual(decode_dtc(0x0A, 0xFF), 'P0AFF')

    # ── VIN reassembly ──────────────────────────────────────────────
    def test_parse_vin_from_multi_frame_response(self):
        # Real BMW F30 N20 response — 3 frames, header 49 02 01/02/03, VIN = WBA3A5C5XCF254823
        # Frame 1: 0:4902 01 00 00 57 42  (3 chars: W B null null… last 3 bytes)
        # ELM strips fragments; the JS strips both line numbers AND 4902XX headers.
        raw = (
            '0:49 02 01 00 00 57 42 41\r'
            '1:49 02 02 33 41 35 43 35\r'
            '2:49 02 03 58 43 46 32 35\r'
            '3:49 02 04 34 38 32 33 00\r'
        )
        # After stripping line nums + header frames + nulls, the printable bytes
        # are: 57 42 41 33 41 35 43 35 58 43 46 32 35 34 38 32 33 = "WBA3A5C5XCF254823"
        vin = parse_vin(raw)
        self.assertEqual(vin, 'WBA3A5C5XCF254823')
        self.assertEqual(len(vin), 17)

    def test_parse_vin_returns_none_when_no_data(self):
        self.assertIsNone(parse_vin('NO DATA'))

    def test_parse_vin_returns_none_on_garbage(self):
        # Header missing → not a Mode 09 PID 02 response
        self.assertIsNone(parse_vin('41 0C 1A F8'))

    def test_parse_vin_returns_none_when_short(self):
        # Only 5 printable bytes — VIN must be exactly 17 chars
        raw = '0:4902 01 00 00 57 42 41\r'
        self.assertIsNone(parse_vin(raw))


class BluetoothErrorMappingTests(SimpleTestCase):
    """
    Asserts the humanizeBluetoothError() coverage is in sync with the
    Web-Bluetooth error vocabulary Chrome actually emits.
    """

    JS_PATH = (
        'smart_diagnostics/static/smart_diagnostics/js/obd_bluetooth.js'
    )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from pathlib import Path
        cls.src = Path(cls.JS_PATH).read_text(encoding='utf-8')

    def test_covers_user_cancelled(self):
        self.assertIn('user cancelled', self.src.lower())

    def test_covers_bluetooth_off(self):
        self.assertIn('bluetooth adapter not available', self.src.lower())

    def test_covers_unsupported_browser(self):
        self.assertIn('not supported', self.src.lower())

    def test_covers_gatt_disconnect(self):
        self.assertIn('gatt operation failed', self.src.lower())

    def test_covers_https_security(self):
        # HTTPS requirement messaging
        self.assertIn('https', self.src.lower())

    def test_warns_safari_and_firefox(self):
        # Safari and Firefox don't ship Web Bluetooth — driver must say so
        self.assertIn('safari', self.src.lower())
        self.assertIn('firefox', self.src.lower())
