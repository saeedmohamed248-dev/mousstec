"""FA engine-code extraction — pure Python (no Django, no hardware).

Proves the extractor NEVER guesses: it returns an engine only from a
verified type_code catalog or an explicit token in the FA text, and ""
(unknown) otherwise — so the diagnosis can't raise a false engine mismatch.
"""
from __future__ import annotations

import unittest

from bmw_ecu.coding.fa_vo import parse_fa
from bmw_ecu.coding.fa_engine import (
    engine_from_vo,
    register_type_engine,
    TYPE_CODE_ENGINE,
)


class EngineFromVoTests(unittest.TestCase):
    def tearDown(self) -> None:
        TYPE_CODE_ENGINE.clear()

    def test_explicit_engine_token_in_fa_is_read(self) -> None:
        vo = parse_fa("3F30-N18-S1CBA-S205A")
        self.assertEqual(engine_from_vo(vo), "N18")

    def test_salapa_codes_never_false_match_as_engine(self) -> None:
        # No engine token present → must be "", not a SALAPA misread.
        vo = parse_fa("3F30-S322A-S205A-S1CBA")
        self.assertEqual(engine_from_vo(vo), "")

    def test_registered_type_code_wins(self) -> None:
        register_type_engine("XR12", "N14")
        vo = parse_fa("XR12-S205A")
        self.assertEqual(engine_from_vo(vo), "N14")

    def test_registered_type_code_beats_stray_token(self) -> None:
        # Catalog is authoritative over an incidental token match.
        register_type_engine("XR99", "N18")
        vo = parse_fa("XR99-N14-S205A")
        self.assertEqual(engine_from_vo(vo), "N18")

    def test_unknown_returns_empty(self) -> None:
        vo = parse_fa("ABCD-S205A")
        self.assertEqual(engine_from_vo(vo), "")


if __name__ == "__main__":
    unittest.main()
