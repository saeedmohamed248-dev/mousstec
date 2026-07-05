"""Engine-swap Auto-Diagnose endpoint (`swap_diagnose`).

Hardware-free. Proves the single-shot diagnosis is wired into HTTP:
  • simulator ON, no facts → demo swap scenario (ISN mismatch + FA N14→N18);
  • caller-supplied facts are diagnosed verbatim (matching ISN → OK);
  • simulator OFF, no facts → honest 503 (never fabricates against a car);
  • unknown profile → 400.
"""
from __future__ import annotations

import os
import unittest

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from bmw_ecu.api.diagnose_views import swap_diagnose


class _User:
    is_authenticated = True
    is_active = True
    is_staff = False
    pk = 1
    id = 1


class _Base(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = APIRequestFactory()

    def _post(self, payload: dict, expect: int = 200) -> dict:
        request = self.factory.post("/api/ecu/diagnose/swap", payload, format="json")
        force_authenticate(request, user=_User())
        response = swap_diagnose(request)
        self.assertEqual(response.status_code, expect, getattr(response, "data", None))
        return response.data


class SimulatorDiagnoseTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self._prev = os.environ.get("BMW_ECU_SIMULATOR")
        os.environ["BMW_ECU_SIMULATOR"] = "1"

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("BMW_ECU_SIMULATOR", None)
        else:
            os.environ["BMW_ECU_SIMULATOR"] = self._prev

    def test_demo_scenario_flags_isn_mismatch_bench_and_fa_update(self) -> None:
        out = self._post({"profile_name": "MEVD17_2_2_N18"})
        self.assertTrue(out["simulated"])
        d = out["diagnosis"]
        self.assertTrue(d["isn_mismatch"])
        self.assertFalse(d["will_start"])
        self.assertEqual(d["actual_engine"], "N18")
        self.assertEqual(d["fa_engine"], "N14")
        by_key = {a["key"]: a for a in d["actions"]}
        # ISN alignment is bench-only, bot can't drive it over the cable.
        self.assertEqual(by_key["align_isn"]["where"], "bench")
        self.assertFalse(by_key["align_isn"]["bot_can_do"])
        # FA update is OBD and the bot can do it.
        self.assertEqual(by_key["update_fa"]["where"], "obd")
        self.assertTrue(by_key["update_fa"]["bot_can_do"])

    def test_supplied_matching_facts_diagnose_verbatim(self) -> None:
        isn = bytes(range(0x10, 0x30)).hex()
        out = self._post({
            "profile_name": "MEVD17_2_2_N18",
            "cas_isn_hex": isn, "dme_isn_hex": isn, "fa_engine": "N18",
        })
        self.assertFalse(out["simulated"])
        d = out["diagnosis"]
        self.assertFalse(d["isn_mismatch"])
        self.assertTrue(d["will_start"])
        self.assertEqual(d["actions"], [])

    def test_unknown_profile_400(self) -> None:
        self._post({"profile_name": "NOPE_9000"}, expect=400)


class HardwareLockDiagnoseTests(_Base):
    def test_no_simulator_no_facts_returns_503(self) -> None:
        prev = os.environ.get("BMW_ECU_SIMULATOR")
        os.environ.pop("BMW_ECU_SIMULATOR", None)
        try:
            out = self._post({"profile_name": "MEVD17_2_2_N18"}, expect=503)
            self.assertEqual(out["error"], "read_layer_unavailable")
        finally:
            if prev is not None:
                os.environ["BMW_ECU_SIMULATOR"] = prev

    def test_no_simulator_but_facts_supplied_still_works(self) -> None:
        prev = os.environ.get("BMW_ECU_SIMULATOR")
        os.environ.pop("BMW_ECU_SIMULATOR", None)
        try:
            cas = bytes(range(0x10, 0x30)).hex()
            dme = bytes(range(0x40, 0x60)).hex()
            out = self._post({
                "profile_name": "MEVD17_2_2_N18",
                "cas_isn_hex": cas, "dme_isn_hex": dme, "fa_engine": "N14",
            })
            self.assertFalse(out["simulated"])
            self.assertTrue(out["diagnosis"]["isn_mismatch"])
        finally:
            if prev is not None:
                os.environ["BMW_ECU_SIMULATOR"] = prev


class DiagnoseUrlTests(SimpleTestCase):
    def test_route_resolves(self) -> None:
        from django.urls import reverse
        self.assertEqual(reverse("bmw_ecu:bmw_ecu_api:swap_diagnose"),
                         "/api/ecu/diagnose/swap")


if __name__ == "__main__":
    unittest.main()
