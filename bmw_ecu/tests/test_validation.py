from __future__ import annotations

import unittest

from bmw_ecu.validation import PayloadValidator
from bmw_ecu.validation.payload_validator import FlashRegion


class ValidatorTests(unittest.TestCase):
    def test_unknown_ecu_warns_but_passes(self) -> None:
        v = PayloadValidator(regions={})
        r = v.validate(ecu_name="UNKNOWN", payload=b"\xff" * 16, target_addr=0x800000)
        self.assertTrue(r.ok)
        self.assertTrue(r.warnings)

    def test_out_of_range_fails(self) -> None:
        regions = {"DME_N20": FlashRegion("DME_N20", 0x800000, 0x80FFFF)}
        v = PayloadValidator(regions)
        r = v.validate(ecu_name="DME_N20", payload=b"\xff" * 16, target_addr=0x900000)
        self.assertFalse(r.ok)

    def test_checksum_mismatch_fails(self) -> None:
        import zlib
        payload = b"hello world"
        regions = {"DME_N20": FlashRegion(
            "DME_N20", 0, 0xFFFFFF, expected_crc32=zlib.crc32(payload) ^ 1,
        )}
        v = PayloadValidator(regions)
        r = v.validate(ecu_name="DME_N20", payload=payload, target_addr=0)
        self.assertFalse(r.ok)
