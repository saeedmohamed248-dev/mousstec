"""Persistent FA catalog loader/saver — pure Python (no Django).

Proves the workshop's verified values round-trip: loaded into the live
registries, and saved back to JSON so they survive a restart. Nothing is
seeded implicitly — an empty/missing catalog registers nothing.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bmw_ecu.coding.fa_engine import TYPE_CODE_ENGINE
from bmw_ecu.coding.fa_transform import (
    clear_fa_engine_transforms,
    plan_from_raw,
    UnverifiedFaTransform,
)
from bmw_ecu.coding.fa_catalog import (
    load_fa_catalog_from_dict,
    load_fa_catalog_from_file,
    save_engine_transform,
)


class FaCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_fa_engine_transforms()
        TYPE_CODE_ENGINE.clear()

    def tearDown(self) -> None:
        clear_fa_engine_transforms()
        TYPE_CODE_ENGINE.clear()

    def test_load_from_dict_registers_type_and_transform(self) -> None:
        n = load_fa_catalog_from_dict({
            "type_code_engine": {"3F30": "N14"},
            "engine_transforms": [
                {"from": "N14", "to": "N18", "new_type_code": "3R18",
                 "add_options": ["S1CBA"], "remove_options": ["S205A"]},
            ],
        })
        self.assertEqual(n, 2)
        plan = plan_from_raw("3F30-S205A-S322A", to_engine="N18")
        self.assertEqual(plan.new_type_code, "3R18")
        self.assertIn("S1CBA", plan.added_options)

    def test_missing_file_is_noop(self) -> None:
        self.assertEqual(load_fa_catalog_from_file(Path("/no/such/catalog.json")), 0)

    def test_malformed_transform_entry_skipped(self) -> None:
        # Missing new_type_code → that entry is skipped, others still load.
        n = load_fa_catalog_from_dict({
            "engine_transforms": [
                {"from": "N14", "to": "N18"},                       # bad
                {"from": "N20", "to": "N26", "new_type_code": "3X"},  # good
            ],
        })
        self.assertEqual(n, 1)

    def test_save_then_load_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "fa_catalog.json"
            save_engine_transform(
                path, from_engine="N14", to_engine="N18",
                new_type_code="3R18", from_type_code="3F30",
                add_options=["S1CBA"], remove_options=["S205A"],
                note="verified")
            # On-disk shape is well-formed.
            data = json.loads(path.read_text())
            self.assertEqual(data["type_code_engine"]["3F30"], "N14")
            self.assertEqual(data["type_code_engine"]["3R18"], "N18")
            self.assertEqual(len(data["engine_transforms"]), 1)

            # A fresh registry loads it and the transform works.
            clear_fa_engine_transforms()
            TYPE_CODE_ENGINE.clear()
            self.assertGreaterEqual(load_fa_catalog_from_file(path), 1)
            plan = plan_from_raw("3F30-S205A", to_engine="N18")
            self.assertEqual(plan.new_type_code, "3R18")

    def test_save_rejects_empty_type_code(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "fa_catalog.json"
            with self.assertRaises(ValueError):
                save_engine_transform(path, from_engine="N14", to_engine="N18",
                                      new_type_code="")
            self.assertFalse(path.exists())   # nothing written on rejection

    def test_unregistered_still_refuses_after_empty_load(self) -> None:
        load_fa_catalog_from_dict({})
        with self.assertRaises(UnverifiedFaTransform):
            plan_from_raw("3F30-N14-S205A", to_engine="N18")


if __name__ == "__main__":
    unittest.main()
