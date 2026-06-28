"""Smart Auto-Detect: API endpoint + RealUniversalEcuIo bridge.

Two layers, both hardware-free:

  • ApiFlowTests drive the real DRF view (`smart_step`) end-to-end in
    SIMULATOR mode — proving the UniversalSmartOrchestrator is actually wired
    into HTTP with a *persistent* session that survives across stateless POSTs
    (the whole point: the chatbot UI clicks one event at a time).

  • RealProviderTests unit-test `RealUniversalEcuIo` against a fake UDS client,
    proving the two guarantees that matter on a live ECU: every wire error is
    translated into a safe `UniversalIoError`, and the proprietary coding
    operations REFUSE rather than fabricate.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from bmw_ecu.api.smart_views import smart_step
from bmw_ecu.universal import RealUniversalEcuIo
from bmw_ecu.universal.provider import UniversalIoError


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
                "LOCATION": "smart-session-tests"}
}


@override_settings(CACHES=_LOCMEM)
class ApiFlowTests(SimpleTestCase):
    """Drive the endpoint exactly like the frontend would: one POST per click."""

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

    def _post(self, payload: dict) -> dict:
        request = self.factory.post("/api/ecu/smart/step", payload, format="json")
        force_authenticate(request, user=_User())
        response = smart_step(request)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.data

    def test_unlocked_happy_path_persists_session_to_done(self) -> None:
        # 1) START — auto-detect (no session_id yet)
        out = self._post({"event": "start", "vin": "WBASMART0000001",
                          "profile_name": "MEVD17_2_2_N18",
                          "sim": {"transport_kind": "doip", "dme_locked": False}})
        sid = out["session_id"]
        self.assertTrue(sid)
        self.assertEqual(out["prompt"]["state"], "detected")
        self.assertEqual(out["prompt"]["expects"], "BACKUP")
        self.assertEqual(out["prompt"]["payload"]["body_module"], "FEM")

        # 2) BACKUP — same session_id, server reloaded state from cache
        out = self._post({"event": "backup", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "backed_up")
        self.assertEqual(out["prompt"]["expects"], "CODE")
        self.assertTrue(out["prompt"]["payload"]["backup_sha256"])

        # 3) CODE
        out = self._post({"event": "code", "session_id": sid,
                          "payload": {"options": {"a": 1, "b": 2}}})
        self.assertEqual(out["prompt"]["state"], "coded")
        self.assertEqual(out["prompt"]["expects"], "SYNC")

        # 4) SYNC → DONE (terminal → session is cleared)
        out = self._post({"event": "sync", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "done")
        self.assertTrue(out["prompt"]["is_terminal"])

        # Session was freed on completion.
        from bmw_ecu.universal import SmartSessionStore
        self.assertIsNone(SmartSessionStore().load(sid))

    def test_rollback_survives_across_requests(self) -> None:
        out = self._post({"event": "start", "vin": "WBAROLL0000002",
                          "profile_name": "MEVD17_2_2_N18",
                          "sim": {"transport_kind": "doip", "dme_locked": False}})
        sid = out["session_id"]
        self._post({"event": "backup", "session_id": sid})

        # Abort after backup → FAILED but Rollback offered, session KEPT alive.
        out = self._post({"event": "abort", "session_id": sid})
        self.assertTrue(out["prompt"]["is_error"])
        self.assertEqual(out["prompt"]["expects"], "ROLLBACK")
        self.assertIn("rollback", [a["event"] for a in out["prompt"]["actions"]])
        from bmw_ecu.universal import SmartSessionStore
        self.assertIsNotNone(SmartSessionStore().load(sid))  # still resumable

        # Click Rollback on a brand-new request — backup bytes reloaded from
        # the content-addressed store, not from memory.
        out = self._post({"event": "rollback", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "rolled_back")
        self.assertTrue(out["prompt"]["is_terminal"])
        self.assertIsNone(SmartSessionStore().load(sid))  # freed after rollback

    def test_locked_kdcan_routes_to_bench_register_when_no_pinout(self) -> None:
        out = self._post({"event": "start", "vin": "WMWLOCK0000003",
                          "profile_name": "MEVD17_2_2_N18",
                          "sim": {"transport_kind": "kdcan", "dme_locked": True}})
        sid = out["session_id"]
        self.assertEqual(out["prompt"]["payload"]["body_module"], "CAS")
        out = self._post({"event": "backup", "session_id": sid})
        self.assertEqual(out["prompt"]["state"], "bench_halted")
        self.assertFalse(out["prompt"]["payload"]["has_pinout"])
        self.assertEqual(out["prompt"]["expects"], "register_board")

    def test_event_without_session_is_rejected(self) -> None:
        request = self.factory.post(
            "/api/ecu/smart/step", {"event": "backup"}, format="json")
        force_authenticate(request, user=_User())
        response = smart_step(request)
        self.assertEqual(response.status_code, 409)


# --- RealUniversalEcuIo (live bridge) unit tests ---------------------------
class _FakeClient:
    """Records UDS calls; scriptable responses / failures."""

    def __init__(self, *, vin=b"WBAFAKE0000000001", seed=b"\x00\x00\x00\x00",
                 coding=b"\xCA\xFD\x01ORIG", raise_on=None) -> None:
        self._vin = vin
        self._seed = seed
        self._coding = coding
        self._raise_on = raise_on
        self.writes: list[tuple[int, bytes]] = []

    def _maybe(self, op):
        if self._raise_on == op:
            raise TimeoutError(f"injected {op}")

    async def read_data_by_identifier(self, did: int) -> bytes:
        if did == 0xF190:
            self._maybe("read_vin")
            return self._vin
        self._maybe("read_coding")
        return self._coding

    async def raw_request(self, payload: bytes, *, timeout: float = 5.0) -> bytes:
        self._maybe("seed")
        # [0x67, level, seed...]
        return bytes([0x67, payload[1]]) + self._seed

    async def write_data_by_identifier(self, did: int, data: bytes) -> bytes:
        self._maybe("write")
        self.writes.append((did, bytes(data)))
        return b"\x6E"


class _FakeSecurity:
    class _P:
        security_level = 0x01
    provider = _P()

    def __init__(self) -> None:
        self.unlocked = 0

    async def unlock(self, *, vin=None, level=None) -> None:
        self.unlocked += 1


class RealProviderTests(unittest.TestCase):
    def _io(self, **kw):
        client = kw.pop("client", None) or _FakeClient()
        sec = kw.pop("security", None) or _FakeSecurity()
        defaults = dict(client=client, security=sec, transport_kind="doip")
        defaults.update(kw)
        return RealUniversalEcuIo(**defaults), client, sec

    def test_detect_transport_reports_connected_kind(self) -> None:
        io, _, _ = self._io(transport_kind="kdcan")
        self.assertEqual(_run(io.detect_transport()), "kdcan")

    def test_read_vin_decodes_did_f190(self) -> None:
        io, _, _ = self._io(client=_FakeClient(vin=b"WBAREAL0000000009\x00"))
        self.assertEqual(_run(io.read_vin()), "WBAREAL0000000009")

    def test_probe_unlocked_on_all_zero_seed(self) -> None:
        io, _, _ = self._io(client=_FakeClient(seed=b"\x00\x00\x00\x00"))
        self.assertFalse(_run(io.probe_dme_locked()))

    def test_probe_locked_on_nonzero_seed(self) -> None:
        io, _, _ = self._io(client=_FakeClient(seed=b"\x12\x34\x56\x78"))
        self.assertTrue(_run(io.probe_dme_locked()))

    def test_probe_treats_wire_error_as_locked(self) -> None:
        io, _, _ = self._io(client=_FakeClient(raise_on="seed"))
        self.assertTrue(_run(io.probe_dme_locked()))  # conservative

    def test_backup_refuses_without_configured_coding_did(self) -> None:
        io, _, _ = self._io(coding_did=None)
        with self.assertRaises(UniversalIoError):
            _run(io.read_coding_snapshot())

    def test_backup_restore_roundtrip_is_symmetric(self) -> None:
        io, client, sec = self._io(coding_did=0xC200,
                                   client=_FakeClient(coding=b"\xCA\xFD\x01ORIG"))
        snap = _run(io.read_coding_snapshot())
        self.assertEqual(snap, b"\xCA\xFD\x01ORIG")
        _run(io.write_coding_snapshot(snap))
        self.assertEqual(client.writes[-1], (0xC200, b"\xCA\xFD\x01ORIG"))
        self.assertEqual(sec.unlocked, 1)  # unlocked before writing

    def test_wire_timeout_becomes_universal_io_error(self) -> None:
        io, _, _ = self._io(coding_did=0xC200,
                            client=_FakeClient(raise_on="read_coding"))
        with self.assertRaises(UniversalIoError):
            _run(io.read_coding_snapshot())

    def test_code_dme_refuses_to_fabricate_without_raw_bytes(self) -> None:
        io, _, _ = self._io(coding_did=0xC200)
        with self.assertRaises(UniversalIoError):
            _run(io.code_dme({"options": {"sport_display": True}}))

    def test_code_dme_writes_exact_caller_supplied_bytes(self) -> None:
        io, client, sec = self._io(coding_did=0xC200)
        _run(io.code_dme({"raw_coding_hex": "cafd0099"}))
        self.assertEqual(client.writes[-1], (0xC200, bytes.fromhex("cafd0099")))
        self.assertEqual(sec.unlocked, 1)

    def test_sync_and_extract_refuse_until_confirmed_routines(self) -> None:
        io, _, _ = self._io(coding_did=0xC200)
        with self.assertRaises(UniversalIoError):
            _run(io.sync_module("FEM"))
        with self.assertRaises(UniversalIoError):
            _run(io.extract_bench())

    def test_bench_pinout_passes_through_catalog_value(self) -> None:
        io, _, _ = self._io(pinout={"boot_pin": 24})
        self.assertEqual(_run(io.bench_pinout())["boot_pin"], 24)
        io2, _, _ = self._io()
        self.assertIsNone(_run(io2.bench_pinout()))


class _FakeKind:
    def __init__(self, value): self.value = value


class _FakeTransport:
    def __init__(self, value="doip"):
        self.kind = _FakeKind(value)
        self.closed = False
    async def close(self): self.closed = True


class _FakeCM:
    """Records how connect() was called so we can assert auto-detect vs prefer."""
    last_prefer = "unset"

    def __init__(self): pass

    async def connect(self, prefer=None):
        _FakeCM.last_prefer = prefer
        kind = prefer.kind.value if prefer is not None else "doip"
        return _FakeTransport(kind)


class RealBuildTests(SimpleTestCase):
    """`_build_io` wiring for the LIVE path (transport mocked, no hardware)."""

    def setUp(self):
        import bmw_ecu.api.smart_views as sv
        self.sv = sv
        self._cm = sv.ConnectionManager
        self._resolve = sv.resolve_seed_key_provider
        sv.ConnectionManager = _FakeCM
        sv.resolve_seed_key_provider = lambda **kw: object()
        _FakeCM.last_prefer = "unset"

    def tearDown(self):
        self.sv.ConnectionManager = self._cm
        self.sv.resolve_seed_key_provider = self._resolve

    def _record(self, **kw):
        from bmw_ecu.universal import SmartSessionRecord
        base = dict(session_id="r1", profile_name="MEVD17_2_2_N18",
                    simulator=False, transport={}, sim={})
        base.update(kw)
        return SmartSessionRecord(**base)

    def test_bare_kind_falls_back_to_autodetect(self):
        # UI sends {"kind":"doip"} with no host → must NOT force a hostless
        # DoIP config (that raises); must auto-detect instead.
        rec = self._record(transport={"kind": "doip"})
        io, cleanup = _run(self.sv._build_io(rec, {}))
        self.assertIsNone(_FakeCM.last_prefer)          # auto-detect path
        self.assertIsInstance(io, RealUniversalEcuIo)
        _run(cleanup())

    def test_explicit_host_uses_prefer(self):
        rec = self._record(transport={"kind": "doip", "host": "169.254.255.0"})
        io, cleanup = _run(self.sv._build_io(rec, {}))
        self.assertIsNotNone(_FakeCM.last_prefer)
        self.assertEqual(_FakeCM.last_prefer.host, "169.254.255.0")
        _run(cleanup())

    def test_coding_did_propagates_to_real_io(self):
        rec = self._record(transport={"kind": "doip"}, sim={"coding_did": 0xC200})
        io, cleanup = _run(self.sv._build_io(rec, {}))
        self.assertEqual(io._coding_did, 0xC200)
        _run(cleanup())

    def test_cleanup_closes_transport(self):
        rec = self._record(transport={"kind": "doip"})
        io, cleanup = _run(self.sv._build_io(rec, {}))
        _run(cleanup())  # should close without raising


class UrlAliasTests(SimpleTestCase):
    def test_smart_detect_alias_resolves_to_same_view(self):
        from django.urls import reverse
        self.assertEqual(reverse("bmw_ecu:bmw_ecu_api:smart_detect"),
                         "/api/ecu/smart-detect/")
        self.assertEqual(reverse("bmw_ecu:bmw_ecu_api:smart_step"),
                         "/api/ecu/smart/step")


if __name__ == "__main__":
    unittest.main()
