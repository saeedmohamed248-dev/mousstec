"""Guided ECU-flash orchestrator — pure-Python, zero DB, zero hardware.

Drives the FlashOrchestrator over the deterministic MockFlashProvider +
MockSafetyGate, asserting the invariants that keep a flash from bricking
an ECU:
  • the catalog is well-formed (size bands sane, addresses distinct),
  • the happy path walks IDLE→READY→BACKED_UP→FLASHED→DONE and writes
    every block of the payload,
  • a BACKUP is ALWAYS taken before anything is erased, and a security
    unlock only happens on jobs that need it,
  • a failure at ANY flash step (erase / download / transfer / exit /
    dependencies) rolls the saved backup back in — the mock's
    `image_written` flag proves the ECU was made whole again,
  • ABORT after backup also rolls back,
  • payload size-band validation catches a wrong-file flash before erase,
  • entitlement (check at START, consume once on FINISH; never consume on
    a failed/blocked/aborted flash),
  • the bad-event, illegal-transition + snapshot/restore paths.
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.flashing import (
    FLASH_CATALOG,
    FLASH_FEATURE,
    AbstractFlashProvider,
    FlashBackup,
    FlashData,
    FlashEvent,
    FlashOrchestrator,
    FlashPrompt,
    FlashState,
    IllegalFlashTransition,
    MockFlashProvider,
    all_flash_jobs,
    get_flash_job,
)
from bmw_ecu.flashing.checksum import compute_checksum
from bmw_ecu.premium.safety_checks import IgnitionState, MockSafetyGate
from bmw_ecu.services.entitlement_guard import MockEntitlementGuard


def _run(coro):
    return asyncio.run(coro)


_VIN = "WBA12345678901234"


def _good_safety() -> MockSafetyGate:
    # Charged battery, key-on/engine-off — exactly what flashing needs.
    return MockSafetyGate(voltage_v=13.6, ignition=IgnitionState.KOEO)


def _payload_for(job, *, extra: int = 16) -> bytes:
    return b"\x12" * (job.expected_min_bytes + extra)


def _orch(*, safety=None, provider=None, entitlement=None) -> FlashOrchestrator:
    return FlashOrchestrator(
        safety=safety or _good_safety(),
        provider=provider or MockFlashProvider(),
        entitlement=entitlement,
    )


async def _drive_to_backed_up(orch, job_code, *, vin=_VIN, payload=None):
    job = get_flash_job(job_code)
    await orch.handle(FlashEvent.START, {
        "job_code": job_code, "vin": vin,
        "payload": payload if payload is not None else _payload_for(job),
    })
    await orch.handle(FlashEvent.BACKUP)
    return orch


# ─────────────────────────────────────────────────────────────────────
# Catalog
# ─────────────────────────────────────────────────────────────────────
class FlashCatalogTests(unittest.TestCase):
    def test_catalog_non_empty(self) -> None:
        self.assertTrue(FLASH_CATALOG)
        self.assertEqual(len(all_flash_jobs()), len(FLASH_CATALOG))

    def test_jobs_well_formed(self) -> None:
        for job in all_flash_jobs():
            with self.subTest(job.code):
                self.assertTrue(job.code)
                self.assertTrue(job.name_ar)
                self.assertTrue(job.target_module)
                self.assertGreater(job.target_addr, 0)
                self.assertEqual(job.feature_code, FLASH_FEATURE)
                # Sane payload band.
                self.assertGreater(job.expected_min_bytes, 0)
                self.assertLess(job.expected_min_bytes, job.expected_max_bytes)
                self.assertTrue(job.success_message_ar)
                self.assertTrue(job.preflight_ar)
                self.assertEqual(job.checksum_algo, "crc32")

    def test_addresses_distinct(self) -> None:
        addrs = [j.target_addr for j in all_flash_jobs()]
        self.assertEqual(len(addrs), len(set(addrs)))

    def test_size_ok_band(self) -> None:
        job = get_flash_job("dme_sw_update")
        self.assertTrue(job.size_ok(job.expected_min_bytes))
        self.assertTrue(job.size_ok(job.expected_max_bytes))
        self.assertFalse(job.size_ok(job.expected_min_bytes - 1))
        self.assertFalse(job.size_ok(job.expected_max_bytes + 1))

    def test_get_flash_job_unknown(self) -> None:
        self.assertIsNone(get_flash_job("does_not_exist"))

    def test_to_dict_shape(self) -> None:
        d = get_flash_job("dme_sw_update").to_dict()
        self.assertEqual(d["code"], "dme_sw_update")
        self.assertTrue(d["target_addr"].startswith("0x"))
        self.assertEqual(d["feature_code"], FLASH_FEATURE)


# ─────────────────────────────────────────────────────────────────────
# Provider mock
# ─────────────────────────────────────────────────────────────────────
class FlashProviderTests(unittest.TestCase):
    def test_is_abstract_subclass(self) -> None:
        self.assertIsInstance(MockFlashProvider(), AbstractFlashProvider)

    def test_version_reflects_image_state(self) -> None:
        prov = MockFlashProvider(current_version="A", new_version="B")
        self.assertEqual(_run(prov.read_current_version()), "A")
        _run(prov.erase(addr=0x1000))
        self.assertTrue(prov.image_written)
        self.assertEqual(_run(prov.read_current_version()), "B")

    def test_restore_makes_whole(self) -> None:
        prov = MockFlashProvider()
        _run(prov.erase(addr=0x1000))
        self.assertTrue(prov.image_written)
        backup = FlashBackup(ecu_name="m", vin="", origin_addr=0x1000,
                             data=b"\xFF" * 32)
        _run(prov.restore_backup(backup))
        self.assertFalse(prov.image_written)
        self.assertEqual(prov.restore_calls, [32])


# ─────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────
class FlashHappyPathTests(unittest.TestCase):
    def test_full_flow_dme(self) -> None:
        prov = MockFlashProvider()
        ent = MockEntitlementGuard(feature_code=FLASH_FEATURE)
        orch = _orch(provider=prov, entitlement=ent)
        job = get_flash_job("dme_sw_update")
        payload = _payload_for(job)

        p = _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN, "payload": payload}))
        self.assertEqual(orch.state, FlashState.READY)
        self.assertEqual(p.expects, "BACKUP")
        self.assertEqual(p.payload["current_version"], "SW_01")
        self.assertEqual(p.payload["payload_checksum"],
                         compute_checksum(payload, algo="crc32"))

        p = _run(orch.handle(FlashEvent.BACKUP))
        self.assertEqual(orch.state, FlashState.BACKED_UP)
        self.assertEqual(p.expects, "FLASH")
        # Backup taken BEFORE any erase.
        self.assertEqual(prov.backup_calls, [(job.target_addr, len(payload))])
        self.assertFalse(prov.erase_calls)
        self.assertFalse(prov.image_written)

        p = _run(orch.handle(FlashEvent.FLASH))
        self.assertEqual(orch.state, FlashState.FLASHED)
        self.assertEqual(p.expects, "FINISH")
        self.assertEqual(prov.erase_calls, [job.target_addr])
        self.assertEqual(prov.exit_calls, 1)
        self.assertEqual(prov.dependency_calls, 1)
        self.assertEqual(prov.reset_calls, 1)
        self.assertEqual(p.payload["new_version"], "SW_02")

        p = _run(orch.handle(FlashEvent.FINISH))
        self.assertEqual(orch.state, FlashState.DONE)
        self.assertTrue(p.is_terminal)
        self.assertFalse(p.is_error)
        self.assertEqual(p.progress_pct, 100)

    def test_every_block_written(self) -> None:
        job = get_flash_job("dme_sw_update")
        payload = _payload_for(job, extra=0)  # exact min
        prov = MockFlashProvider(max_block_len=0x400)
        orch = _orch(provider=prov)
        _run(_drive_to_backed_up(orch, "dme_sw_update", payload=payload))
        _run(orch.handle(FlashEvent.FLASH))
        # Total bytes transferred == payload length.
        total = sum(n for _seq, n in prov.transfer_calls)
        self.assertEqual(total, len(payload))
        # Sequence numbers are contiguous from 1.
        seqs = [seq for seq, _n in prov.transfer_calls]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertEqual(orch.data.blocks_written, len(seqs))

    def test_security_unlock_only_when_needed(self) -> None:
        # DME needs security.
        prov_dme = MockFlashProvider()
        _run(_drive_to_backed_up(_orch(provider=prov_dme), "dme_sw_update"))
        self.assertEqual(prov_dme.security_calls, [_VIN])
        # KOMBI does not.
        prov_k = MockFlashProvider()
        _run(_drive_to_backed_up(_orch(provider=prov_k), "kombi_update"))
        self.assertEqual(prov_k.security_calls, [])

    def test_kombi_full_flow(self) -> None:
        prov = MockFlashProvider()
        orch = _orch(provider=prov)
        _run(_drive_to_backed_up(orch, "kombi_update"))
        _run(orch.handle(FlashEvent.FLASH))
        p = _run(orch.handle(FlashEvent.FINISH))
        self.assertEqual(orch.state, FlashState.DONE)
        self.assertIn("KOMBI", p.body)


# ─────────────────────────────────────────────────────────────────────
# Backup-before-erase + rollback invariant
# ─────────────────────────────────────────────────────────────────────
class FlashRollbackTests(unittest.TestCase):
    def _assert_rolled_back(self, fail_kwargs, expected_code) -> None:
        prov = MockFlashProvider(**fail_kwargs)
        ent = MockEntitlementGuard(feature_code=FLASH_FEATURE)
        orch = _orch(provider=prov, entitlement=ent)
        _run(_drive_to_backed_up(orch, "dme_sw_update"))
        p = _run(orch.handle(FlashEvent.FLASH))
        self.assertEqual(orch.state, FlashState.FAILED)
        self.assertTrue(p.is_error)
        self.assertEqual(orch.data.error_code, expected_code)
        # Rollback happened and the ECU is whole again.
        self.assertTrue(orch.data.rolled_back)
        self.assertTrue(p.payload["rolled_back"])
        self.assertEqual(len(prov.restore_calls), 1)
        self.assertFalse(prov.image_written)
        # A failed flash never consumes the grant.
        self.assertEqual(ent.consume_calls, [])

    def test_rollback_on_erase(self) -> None:
        self._assert_rolled_back({"fail_on": "erase"}, "flash_rejected")

    def test_rollback_on_request_download(self) -> None:
        self._assert_rolled_back(
            {"fail_on": "request_download"}, "flash_rejected")

    def test_rollback_on_transfer(self) -> None:
        self._assert_rolled_back({"fail_on": "transfer"}, "flash_rejected")

    def test_rollback_on_transfer_exit(self) -> None:
        self._assert_rolled_back({"fail_on": "transfer_exit"}, "flash_rejected")

    def test_rollback_on_dependencies(self) -> None:
        self._assert_rolled_back(
            {"fail_dependencies": True}, "dependency_failed")

    def test_abort_after_backup_rolls_back(self) -> None:
        prov = MockFlashProvider()
        orch = _orch(provider=prov)
        _run(_drive_to_backed_up(orch, "dme_sw_update"))
        p = _run(orch.handle(FlashEvent.ABORT))
        self.assertEqual(orch.state, FlashState.FAILED)
        self.assertEqual(orch.data.error_code, "aborted_by_user")
        self.assertTrue(orch.data.rolled_back)
        self.assertEqual(len(prov.restore_calls), 1)
        self.assertFalse(prov.image_written)

    def test_abort_before_backup_does_not_restore(self) -> None:
        # ABORT in READY (nothing erased, no backup yet) — no restore call.
        prov = MockFlashProvider()
        job = get_flash_job("dme_sw_update")
        orch = _orch(provider=prov)
        _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN,
            "payload": _payload_for(job)}))
        p = _run(orch.handle(FlashEvent.ABORT))
        self.assertEqual(orch.state, FlashState.FAILED)
        self.assertEqual(prov.restore_calls, [])
        self.assertFalse(orch.data.rolled_back)

    def test_rollback_is_idempotent(self) -> None:
        prov = MockFlashProvider(fail_on="transfer")
        orch = _orch(provider=prov)
        _run(_drive_to_backed_up(orch, "dme_sw_update"))
        _run(orch.handle(FlashEvent.FLASH))
        self.assertEqual(len(prov.restore_calls), 1)
        # A second ABORT must not restore twice.
        _run(orch.handle(FlashEvent.ABORT))
        self.assertEqual(len(prov.restore_calls), 1)


# ─────────────────────────────────────────────────────────────────────
# Pre-flash guards (size band, safety, unknown job, bad payload)
# ─────────────────────────────────────────────────────────────────────
class FlashGuardTests(unittest.TestCase):
    def test_payload_too_small_blocks_before_erase(self) -> None:
        prov = MockFlashProvider()
        orch = _orch(provider=prov)
        p = _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN, "payload": b"\x00" * 100}))
        self.assertEqual(orch.state, FlashState.FAILED)
        self.assertEqual(orch.data.error_code, "bad_payload_size")
        self.assertEqual(prov.erase_calls, [])
        self.assertEqual(prov.backup_calls, [])

    def test_payload_too_large_blocks(self) -> None:
        job = get_flash_job("kombi_update")
        prov = MockFlashProvider()
        orch = _orch(provider=prov)
        big = b"\x00" * (job.expected_max_bytes + 1)
        p = _run(orch.handle(FlashEvent.START, {
            "job_code": "kombi_update", "vin": _VIN, "payload": big}))
        self.assertEqual(orch.data.error_code, "bad_payload_size")

    def test_payload_not_bytes(self) -> None:
        orch = _orch()
        p = _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN, "payload": "not-bytes"}))
        self.assertEqual(orch.data.error_code, "bad_payload")

    def test_unknown_job(self) -> None:
        orch = _orch()
        p = _run(orch.handle(FlashEvent.START, {
            "job_code": "nope", "vin": _VIN, "payload": b"\x00" * 999999}))
        self.assertEqual(orch.data.error_code, "unknown_job")

    def test_low_voltage_blocks(self) -> None:
        job = get_flash_job("dme_sw_update")
        prov = MockFlashProvider()
        bad = MockSafetyGate(voltage_v=11.4, ignition=IgnitionState.KOEO)
        orch = _orch(safety=bad, provider=prov)
        p = _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN,
            "payload": _payload_for(job)}))
        self.assertEqual(orch.state, FlashState.FAILED)
        self.assertEqual(orch.data.error_code, "prereq_failed")
        self.assertEqual(prov.session_calls, 0)

    def test_wrong_ignition_blocks(self) -> None:
        job = get_flash_job("dme_sw_update")
        bad = MockSafetyGate(voltage_v=13.6, ignition=IgnitionState.KOER)
        orch = _orch(safety=bad)
        p = _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN,
            "payload": _payload_for(job)}))
        self.assertEqual(orch.data.error_code, "prereq_failed")

    def test_bus_down_at_backup(self) -> None:
        job = get_flash_job("dme_sw_update")
        prov = MockFlashProvider(bus_down=True)
        orch = _orch(provider=prov)
        _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN,
            "payload": _payload_for(job)}))
        p = _run(orch.handle(FlashEvent.BACKUP))
        self.assertEqual(orch.state, FlashState.FAILED)
        self.assertEqual(orch.data.error_code, "transport_error")
        # No erase, nothing to roll back (failed before BACKED_UP).
        self.assertFalse(orch.data.rolled_back)

    def test_security_denied_at_backup(self) -> None:
        job = get_flash_job("dme_sw_update")
        prov = MockFlashProvider(deny_security=True)
        orch = _orch(provider=prov)
        _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN,
            "payload": _payload_for(job)}))
        p = _run(orch.handle(FlashEvent.BACKUP))
        self.assertEqual(orch.data.error_code, "security_denied")
        self.assertFalse(prov.erase_calls)


# ─────────────────────────────────────────────────────────────────────
# Entitlement
# ─────────────────────────────────────────────────────────────────────
class FlashEntitlementTests(unittest.TestCase):
    def test_check_at_start_consume_on_finish(self) -> None:
        ent = MockEntitlementGuard(feature_code=FLASH_FEATURE)
        orch = _orch(entitlement=ent)
        _run(_drive_to_backed_up(orch, "dme_sw_update"))
        self.assertEqual(ent.check_calls, 1)
        self.assertEqual(ent.consume_calls, [])
        _run(orch.handle(FlashEvent.FLASH))
        _run(orch.handle(FlashEvent.FINISH))
        self.assertEqual(len(ent.consume_calls), 1)
        self.assertEqual(ent.consume_calls[0]["operation_ref"],
                         f"dme_sw_update-{_VIN}")

    def test_unentitled_blocks_at_start(self) -> None:
        job = get_flash_job("dme_sw_update")
        prov = MockFlashProvider()
        ent = MockEntitlementGuard(
            feature_code=FLASH_FEATURE, entitled_result=False)
        orch = _orch(provider=prov, entitlement=ent)
        p = _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN,
            "payload": _payload_for(job)}))
        self.assertEqual(orch.state, FlashState.FAILED)
        self.assertEqual(orch.data.error_code, "not_entitled")
        self.assertEqual(ent.consume_calls, [])
        # No safety probe / bus chatter after an entitlement refusal.
        self.assertEqual(prov.session_calls, 0)

    def test_no_consume_on_rollback(self) -> None:
        ent = MockEntitlementGuard(feature_code=FLASH_FEATURE)
        prov = MockFlashProvider(fail_dependencies=True)
        orch = _orch(provider=prov, entitlement=ent)
        _run(_drive_to_backed_up(orch, "dme_sw_update"))
        _run(orch.handle(FlashEvent.FLASH))
        self.assertEqual(ent.consume_calls, [])

    def test_orchestrator_works_without_entitlement(self) -> None:
        orch = _orch(entitlement=None)
        _run(_drive_to_backed_up(orch, "dme_sw_update"))
        _run(orch.handle(FlashEvent.FLASH))
        p = _run(orch.handle(FlashEvent.FINISH))
        self.assertEqual(orch.state, FlashState.DONE)


# ─────────────────────────────────────────────────────────────────────
# Illegal transitions + bad events
# ─────────────────────────────────────────────────────────────────────
class FlashTransitionTests(unittest.TestCase):
    def test_backup_before_start_is_illegal(self) -> None:
        orch = _orch()
        p = _run(orch.handle(FlashEvent.BACKUP))
        self.assertEqual(orch.data.error_code, "illegal_transition")

    def test_flash_before_backup_is_illegal(self) -> None:
        job = get_flash_job("dme_sw_update")
        orch = _orch()
        _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN,
            "payload": _payload_for(job)}))
        p = _run(orch.handle(FlashEvent.FLASH))
        self.assertEqual(orch.data.error_code, "illegal_transition")

    def test_finish_before_flash_is_illegal(self) -> None:
        orch = _orch()
        _run(_drive_to_backed_up(orch, "dme_sw_update"))
        p = _run(orch.handle(FlashEvent.FINISH))
        self.assertEqual(orch.data.error_code, "illegal_transition")

    def test_double_start_is_illegal(self) -> None:
        job = get_flash_job("dme_sw_update")
        orch = _orch()
        _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN,
            "payload": _payload_for(job)}))
        p = _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN,
            "payload": _payload_for(job)}))
        self.assertEqual(orch.data.error_code, "illegal_transition")

    def test_unknown_event(self) -> None:
        orch = _orch()
        p = _run(orch.handle("frobnicate"))
        self.assertEqual(orch.data.error_code, "unknown_event")

    def test_terminal_state_rejects_events(self) -> None:
        orch = _orch()
        _run(_drive_to_backed_up(orch, "dme_sw_update"))
        _run(orch.handle(FlashEvent.FLASH))
        _run(orch.handle(FlashEvent.FINISH))
        self.assertEqual(orch.state, FlashState.DONE)
        p = _run(orch.handle(FlashEvent.FLASH))
        self.assertEqual(orch.data.error_code, "illegal_transition")


# ─────────────────────────────────────────────────────────────────────
# Snapshot / restore
# ─────────────────────────────────────────────────────────────────────
class FlashSnapshotTests(unittest.TestCase):
    def test_snapshot_roundtrip_at_ready(self) -> None:
        job = get_flash_job("dme_sw_update")
        prov = MockFlashProvider()
        orch = _orch(provider=prov)
        _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN,
            "payload": _payload_for(job)}))
        snap = orch.snapshot()
        self.assertEqual(snap["state"], "ready")

        o2 = FlashOrchestrator.restore(
            safety=_good_safety(), provider=prov, snapshot=snap)
        self.assertEqual(o2.state, FlashState.READY)
        self.assertEqual(o2.data.job_code, "dme_sw_update")
        self.assertEqual(o2.data.vin, _VIN)
        self.assertEqual(o2.job.code, "dme_sw_update")
        self.assertEqual(o2.data.payload_len, len(_payload_for(job)))

    def test_snapshot_preserves_progress_fields(self) -> None:
        prov = MockFlashProvider()
        orch = _orch(provider=prov)
        _run(_drive_to_backed_up(orch, "dme_sw_update"))
        _run(orch.handle(FlashEvent.FLASH))
        snap = orch.snapshot()
        o2 = FlashOrchestrator.restore(
            safety=_good_safety(), provider=prov, snapshot=snap)
        self.assertEqual(o2.state, FlashState.FLASHED)
        self.assertEqual(o2.data.blocks_written, orch.data.blocks_written)
        self.assertEqual(o2.data.backup_size, orch.data.backup_size)

    def test_prompt_to_dict_shape(self) -> None:
        job = get_flash_job("dme_sw_update")
        orch = _orch()
        p = _run(orch.handle(FlashEvent.START, {
            "job_code": "dme_sw_update", "vin": _VIN,
            "payload": _payload_for(job)}))
        d = p.to_dict()
        for key in ("state", "title", "body", "expects", "progress_pct",
                    "payload", "is_terminal", "is_error"):
            self.assertIn(key, d)
        self.assertEqual(d["state"], "ready")


if __name__ == "__main__":
    unittest.main()
