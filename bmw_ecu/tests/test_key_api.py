"""Key-Programming: API endpoint (`key_step`) over the BenchOrchestrator.

All hardware-free, in SIMULATOR mode. These prove the bench key-programming
flow is wired into HTTP with a *persistent* session that survives across
stateless POSTs, for both read paths:

  • CAS3  → EEPROM flow (the sim preloads a valid dump).
  • FEM   → UDS flow   (the sim injects a demo ISN so EXTRACT_ISN works).

plus the used-key path (server virginizes the fob before the burn) and the
honest hardware-missing response when the simulator is OFF.
"""
from __future__ import annotations

import os
import unittest

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from bmw_ecu.api.key_views import key_step
from bmw_ecu.key_learning.session import KeyLearningSessionStore


class _User:
    is_authenticated = True
    is_active = True
    is_staff = False
    pk = 1
    id = 1


_LOCMEM = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "key-session-tests"}
}


@override_settings(CACHES=_LOCMEM)
class _ApiBase(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = APIRequestFactory()
        self._prev_sim = os.environ.get("BMW_ECU_SIMULATOR")
        os.environ["BMW_ECU_SIMULATOR"] = "1"
        cache.clear()

    def tearDown(self) -> None:
        if self._prev_sim is None:
            os.environ.pop("BMW_ECU_SIMULATOR", None)
        else:
            os.environ["BMW_ECU_SIMULATOR"] = self._prev_sim
        cache.clear()

    def _post(self, payload: dict, expect: int = 200) -> dict:
        request = self.factory.post("/api/ecu/key/step", payload, format="json")
        force_authenticate(request, user=_User())
        response = key_step(request)
        self.assertEqual(response.status_code, expect,
                         getattr(response, "data", None))
        return response.data

    def _drive_to_slot(self, *, family: str, used_key: bool, vin: str) -> str:
        out = self._post({"event": "select_profile", "vin": vin,
                          "family": family, "used_key": used_key})
        sid = out["session_id"]
        self.assertEqual(out["prompt"]["expects"], "CONFIRM_WIRING")
        self._post({"event": "confirm_wiring", "session_id": sid})
        self._post({"event": "power_on", "session_id": sid})
        self._post({"event": "enter_bench", "session_id": sid})
        self._post({"event": "dump_now", "session_id": sid})
        out = self._post({"event": "extract_isn", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "isn_extracted")
        out = self._post({"event": "pick_key_slot", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "key_slot_picked")
        return sid


class KeyCas3FlowTests(_ApiBase):
    def test_full_eeprom_flow_persists_to_done(self) -> None:
        sid = self._drive_to_slot(family="CAS3", used_key=False,
                                  vin="WMWKEY000000001")
        out = self._post({"event": "burn_key", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "key_burned")
        self.assertTrue(out["prompt"]["payload"]["fcc_id"])
        self.assertFalse(out["prompt"]["payload"]["virginized_used_key"])

        self._post({"event": "verify", "session_id": sid})
        out = self._post({"event": "finish", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "done")
        self.assertTrue(out["prompt"]["is_terminal"])
        self.assertEqual(out["prompt"]["progress_pct"], 100)
        # Session freed on completion.
        self.assertIsNone(KeyLearningSessionStore().load(sid))

    def test_first_prompt_carries_pinout_callouts(self) -> None:
        out = self._post({"event": "select_profile", "vin": "WMWKEY000000002",
                          "family": "CAS3"})
        labels = {row["label"] for row in out["prompt"]["pin_callouts"]}
        self.assertIn("SDA", labels)
        self.assertIn("SCL", labels)

    def test_event_without_session_is_rejected(self) -> None:
        self._post({"event": "confirm_wiring"}, expect=409)


class KeyUsedKeyFlowTests(_ApiBase):
    def test_used_key_is_virginized_before_burn(self) -> None:
        sid = self._drive_to_slot(family="CAS3", used_key=True,
                                  vin="WMWUSED00000001")
        out = self._post({"event": "burn_key", "session_id": sid})
        self.assertTrue(out["prompt"]["payload"]["virginized_used_key"])
        self.assertEqual(out["prompt"]["state"], "key_burned")

    def test_used_key_flag_survives_session_roundtrip(self) -> None:
        out = self._post({"event": "select_profile", "vin": "WMWUSED00000002",
                          "family": "CAS3", "used_key": True})
        sid = out["session_id"]
        rec = KeyLearningSessionStore().load(sid)
        self.assertIsNotNone(rec)
        self.assertTrue(rec.used_key)
        self.assertTrue(rec.snapshot["data"]["used_key"])


class KeyFemUdsFlowTests(_ApiBase):
    def test_uds_flow_injects_sim_isn_and_completes(self) -> None:
        # FEM has no EEPROM blob; the endpoint injects a demo ISN in sim so the
        # tech can still walk the whole click-path.
        sid = self._drive_to_slot(family="FEM", used_key=False,
                                  vin="WMWFEM000000001")
        out = self._post({"event": "burn_key", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "key_burned")
        self._post({"event": "verify", "session_id": sid})
        out = self._post({"event": "finish", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "done")


class KeyHardwareLockTests(SimpleTestCase):
    """Simulator OFF → honest 503, never a silently faked key."""

    @override_settings(CACHES=_LOCMEM)
    def test_no_simulator_returns_503(self) -> None:
        prev = os.environ.get("BMW_ECU_SIMULATOR")
        os.environ.pop("BMW_ECU_SIMULATOR", None)
        cache.clear()
        try:
            factory = APIRequestFactory()
            request = factory.post(
                "/api/ecu/key/step",
                {"event": "select_profile", "family": "CAS3", "vin": "X"},
                format="json")
            force_authenticate(request, user=_User())
            response = key_step(request)
            self.assertEqual(response.status_code, 503, response.data)
            self.assertEqual(response.data["error"], "hardware_not_found")
        finally:
            if prev is not None:
                os.environ["BMW_ECU_SIMULATOR"] = prev
            cache.clear()


class KeyUrlTests(SimpleTestCase):
    def test_key_step_route_resolves(self) -> None:
        from django.urls import reverse
        self.assertEqual(reverse("bmw_ecu:bmw_ecu_api:key_step"),
                         "/api/ecu/key/step")


if __name__ == "__main__":
    unittest.main()
