"""Used-DME swap: API endpoint (`swap_step`) over the DmeSwapOrchestrator.

All hardware-free, in SIMULATOR mode. These prove the orchestrator is actually
wired into HTTP with a *persistent* session that survives across stateless
POSTs — including the whole point of this feature: the paused DME_BSL_FALLBACK
wizard must round-trip through the session store so the technician can walk
away from the bench mid-job and resume click-by-click.

  • SwapHappyPathTests   — UDS write succeeds → verify → align → done.
  • SwapBslFallbackTests  — UDS write rejected (NRC) → BSL wizard → the three
    Phase-2 outcomes (success / handshake-retry / not-configured).
  • SwapBuildTests        — `_build_provider` live wiring (transport mocked).
  • SwapUrlTests          — the route resolves.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from bmw_ecu.api.swap_views import swap_step
from bmw_ecu.isn import RealDmeSwapProvider, SwapSessionStore


def _run(coro):
    return asyncio.run(coro)


class _User:
    is_authenticated = True
    is_active = True
    is_staff = False
    pk = 1
    id = 1


_LOCMEM = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "swap-session-tests"}
}


@override_settings(CACHES=_LOCMEM)
class _ApiBase(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = APIRequestFactory()
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_env = {
            "BMW_ECU_SIMULATOR": os.environ.get("BMW_ECU_SIMULATOR"),
            "BMW_ECU_BACKUP_ROOT": os.environ.get("BMW_ECU_BACKUP_ROOT"),
        }
        os.environ["BMW_ECU_SIMULATOR"] = "1"
        os.environ["BMW_ECU_BACKUP_ROOT"] = self._tmp.name
        cache.clear()

    def tearDown(self) -> None:
        for k, v in self._prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()
        cache.clear()

    def _post(self, payload: dict, expect: int = 200) -> dict:
        request = self.factory.post("/api/ecu/swap/step", payload, format="json")
        force_authenticate(request, user=_User())
        response = swap_step(request)
        self.assertEqual(response.status_code, expect,
                         getattr(response, "data", None))
        return response.data


class SwapHappyPathTests(_ApiBase):
    def test_uds_write_succeeds_persists_to_done(self) -> None:
        # 1) SELECT_PROFILE (no session_id yet)
        out = self._post({"event": "select_profile", "vin": "WMWHAPPY0000001",
                          "payload": {"profile_key": "R56_N18_MEVD17"}})
        sid = out["session_id"]
        self.assertTrue(sid)
        self.assertEqual(out["prompt"]["state"], "profile_selected")
        self.assertEqual(out["prompt"]["expects"], "READ_CAS_ISN")

        # 2) READ_CAS_ISN — reloaded state from cache
        out = self._post({"event": "read_cas_isn", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "cas_isn_read")
        self.assertEqual(out["prompt"]["expects"], "BACKUP_DME")

        # 3) BACKUP_DME
        out = self._post({"event": "backup_dme", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "dme_backed_up")
        self.assertEqual(out["prompt"]["expects"], "WRITE_DME_ISN")

        # 4) WRITE_DME_ISN — UDS path succeeds (no reject flag)
        out = self._post({"event": "write_dme_isn", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "dme_isn_written")
        self.assertEqual(out["prompt"]["payload"]["path"], "uds")
        self.assertEqual(out["prompt"]["expects"], "VERIFY")

        # 5) VERIFY → 6) ALIGN → 7) FINISH → done
        out = self._post({"event": "verify", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "verified")
        out = self._post({"event": "align", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "aligned")
        out = self._post({"event": "finish", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "done")
        self.assertTrue(out["prompt"]["is_terminal"])

        # Session freed on completion.
        self.assertIsNone(SwapSessionStore().load(sid))

    def test_event_without_session_is_rejected(self) -> None:
        self._post({"event": "read_cas_isn"}, expect=409)


class SwapBslFallbackTests(_ApiBase):
    def _drive_to_fallback(self, sim: dict) -> str:
        """SELECT→READ→BACKUP→WRITE(rejected) → returns session id parked in the
        BSL wizard."""
        out = self._post({"event": "select_profile", "vin": "WMWBSL00000001",
                          "payload": {"profile_key": "R56_N18_MEVD17"},
                          "sim": sim})
        sid = out["session_id"]
        self._post({"event": "read_cas_isn", "session_id": sid})
        self._post({"event": "backup_dme", "session_id": sid})
        out = self._post({"event": "write_dme_isn", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "dme_bsl_fallback")
        self.assertEqual(out["prompt"]["expects"], "BSL_START")
        return sid

    def test_uds_nrc_diverts_to_bsl_wizard_with_full_payload(self) -> None:
        sid = self._drive_to_fallback({"uds_reject_nrc": 0x33})
        # Reload the *persisted* session — the paused wizard must survive.
        rec = SwapSessionStore().load(sid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.snapshot["state"], "dme_bsl_fallback")
        self.assertEqual(rec.snapshot["data"]["uds_reject_nrc"], "0x33")

        # A fresh divert (0x22 this time) to assert the full wizard payload the
        # frontend renders: NRC, steps, hardware spec, and the verified flag.
        out = self._post({"event": "select_profile", "vin": "WMWBSL00000002",
                          "payload": {"profile_key": "R56_N18_MEVD17"},
                          "sim": {"uds_reject_nrc": 0x22}})
        sid2 = out["session_id"]
        self._post({"event": "read_cas_isn", "session_id": sid2})
        self._post({"event": "backup_dme", "session_id": sid2})
        out = self._post({"event": "write_dme_isn", "session_id": sid2})
        pl = out["prompt"]["payload"]
        self.assertEqual(pl["path"], "bsl")
        self.assertEqual(pl["uds_reject_nrc"], "0x22")
        self.assertEqual(pl["dme_family"], "MEVD17")
        # MEVD17 BSL profile ships verified=False (template) → UI shows warning.
        self.assertFalse(pl["hardware_verified"])
        self.assertEqual(len(pl["steps"]), 4)
        self.assertIn("boot_pin", pl["hardware"])
        self.assertIn("serial_pin_map", pl["hardware"])

    def test_bsl_start_success_completes_to_done(self) -> None:
        sid = self._drive_to_fallback({"uds_reject_nrc": 0x33})
        out = self._post({"event": "bsl_start", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "dme_isn_written")
        self.assertEqual(out["prompt"]["payload"]["path"], "bsl")
        # Finish the rest of the pipeline.
        self._post({"event": "verify", "session_id": sid})
        self._post({"event": "align", "session_id": sid})
        out = self._post({"event": "finish", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "done")

    def test_bsl_handshake_fail_stays_paused_with_retry(self) -> None:
        sid = self._drive_to_fallback(
            {"uds_reject_nrc": 0x33, "bsl_handshake_fail": True})
        out = self._post({"event": "bsl_start", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "dme_bsl_fallback")
        self.assertTrue(out["prompt"]["is_error"])
        self.assertTrue(out["prompt"]["payload"]["retry"])
        self.assertEqual(out["prompt"]["expects"], "BSL_START")
        # Session stays alive so the tech can fix wiring and retry.
        self.assertIsNotNone(SwapSessionStore().load(sid))

    def test_bsl_not_configured_stays_paused_asks_for_profile(self) -> None:
        sid = self._drive_to_fallback(
            {"uds_reject_nrc": 0x22, "bsl_not_configured": True})
        out = self._post({"event": "bsl_start", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "dme_bsl_fallback")
        self.assertFalse(out["prompt"]["is_error"])  # not a wiring error
        self.assertTrue(
            out["prompt"]["payload"]["needs_confirmed_flash_profile"])
        self.assertEqual(out["prompt"]["expects"], "BSL_START")


# --- Live-build wiring (transport + config mocked, no hardware) -------------
class _FakeKind:
    def __init__(self, value): self.value = value


class _FakeTransport:
    def __init__(self, value="kline"):
        self.kind = _FakeKind(value)
        self.closed = False

    async def close(self): self.closed = True


class _FakeCM:
    last_prefer = "unset"

    async def connect(self, prefer=None):
        _FakeCM.last_prefer = prefer
        kind = prefer.kind.value if prefer is not None else "kline"
        return _FakeTransport(kind)


class SwapBuildTests(SimpleTestCase):
    """`_build_provider` LIVE path: config + transport mocked, wiring asserted."""

    def setUp(self):
        import bmw_ecu.api.swap_views as sv
        from bmw_ecu.isn import SwapAddressConfig
        self.sv = sv
        self._cm = sv.ConnectionManager
        self._resolve = sv.resolve_seed_key_provider
        self._cfg = sv.swap_address_config_from_env
        sv.ConnectionManager = _FakeCM
        sv.resolve_seed_key_provider = lambda **kw: object()
        # Confirmed addresses would come from BMW_ECU_SWAP_* env; inject directly.
        sv.swap_address_config_from_env = lambda: SwapAddressConfig(
            cas_ecu_addr=0x40, dme_ecu_addr=0x12)
        _FakeCM.last_prefer = "unset"

    def tearDown(self):
        self.sv.ConnectionManager = self._cm
        self.sv.resolve_seed_key_provider = self._resolve
        self.sv.swap_address_config_from_env = self._cfg

    def _record(self, **kw):
        from bmw_ecu.isn import SwapSessionRecord
        base = dict(session_id="s1", profile_key="R56_N18_MEVD17",
                    simulator=False, transport={}, sim={})
        base.update(kw)
        return SwapSessionRecord(**base)

    def test_bare_kind_falls_back_to_autodetect(self):
        rec = self._record(transport={"kind": "kline"})
        provider, cleanup = _run(self.sv._build_provider(rec))
        self.assertIsNone(_FakeCM.last_prefer)          # auto-detect path
        self.assertIsInstance(provider, RealDmeSwapProvider)
        _run(cleanup())

    def test_explicit_serial_port_uses_prefer(self):
        rec = self._record(transport={"kind": "kline",
                                      "serial_port": "/dev/cu.usbserial-A"})
        provider, cleanup = _run(self.sv._build_provider(rec))
        self.assertIsNotNone(_FakeCM.last_prefer)
        self.assertEqual(_FakeCM.last_prefer.serial_port, "/dev/cu.usbserial-A")
        _run(cleanup())

    def test_cleanup_closes_transport(self):
        rec = self._record(transport={"kind": "kline",
                                      "serial_port": "/dev/cu.usbserial-A"})
        provider, cleanup = _run(self.sv._build_provider(rec))
        _run(cleanup())  # closes without raising


class SwapUrlTests(SimpleTestCase):
    def test_swap_step_route_resolves(self):
        from django.urls import reverse
        self.assertEqual(reverse("bmw_ecu:bmw_ecu_api:swap_step"),
                         "/api/ecu/swap/step")


if __name__ == "__main__":
    unittest.main()
