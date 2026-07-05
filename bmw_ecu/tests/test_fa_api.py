"""FA engine-change endpoints (`fa_plan` / `fa_write`).

Hardware-free. Proves the catalog gate and the write guards hold at the
HTTP layer:
  • plan/write refuse an unregistered transform with 409;
  • write refuses without confirm (412);
  • with a registered transform + confirm, simulator returns a labelled
    dry-run of the write (no hardware);
  • missing fields → 400.
"""
from __future__ import annotations

import os
import unittest

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from bmw_ecu.api.fa_views import fa_plan, fa_write
from bmw_ecu.coding.fa_transform import (
    clear_fa_engine_transforms,
    register_fa_engine_transform,
)


class _User:
    is_authenticated = True
    is_active = True
    is_staff = False
    pk = 1
    id = 1


class _Base(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = APIRequestFactory()
        clear_fa_engine_transforms()

    def tearDown(self) -> None:
        clear_fa_engine_transforms()

    def _post(self, view, path, payload, expect=200):
        request = self.factory.post(path, payload, format="json")
        force_authenticate(request, user=_User())
        response = view(request)
        self.assertEqual(response.status_code, expect, getattr(response, "data", None))
        return response.data


class FaPlanTests(_Base):
    def test_unregistered_transform_409(self) -> None:
        out = self._post(fa_plan, "/api/ecu/fa/plan",
                         {"fa_raw": "3F30-N14-S205A", "to_engine": "N18"},
                         expect=409)
        self.assertEqual(out["error"], "unverified_transform")

    def test_registered_transform_returns_plan(self) -> None:
        register_fa_engine_transform("N14", "N18", new_type_code="3R18")
        out = self._post(fa_plan, "/api/ecu/fa/plan",
                         {"fa_raw": "3F30-N14-S205A", "to_engine": "N18"})
        self.assertEqual(out["plan"]["new_type_code"], "3R18")
        self.assertIn("3R18", out["plan"]["new_raw"])

    def test_missing_fields_400(self) -> None:
        self._post(fa_plan, "/api/ecu/fa/plan", {"to_engine": "N18"}, expect=400)
        self._post(fa_plan, "/api/ecu/fa/plan", {"fa_raw": "3F30-N14"}, expect=400)


class FaWriteTests(_Base):
    def test_write_without_confirm_412(self) -> None:
        register_fa_engine_transform("N14", "N18", new_type_code="3R18")
        out = self._post(fa_write, "/api/ecu/fa/write",
                         {"fa_raw": "3F30-N14-S205A", "to_engine": "N18"},
                         expect=412)
        self.assertEqual(out["error"], "confirm_required")
        self.assertIn("plan", out)      # plan returned so the UI can show it

    def test_write_unregistered_transform_409_even_with_confirm(self) -> None:
        self._post(fa_write, "/api/ecu/fa/write",
                   {"fa_raw": "3F30-N14-S205A", "to_engine": "N18",
                    "confirm": True}, expect=409)

    def test_simulator_write_is_labelled_dry_run(self) -> None:
        register_fa_engine_transform("N14", "N18", new_type_code="3R18")
        prev = os.environ.get("BMW_ECU_SIMULATOR")
        os.environ["BMW_ECU_SIMULATOR"] = "1"
        try:
            out = self._post(fa_write, "/api/ecu/fa/write",
                             {"fa_raw": "3F30-N14-S205A", "to_engine": "N18",
                              "confirm": True})
            self.assertTrue(out["simulated"])
            self.assertTrue(out["written"])
            self.assertTrue(out["verified"])
            self.assertEqual(out["plan"]["new_type_code"], "3R18")
        finally:
            if prev is None:
                os.environ.pop("BMW_ECU_SIMULATOR", None)
            else:
                os.environ["BMW_ECU_SIMULATOR"] = prev


class FaUrlTests(SimpleTestCase):
    def test_routes_resolve(self) -> None:
        from django.urls import reverse
        self.assertEqual(reverse("bmw_ecu:bmw_ecu_api:fa_plan"),
                         "/api/ecu/fa/plan")
        self.assertEqual(reverse("bmw_ecu:bmw_ecu_api:fa_write"),
                         "/api/ecu/fa/write")


if __name__ == "__main__":
    unittest.main()
