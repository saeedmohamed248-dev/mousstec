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

    def __init__(self, isn: bytes) -> None:
        super().__init__(TransportConfig(kind=TransportKind.KLINE))
        self._isn = isn
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


def _verified_cas_spec() -> IsnAccessSpec:
    return IsnAccessSpec(family="CAS", did=0xABCD, security_level=0x03,
                         length=32, over_uds=True, verified=True,
                         notes="test-confirmed")


def _make_provider(*, bench=True, cas_spec=None, ews_routine=0x1234,
                   allow_unverified=False, store_root=None):
    transport = _FakeTransport(ISN)
    addr = SwapAddressConfig(
        cas_ecu_addr=0x40, dme_ecu_addr=0x12,
        cas_isn_spec=cas_spec, ews_routine_id=ews_routine,
        allow_unverified=allow_unverified)
    store = BackupStore(Path(store_root or tempfile.mkdtemp()))
    prov = RealDmeSwapProvider(
        transport=transport,
        cas_seed_provider=_StubSeed(), dme_seed_provider=_StubSeed(),
        addr=addr, backup_store=store,
        bench=_MockBench(ISN) if bench else None)
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

    def test_bench_write_refuses_without_bench(self) -> None:
        prov, _ = _make_provider(bench=False)
        with self.assertRaises(SwapProviderError) as cm:
            _run(prov.write_dme_isn(vin=VIN, dme_name="MEVD17_2_2",
                                    isn=ISN, requires_bench=True))
        self.assertIn("bench", str(cm.exception).lower())

    def test_uds_write_path_is_locked(self) -> None:
        prov, _ = _make_provider(bench=True)
        with self.assertRaises(SwapProviderError):
            _run(prov.write_dme_isn(vin=VIN, dme_name="SOME_UDS_DME",
                                    isn=ISN, requires_bench=False))

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


if __name__ == "__main__":
    unittest.main()
