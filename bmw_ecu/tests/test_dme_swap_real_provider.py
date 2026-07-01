"""RealDmeSwapProvider tests — honest refusals + a real composed happy path.

Two halves:
  • REFUSALS (no wire I/O): the provider must refuse — never fake — when a
    confirmed CAS ISN spec, a bench harness, the EWS routine ID, or the CAS/DME
    addresses are missing. This is the no-guessed-hardware-data policy in code.
  • COMPOSED HAPPY PATH: with a fake transport answering UDS frames and a mock
    bench, RealDmeSwapProvider satisfies the DmeSwapOrchestrator contract end
    to end (SELECT → READ_CAS_ISN → BACKUP → WRITE → VERIFY → ALIGN → DONE).
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from bmw_ecu.connection.base import TransportConfig, TransportKind
from bmw_ecu.connection.base import AbstractTransport
from bmw_ecu.isn.dme_swap_orchestrator import (
    DmeSwapOrchestrator,
    DmeUdsWriteRejected,
    SwapEvent,
    SwapState,
)
from bmw_ecu.isn.dme_swap_real_provider import (
    RealDmeSwapProvider,
    SwapAddressConfig,
    SwapProviderError,
    swap_address_config_from_env,
)
from bmw_ecu.isn.isn_map import IsnAccessSpec
from bmw_ecu.isn.tricore_bsl import BslHandshakeFailed, BslNotConfigured
from bmw_ecu.safety.backup import BackupStore

PKEY = "R56_N18_MEVD17"
VIN = "WMWXX_TEST_000001"
ISN = bytes(range(1, 33))  # non-virgin, 32 bytes


def _run(coro):
    return asyncio.run(coro)


# ── doubles ──────────────────────────────────────────────────────────────
class _StubSeed:
    """Minimal seed-key provider. The fake ECU returns an all-zero seed (UDS
    'already unlocked'), so compute_key is never reached."""
    security_level = 0x03

    def compute_key(self, seed: bytes, *, vin=None) -> bytes:  # pragma: no cover
        return bytes(len(seed))


class _FakeTransport(AbstractTransport):
    """Answers just the UDS services the swap flow uses."""
    kind = TransportKind.KLINE

    def __init__(self, isn: bytes, *, dme_write_nrc: int | None = None) -> None:
        super().__init__(TransportConfig(kind=TransportKind.KLINE))
        self._isn = isn
        self._dme_write_nrc = dme_write_nrc
        self.requests: list[tuple[int, bytes]] = []

    async def open(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def send(self, target_addr: int, payload: bytes) -> None:  # pragma: no cover
        self._pending = (target_addr, bytes(payload))

    async def recv(self, timeout=None) -> bytes:  # pragma: no cover
        raise NotImplementedError

    async def request(self, target_addr: int, payload: bytes, timeout=None) -> bytes:
        self.requests.append((target_addr, bytes(payload)))
        sid = payload[0]
        if sid == 0x10:                      # DiagnosticSessionControl
            return bytes([0x50, payload[1]])
        if sid == 0x27:                      # SecurityAccess seed → all-zero
            return bytes([0x67, payload[1]]) + bytes(8)
        if sid == 0x22:                      # ReadDataByIdentifier → ISN
            return bytes([0x62, payload[1], payload[2]]) + self._isn
        if sid == 0x2E:                      # WriteDataByIdentifier (DME ISN)
            if self._dme_write_nrc is not None:
                return bytes([0x7F, 0x2E, self._dme_write_nrc])
            return bytes([0x6E, payload[1], payload[2]])
        if sid == 0x31:                      # RoutineControl → success (0x00)
            return bytes([0x71, payload[1], payload[2], payload[3], 0x00])
        return bytes([0x7F, sid, 0x11])      # service not supported


class _MockBench:
    def __init__(self, isn: bytes) -> None:
        self.written = None
        self._flash = b"\xAA" * 64 + isn + b"\xBB" * 64

    async def dump(self) -> bytes:
        return self._flash

    async def write_isn(self, isn: bytes) -> None:
        self.written = bytes(isn)
        self._flash = b"\xAA" * 64 + bytes(isn) + b"\xBB" * 64


class _FakeBslLink:
    """Fake TricoreBslLink: real link isn't opened, but the open/handshake/
    write/close lifecycle is recorded so tests can drive each fallback branch."""

    def __init__(self, *, handshake_fail=False, not_configured=False) -> None:
        self.handshake_fail = handshake_fail
        self.not_configured = not_configured
        self.opened = self.closed = False
        self.written: bytes | None = None

    async def open(self) -> None:
        self.opened = True

    async def handshake(self) -> None:
        if self.handshake_fail:
            raise BslHandshakeFailed("fake: no boot handshake")

    async def write_isn(self, isn: bytes) -> None:
        if self.not_configured:
            raise BslNotConfigured("fake: no confirmed flash profile")
        self.written = bytes(isn)

    async def close(self) -> None:
        self.closed = True


def _verified_cas_spec() -> IsnAccessSpec:
    return IsnAccessSpec(family="CAS", did=0xABCD, security_level=0x03,
                         length=32, over_uds=True, verified=True,
                         notes="test-confirmed")


def _verified_dme_spec() -> IsnAccessSpec:
    return IsnAccessSpec(family="DME", did=0x1234, security_level=0x11,
                         length=32, over_uds=True, verified=True,
                         notes="test-confirmed DME write")


def _make_provider(*, bench=True, cas_spec=None, dme_spec=None, ews_routine=0x1234,
                   allow_unverified=False, store_root=None, dme_write_nrc=None,
                   bsl_port=None, bsl_link=None):
    transport = _FakeTransport(ISN, dme_write_nrc=dme_write_nrc)
    addr = SwapAddressConfig(
        cas_ecu_addr=0x40, dme_ecu_addr=0x12,
        cas_isn_spec=cas_spec, dme_isn_spec=dme_spec, ews_routine_id=ews_routine,
        allow_unverified=allow_unverified, bsl_port=bsl_port)
    store = BackupStore(Path(store_root or tempfile.mkdtemp()))
    factory = (lambda port, profile: bsl_link) if bsl_link is not None else None
    prov = RealDmeSwapProvider(
        transport=transport,
        cas_seed_provider=_StubSeed(), dme_seed_provider=_StubSeed(),
        addr=addr, backup_store=store,
        bench=_MockBench(ISN) if bench else None,
        bsl_link_factory=factory)
    return prov, transport


# ── REFUSALS (no fabrication) ────────────────────────────────────────────
class RefusalTests(unittest.TestCase):
    def test_read_cas_isn_refuses_unverified_spec(self) -> None:
        prov, _ = _make_provider(cas_spec=None, allow_unverified=False)
        with self.assertRaises(SwapProviderError) as cm:
            _run(prov.read_cas_isn(vin=VIN, cas_family="CAS3+"))
        self.assertIn("unverified", str(cm.exception).lower())

    def test_backup_refuses_without_bench(self) -> None:
        prov, _ = _make_provider(bench=False)
        with self.assertRaises(SwapProviderError):
            _run(prov.backup_dme(vin=VIN, dme_name="MEVD17_2_2"))

    def test_write_without_uds_spec_or_bench_diverts_to_bsl(self) -> None:
        # No confirmed DME UDS DID and no bench → do NOT hard-fail; signal the
        # orchestrator to continue into the BSL fallback (nrc=None).
        prov, _ = _make_provider(bench=False, dme_spec=None)
        with self.assertRaises(DmeUdsWriteRejected) as cm:
            _run(prov.write_dme_isn(vin=VIN, dme_name="MEVD17_2_2",
                                    isn=ISN, requires_bench=True))
        self.assertIsNone(cm.exception.nrc)

    def test_align_refuses_without_routine_id(self) -> None:
        prov, _ = _make_provider(ews_routine=None)
        with self.assertRaises(SwapProviderError) as cm:
            _run(prov.align_ews(vin=VIN))
        self.assertIn("0xAF11", str(cm.exception))


# ── env config (addresses are never guessed) ─────────────────────────────
class EnvConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in (
            "BMW_ECU_SWAP_CAS_ADDR", "BMW_ECU_SWAP_DME_ADDR",
            "BMW_ECU_SWAP_CAS_ISN_DID", "BMW_ECU_SWAP_CAS_ISN_LEVEL",
            "BMW_ECU_SWAP_EWS_ROUTINE", "BMW_ECU_SWAP_ALLOW_UNVERIFIED")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_missing_addresses_raise(self) -> None:
        with self.assertRaises(SwapProviderError) as cm:
            swap_address_config_from_env()
        self.assertIn("BMW_ECU_SWAP_CAS_ADDR", str(cm.exception))

    def test_full_env_builds_verified_spec(self) -> None:
        os.environ["BMW_ECU_SWAP_CAS_ADDR"] = "0x40"
        os.environ["BMW_ECU_SWAP_DME_ADDR"] = "0x12"
        os.environ["BMW_ECU_SWAP_CAS_ISN_DID"] = "0xABCD"
        os.environ["BMW_ECU_SWAP_CAS_ISN_LEVEL"] = "0x03"
        os.environ["BMW_ECU_SWAP_EWS_ROUTINE"] = "0x1234"
        cfg = swap_address_config_from_env()
        self.assertEqual(cfg.cas_ecu_addr, 0x40)
        self.assertEqual(cfg.dme_ecu_addr, 0x12)
        self.assertIsNotNone(cfg.cas_isn_spec)
        self.assertTrue(cfg.cas_isn_spec.verified)
        self.assertEqual(cfg.cas_isn_spec.did, 0xABCD)
        self.assertEqual(cfg.ews_routine_id, 0x1234)


# ── COMPOSED HAPPY PATH (real primitives over a fake wire) ───────────────
class ComposedHappyPathTests(unittest.TestCase):
    def test_read_cas_isn_real_over_wire(self) -> None:
        prov, transport = _make_provider(cas_spec=_verified_cas_spec())
        got = _run(prov.read_cas_isn(vin=VIN, cas_family="CAS3+"))
        self.assertEqual(got, ISN)
        # It actually drove the CAS client (0x40) over the wire.
        self.assertTrue(any(t == 0x40 for t, _ in transport.requests))

    def test_verify_true_when_isn_present(self) -> None:
        prov, _ = _make_provider()
        _run(prov.write_dme_isn(vin=VIN, dme_name="MEVD17_2_2",
                                isn=ISN, requires_bench=True))
        ok = _run(prov.verify_dme_isn(vin=VIN, dme_name="MEVD17_2_2", isn=ISN))
        self.assertTrue(ok)

    def test_verify_false_when_isn_absent(self) -> None:
        prov, _ = _make_provider()
        # No write happened; the mock flash contains ISN but a different target
        other = bytes(range(100, 132))
        ok = _run(prov.verify_dme_isn(vin=VIN, dme_name="MEVD17_2_2", isn=other))
        self.assertFalse(ok)

    def test_full_orchestrator_to_done(self) -> None:
        prov, _ = _make_provider(cas_spec=_verified_cas_spec())
        orch = DmeSwapOrchestrator(prov)
        _run(orch.handle(SwapEvent.SELECT_PROFILE, {"profile_key": PKEY,
                                                    "vin": VIN, "gateway": "ZGW"}))
        _run(orch.handle(SwapEvent.READ_CAS_ISN))
        _run(orch.handle(SwapEvent.BACKUP_DME))
        _run(orch.handle(SwapEvent.WRITE_DME_ISN))
        _run(orch.handle(SwapEvent.VERIFY))
        _run(orch.handle(SwapEvent.ALIGN))
        final = _run(orch.handle(SwapEvent.FINISH))
        self.assertEqual(orch.state, SwapState.DONE)
        self.assertTrue(final.is_terminal)
        self.assertFalse(final.is_error)
        self.assertEqual(final.progress_pct, 100)
        # The CAS ISN that flowed through is exactly what the wire returned.
        self.assertEqual(orch.data.cas_isn_hex, ISN.hex().upper())


# ── ADAPTIVE PIPELINE: Phase-1 UDS attempt → BSL fallback ────────────────
class UdsWritePhaseTests(unittest.TestCase):
    """Phase 1: with a confirmed DME ISN write spec the provider actually drives
    the UDS write over the wire, and maps refusal NRCs onto DmeUdsWriteRejected
    so the orchestrator can fall back to BSL — without ever guessing a DID."""

    def test_uds_write_succeeds_with_confirmed_spec(self) -> None:
        prov, transport = _make_provider(dme_spec=_verified_dme_spec(),
                                         bench=False)
        _run(prov.write_dme_isn(vin=VIN, dme_name="MEVD17_2_2",
                                isn=ISN, requires_bench=True))
        # It really issued the WriteDataByIdentifier (0x2E) to the DME (0x12).
        self.assertTrue(any(t == 0x12 and p[0] == 0x2E
                            for t, p in transport.requests))

    def test_uds_security_denied_diverts_to_bsl(self) -> None:
        # The DME answers the WriteDataByIdentifier with NRC 0x33 (Security
        # Access Denied) → mapped to the BSL fallback trigger, carrying the NRC.
        prov, _ = _make_provider(dme_spec=_verified_dme_spec(), bench=False,
                                 dme_write_nrc=0x33)
        with self.assertRaises(DmeUdsWriteRejected) as cm:
            _run(prov.write_dme_isn(vin=VIN, dme_name="MEVD17_2_2",
                                    isn=ISN, requires_bench=True))
        self.assertEqual(cm.exception.nrc, 0x33)

    def test_uds_conditions_not_correct_diverts_to_bsl(self) -> None:
        prov, _ = _make_provider(dme_spec=_verified_dme_spec(), bench=False,
                                 dme_write_nrc=0x22)
        with self.assertRaises(DmeUdsWriteRejected) as cm:
            _run(prov.write_dme_isn(vin=VIN, dme_name="MEVD17_2_2",
                                    isn=ISN, requires_bench=True))
        self.assertEqual(cm.exception.nrc, 0x22)

    def test_uds_other_nrc_is_hard_failure(self) -> None:
        # A non-fallback NRC (e.g. 0x31 request-out-of-range) is a real error,
        # not a reason to enter the BSL wizard.
        prov, _ = _make_provider(dme_spec=_verified_dme_spec(), bench=False,
                                 dme_write_nrc=0x31)
        with self.assertRaises(SwapProviderError):
            _run(prov.write_dme_isn(vin=VIN, dme_name="MEVD17_2_2",
                                    isn=ISN, requires_bench=True))


class BslWriteTests(unittest.TestCase):
    """Phase 2: bsl_write_dme_isn drives the injected TricoreBslLink and lets
    handshake/flash failures propagate (the orchestrator keeps the wizard
    paused). Never a guessed offset."""

    def test_bsl_refuses_without_port(self) -> None:
        prov, _ = _make_provider(bench=False, bsl_port=None,
                                 bsl_link=_FakeBslLink())
        with self.assertRaises(SwapProviderError) as cm:
            _run(prov.bsl_write_dme_isn(vin=VIN, dme_name="MEVD17_2_2",
                                        dme_family="MEVD17", isn=ISN))
        self.assertIn("serial port", str(cm.exception).lower())

    def test_bsl_refuses_unknown_family(self) -> None:
        prov, _ = _make_provider(bench=False, bsl_port="/dev/fake",
                                 bsl_link=_FakeBslLink())
        with self.assertRaises(BslNotConfigured):
            _run(prov.bsl_write_dme_isn(vin=VIN, dme_name="X", dme_family="NOPE",
                                        isn=ISN))

    def test_bsl_handshake_failure_propagates(self) -> None:
        link = _FakeBslLink(handshake_fail=True)
        prov, _ = _make_provider(bench=False, bsl_port="/dev/fake", bsl_link=link)
        with self.assertRaises(BslHandshakeFailed):
            _run(prov.bsl_write_dme_isn(vin=VIN, dme_name="MEVD17_2_2",
                                        dme_family="MEVD17", isn=ISN))
        self.assertTrue(link.opened and link.closed)  # link always closed

    def test_bsl_not_configured_propagates(self) -> None:
        # Handshake ok, but no confirmed flash profile → refuse (no guessed write)
        link = _FakeBslLink(not_configured=True)
        prov, _ = _make_provider(bench=False, bsl_port="/dev/fake", bsl_link=link)
        with self.assertRaises(BslNotConfigured):
            _run(prov.bsl_write_dme_isn(vin=VIN, dme_name="MEVD17_2_2",
                                        dme_family="MEVD17", isn=ISN))
        self.assertIsNone(link.written)  # nothing written on a guessed offset
        self.assertTrue(link.closed)

    def test_bsl_success_writes_isn(self) -> None:
        link = _FakeBslLink()
        prov, _ = _make_provider(bench=False, bsl_port="/dev/fake", bsl_link=link)
        _run(prov.bsl_write_dme_isn(vin=VIN, dme_name="MEVD17_2_2",
                                    dme_family="MEVD17", isn=ISN))
        self.assertEqual(link.written, ISN)
        self.assertTrue(link.opened and link.closed)


class EnvDmeIsnAndBslTests(unittest.TestCase):
    def setUp(self) -> None:
        self._keys = ("BMW_ECU_SWAP_CAS_ADDR", "BMW_ECU_SWAP_DME_ADDR",
                      "BMW_ECU_SWAP_DME_ISN_DID", "BMW_ECU_SWAP_DME_ISN_LEVEL",
                      "BMW_ECU_SWAP_BSL_PORT", "BMW_ECU_KLINE_PORT")
        self._saved = {k: os.environ.get(k) for k in self._keys}
        for k in self._keys:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_dme_spec_built_only_when_both_present(self) -> None:
        os.environ["BMW_ECU_SWAP_CAS_ADDR"] = "0x40"
        os.environ["BMW_ECU_SWAP_DME_ADDR"] = "0x12"
        os.environ["BMW_ECU_SWAP_DME_ISN_DID"] = "0x1234"
        # level missing → no spec (never a half-configured guessed write)
        cfg = swap_address_config_from_env()
        self.assertIsNone(cfg.dme_isn_spec)
        os.environ["BMW_ECU_SWAP_DME_ISN_LEVEL"] = "0x11"
        cfg = swap_address_config_from_env()
        self.assertIsNotNone(cfg.dme_isn_spec)
        self.assertTrue(cfg.dme_isn_spec.verified)
        self.assertEqual(cfg.dme_isn_spec.did, 0x1234)

    def test_bsl_port_falls_back_to_kline_port(self) -> None:
        os.environ["BMW_ECU_SWAP_CAS_ADDR"] = "0x40"
        os.environ["BMW_ECU_SWAP_DME_ADDR"] = "0x12"
        os.environ["BMW_ECU_KLINE_PORT"] = "/dev/cu.usbserial-TEST"
        cfg = swap_address_config_from_env()
        self.assertEqual(cfg.bsl_port, "/dev/cu.usbserial-TEST")
        os.environ["BMW_ECU_SWAP_BSL_PORT"] = "/dev/cu.usbserial-BSL"
        cfg = swap_address_config_from_env()
        self.assertEqual(cfg.bsl_port, "/dev/cu.usbserial-BSL")


if __name__ == "__main__":
    unittest.main()
