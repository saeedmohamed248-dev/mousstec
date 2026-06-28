"""ctypes-backed seed-key provider (`DllWrapperSeedKeyProvider`).

Two layers under test:

  1. The pure provider — given an injected native `key_fn`, it returns the
     key, and on ANY native failure it refuses (SeedKeyUnavailable) instead of
     fabricating a key. This is the safety-critical layer.

  2. The real `ctypes` glue — we compile a tiny native library at test time
     (skipped if no C compiler is present) and prove the buffer-in/buffer-out
     ABI marshalling actually round-trips Seed → Key against real machine code.
     This is the gold standard: it exercises the same code path a factory DLL
     would, with deterministic maths we control.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from bmw_ecu.execution.ecu_profiles import KNOWN_PROFILES
from bmw_ecu.uds import seed_key_providers as skp
from bmw_ecu.uds.dll_seed_key import (
    DllWrapperSeedKeyProvider,
    build_ctypes_key_fn,
    register_dll_seed_key_provider_from_env,
)
from bmw_ecu.uds.seed_key_providers import (
    SeedKeyUnavailable,
    resolve_seed_key_provider,
)


class ProviderRefusalTests(unittest.TestCase):
    """The pure provider never fabricates — it returns or it refuses."""

    def test_returns_native_key(self) -> None:
        prov = DllWrapperSeedKeyProvider(
            family="MEVD17",
            key_fn=lambda seed: bytes(b ^ 0x5A for b in seed),
            security_level=0x01,
        )
        self.assertEqual(
            prov.compute_key(b"\x01\x02\x03\x04"),
            bytes(b ^ 0x5A for b in b"\x01\x02\x03\x04"),
        )

    def test_native_exception_becomes_refusal(self) -> None:
        def boom(_seed: bytes) -> bytes:
            raise OSError("DLL exploded")

        prov = DllWrapperSeedKeyProvider(family="MEVD17", key_fn=boom)
        with self.assertRaises(SeedKeyUnavailable):
            prov.compute_key(b"\x01\x02\x03\x04")

    def test_empty_native_key_is_refused(self) -> None:
        prov = DllWrapperSeedKeyProvider(family="MEVD17", key_fn=lambda s: b"")
        with self.assertRaises(SeedKeyUnavailable):
            prov.compute_key(b"\x01\x02\x03\x04")

    def test_existing_unavailable_is_propagated(self) -> None:
        def refuse(_seed: bytes) -> bytes:
            raise SeedKeyUnavailable("no backend")

        prov = DllWrapperSeedKeyProvider(family="MEVD17", key_fn=refuse)
        with self.assertRaises(SeedKeyUnavailable):
            prov.compute_key(b"\x01\x02\x03\x04")

    def test_missing_dll_refuses_at_call_time_not_build_time(self) -> None:
        # Building against a non-existent path must NOT raise (dev box has no
        # DLL); only the actual ISN read refuses.
        key_fn = build_ctypes_key_fn(
            "/no/such/seedkey.dll", "CalculateKey", convention="cdecl")
        prov = DllWrapperSeedKeyProvider(family="MEVD17", key_fn=key_fn)
        with self.assertRaises(SeedKeyUnavailable):
            prov.compute_key(b"\x01\x02\x03\x04")


class EnvRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_reg = dict(skp._REGISTRY)
        skp._REGISTRY.clear()
        self._saved_env = {
            k: os.environ.get(k) for k in (
                "BMW_ECU_SEEDKEY_DLL_PATH", "BMW_ECU_SEEDKEY_DLL_EXPORT",
                "BMW_ECU_SEEDKEY_DLL_FAMILY", "BMW_ECU_SEEDKEY_DLL_CONV",
            )
        }
        for k in self._saved_env:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        skp._REGISTRY.clear()
        skp._REGISTRY.update(self._saved_reg)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_no_env_registers_nothing(self) -> None:
        self.assertIsNone(register_dll_seed_key_provider_from_env())

    def test_env_registers_for_mevd17_and_unlocks_both_engines(self) -> None:
        lib = _compile_xor_lib(self)
        os.environ["BMW_ECU_SEEDKEY_DLL_PATH"] = lib
        os.environ["BMW_ECU_SEEDKEY_DLL_EXPORT"] = "seedkey"
        os.environ["BMW_ECU_SEEDKEY_DLL_FAMILY"] = "MEVD17"
        os.environ["BMW_ECU_SEEDKEY_DLL_CONV"] = "cdecl"

        prov = register_dll_seed_key_provider_from_env()
        self.assertIsNotNone(prov)

        # Both the N20 DME and the Mini N18 declare seed_key_family "MEVD17",
        # so the SAME kind of DLL-backed provider answers for both and computes
        # the real native key. (Identity may differ because resolve() may
        # re-run env wiring; what matters is the family + the computed key.)
        for name in ("MEVD17_2_9", "MEVD17_2_2_N18"):
            fam = KNOWN_PROFILES[name].seed_key_family
            resolved = resolve_seed_key_provider(family=fam, simulator=False)
            self.assertIsInstance(resolved, DllWrapperSeedKeyProvider)
            self.assertEqual(resolved.ecu_family, "MEVD17")
            self.assertEqual(
                resolved.compute_key(b"\x10\x20\x30\x40"),
                bytes(b ^ 0x5A for b in b"\x10\x20\x30\x40"),
            )


class CtypesAbiTests(unittest.TestCase):
    """Exercise the real ctypes marshalling against compiled machine code."""

    def test_real_native_call_roundtrips(self) -> None:
        lib = _compile_xor_lib(self)
        key_fn = build_ctypes_key_fn(lib, "seedkey", convention="cdecl")
        seed = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE])
        self.assertEqual(key_fn(seed), bytes(b ^ 0x5A for b in seed))

    def test_native_nonzero_status_refuses(self) -> None:
        # The stub returns non-zero when seed_len > 8 — prove we refuse.
        lib = _compile_xor_lib(self)
        key_fn = build_ctypes_key_fn(lib, "seedkey", convention="cdecl")
        with self.assertRaises(SeedKeyUnavailable):
            key_fn(bytes(range(16)))

    def test_missing_export_refuses(self) -> None:
        lib = _compile_xor_lib(self)
        key_fn = build_ctypes_key_fn(lib, "no_such_export", convention="cdecl")
        with self.assertRaises(SeedKeyUnavailable):
            key_fn(b"\x01\x02")


# --- helpers ----------------------------------------------------------------
_C_SOURCE = textwrap.dedent(
    """
    /* Deterministic stand-in for a factory seed-key DLL. XORs each seed byte
       with 0x5A. Returns non-zero for over-long seeds so the failure path is
       testable. Signature matches the buffer-in/buffer-out ABI the wrapper
       expects. */
    int seedkey(const unsigned char *seed, int seed_len,
                unsigned char *key_out, int key_cap, int *key_len_out) {
        if (seed_len > 8 || seed_len > key_cap) return 1;
        for (int i = 0; i < seed_len; i++) key_out[i] = seed[i] ^ 0x5A;
        *key_len_out = seed_len;
        return 0;
    }
    """
)


def _compile_xor_lib(test: unittest.TestCase) -> str:
    """Compile the C stub into a shared lib; skip the test if no compiler."""
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        test.skipTest("no C compiler available to build the native stub")
    tmp = tempfile.mkdtemp(prefix="seedkey_")
    src = os.path.join(tmp, "stub.c")
    out = os.path.join(tmp, "libseedkey.so")
    with open(src, "w") as fh:
        fh.write(_C_SOURCE)
    proc = subprocess.run(
        [cc, "-shared", "-fPIC", "-o", out, src],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        test.skipTest(f"compiler failed: {proc.stderr.strip()}")
    test.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    return out


if __name__ == "__main__":
    unittest.main()
