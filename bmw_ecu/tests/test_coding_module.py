"""VO parser + FDL features + mutate_byte primitives.

No Django ORM, no transport. The Django-touching paths (entitlement
hold persistence, coding orchestrator end-to-end) are exercised by
the integration tests that boot the test tenant.
"""
from __future__ import annotations

import unittest

from bmw_ecu.coding.fdl_features import (
    CATALOG, FdlCategory, FdlFeature, get as get_feature, list_features,
    mutate_byte,
)
from bmw_ecu.coding.vo_parser import (
    options_diff, options_in_common, parse_vo_hex, parse_vo_xml,
)
from bmw_ecu.exceptions import CodingError


class VoXmlParserTests(unittest.TestCase):
    def test_parses_canonical_xml(self) -> None:
        xml = (
            b"<vehicleOrder>"
            b"<typeKey>3F30</typeKey>"
            b"<productionDate>0414</productionDate>"
            b"<salapaList>"
            b'<salapa code="205"/>'
            b'<salapa code="S322A"/>'
            b'<salapa code="488"/>'
            b"</salapaList>"
            b"</vehicleOrder>"
        )
        vo = parse_vo_xml(xml)
        self.assertEqual(vo.type_code, "3F30")
        self.assertEqual(vo.e_word, "0414")
        self.assertIn("S205", vo.options)
        self.assertIn("S322A", vo.options)
        self.assertIn("S488", vo.options)

    def test_parses_text_form_options(self) -> None:
        xml = (b"<vehicleOrder><salapa>S216</salapa>"
               b"<option>S2VB</option></vehicleOrder>")
        vo = parse_vo_xml(xml)
        self.assertEqual(vo.options, {"S216", "S2VB"})

    def test_malformed_xml_raises(self) -> None:
        with self.assertRaises(CodingError):
            parse_vo_xml(b"<not really xml")

    def test_empty_options_raises(self) -> None:
        with self.assertRaises(CodingError):
            parse_vo_xml(b"<vehicleOrder><typeKey>3F30</typeKey></vehicleOrder>")


class VoHexParserTests(unittest.TestCase):
    def test_parses_compact_blob(self) -> None:
        # type=3F30, date=04/14, 2 tokens: S205, P322
        blob = (b"3F30" + bytes([0x04, 0x14]) + (2).to_bytes(2, "big")
                + (0x0000 | 0x0205).to_bytes(2, "big")
                + (0x1000 | 0x0322).to_bytes(2, "big"))
        vo = parse_vo_hex(blob)
        self.assertEqual(vo.type_code, "3F30")
        self.assertEqual(vo.e_word, "0414")
        self.assertIn("S205", vo.options)
        self.assertIn("P322", vo.options)

    def test_blob_too_short_raises(self) -> None:
        with self.assertRaises(CodingError):
            parse_vo_hex(b"X")


class VoDiffTests(unittest.TestCase):
    def test_diff(self) -> None:
        donor = parse_vo_xml(
            b'<v><salapa code="205"/><salapa code="216"/></v>')
        target = parse_vo_xml(
            b'<v><salapa code="205"/><salapa code="322"/></v>')
        added, removed = options_diff(donor, target)
        self.assertEqual(added, {"S322"})
        self.assertEqual(removed, {"S216"})
        self.assertEqual(options_in_common(donor, target), {"S205"})


class MutateByteTests(unittest.TestCase):
    def test_flips_only_masked_bits(self) -> None:
        # Original byte 0xA5 (1010 0101). Mask 0x0F, set to 0x03.
        # Result expected: 1010 0011 = 0xA3.
        original = bytes([0xFF, 0xA5, 0x00])
        new = mutate_byte(original, offset=1, bit_mask=0x0F, new_masked_value=0x03)
        self.assertEqual(new, bytes([0xFF, 0xA3, 0x00]))

    def test_value_outside_mask_rejected(self) -> None:
        with self.assertRaises(ValueError):
            mutate_byte(b"\x00", offset=0, bit_mask=0x0F, new_masked_value=0x10)

    def test_offset_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            mutate_byte(b"\x00", offset=5, bit_mask=0xFF, new_masked_value=0x00)


class FdlCatalogTests(unittest.TestCase):
    def test_catalog_has_known_features(self) -> None:
        for fid in ("folding_mirrors_via_fob", "m_sport_kombi_layout",
                    "seatbelt_chime_off", "digital_speed_in_hud",
                    "drl_as_indicator_off"):
            self.assertIn(fid, CATALOG)

    def test_safety_features_carry_warning_notes(self) -> None:
        f = get_feature("seatbelt_chime_off")
        self.assertEqual(f.category, FdlCategory.SAFETY_DISABLE)
        self.assertTrue(f.notes)

    def test_list_filter_by_chassis(self) -> None:
        f30 = list_features(chassis="F30")
        self.assertTrue(all("F30" in f.applicable_chassis for f in f30))
        f30_ids = {f.id for f in f30}
        self.assertIn("folding_mirrors_via_fob", f30_ids)

    def test_list_filter_by_category(self) -> None:
        lighting = list_features(category=FdlCategory.LIGHTING)
        self.assertTrue(all(f.category == FdlCategory.LIGHTING for f in lighting))

    def test_unknown_feature_raises(self) -> None:
        with self.assertRaises(CodingError):
            get_feature("not_a_real_feature")
