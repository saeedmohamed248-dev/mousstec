"""FA engine-change planner — pure Python (no Django, no hardware).

Proves the planner NEVER writes a fabricated FA:
  • an unregistered engine change raises UnverifiedFaTransform;
  • a registered, verified transform produces the corrected FA (new type
    code + option deltas), preserving the E-word;
  • an unknown source engine (no token, no catalog) refuses.
"""
from __future__ import annotations

import unittest

from bmw_ecu.coding.fa_engine import register_type_engine, TYPE_CODE_ENGINE
from bmw_ecu.coding.fa_transform import (
    UnverifiedFaTransform,
    clear_fa_engine_transforms,
    plan_from_raw,
    register_fa_engine_transform,
)


class FaTransformTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_fa_engine_transforms()
        TYPE_CODE_ENGINE.clear()

    def test_unregistered_change_refuses(self) -> None:
        with self.assertRaises(UnverifiedFaTransform):
            plan_from_raw("3F30-N14-S205A", to_engine="N18")

    def test_registered_transform_produces_corrected_fa(self) -> None:
        register_fa_engine_transform(
            "N14", "N18", new_type_code="3R18",
            add_options=("S1CBA",), remove_options=("S205A",),
            note="verified R56 N14→N18")
        plan = plan_from_raw("3F30-N14-S205A-S322A", to_engine="N18")
        self.assertEqual(plan.from_engine, "N14")
        self.assertEqual(plan.to_engine, "N18")
        self.assertEqual(plan.new_type_code, "3R18")
        # New type code is in the rebuilt FA, old one is gone.
        self.assertIn("3R18", plan.new_raw)
        self.assertNotIn("3F30", plan.new_raw)
        # Option deltas applied.
        self.assertIn("S1CBA", plan.added_options)
        self.assertIn("S205A", plan.removed_options)
        self.assertIn("S1CBA", plan.new_raw)
        self.assertNotIn("S205A", plan.new_raw)
        # Unrelated option preserved.
        self.assertIn("S322A", plan.new_raw)

    def test_source_engine_unknown_refuses(self) -> None:
        # No engine token in the FA and no type_code catalog entry → can't
        # know the source engine → refuse (never assume N14).
        register_fa_engine_transform("N14", "N18", new_type_code="3R18")
        with self.assertRaises(UnverifiedFaTransform):
            plan_from_raw("3F30-S205A-S322A", to_engine="N18")

    def test_type_code_catalog_feeds_source_engine(self) -> None:
        register_type_engine("3F30", "N14")     # verified type→engine
        register_fa_engine_transform("N14", "N18", new_type_code="3R18")
        plan = plan_from_raw("3F30-S205A", to_engine="N18")
        self.assertEqual(plan.from_engine, "N14")
        self.assertEqual(plan.new_type_code, "3R18")

    def test_already_target_engine_refuses(self) -> None:
        with self.assertRaises(UnverifiedFaTransform):
            plan_from_raw("3R18-N18-S205A", to_engine="N18")


if __name__ == "__main__":
    unittest.main()
