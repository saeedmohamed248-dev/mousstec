"""ctypes bridge to a *native, already-licensed* seed-key library.

⚠️  NO CRYPTO LIVES HERE.
========================
This module contains zero BMW seed-key maths. It is a thin `ctypes` shim that
hands a Seed to a native library the workshop already has installed (e.g. an
EDIABAS / E-Sys factory DLL) and returns whatever Key that library natively
computes. Because the real factory algorithm does the work, the Key is always
correct for the immobiliser — we never fabricate or approximate one.

Safety contract (identical to the rest of the seed-key subsystem):
    • If the library is missing, the export can't be found, or the native call
      fails for any reason → raise `SeedKeyUnavailable`. NEVER return a guessed
      key. A wrong key on a real ECU can brick an immobiliser.
    • Nothing here is BMW-proprietary, so it is safe to commit. The proprietary
      part is the DLL on the shop machine, referenced only by path at runtime.

Wiring it up (no code edit needed — env driven):
    BMW_ECU_SEEDKEY_DLL_PATH    = C:\\EDIABAS\\Bin\\your_seedkey.dll
    BMW_ECU_SEEDKEY_DLL_EXPORT  = CalculateKey          # the export symbol
    BMW_ECU_SEEDKEY_DLL_FAMILY  = MEVD17                # default: MEVD17
    BMW_ECU_SEEDKEY_DLL_CONV    = stdcall               # or "cdecl"
    BMW_ECU_SEEDKEY_DLL_LEVEL   = 1                      # UDS 0x27 sub-function

The expected native ABI (the common buffer-in / buffer-out shape) is:

    int Export(const unsigned char *seed, int seed_len,
               unsigned char *key_out, int key_out_cap,
               int *key_len_out);          // returns 0 on success

If your DLL uses a different ABI, supply your own `key_fn` to
`DllWrapperSeedKeyProvider` instead of using `build_ctypes_key_fn`.
"""
from __future__ import annotations

import ctypes
import os
from typing import Callable, Optional

from .seed_key_providers import (
    AbstractSeedKeyProvider,
    SeedKeyUnavailable,
    register_seed_key_provider,
)

# A pure function: Seed bytes in → Key bytes out. Raises on any failure.
KeyFn = Callable[[bytes], bytes]

# Generous upper bound for the returned key buffer. Real BMW keys are a few
# bytes; 256 leaves head-room for any family without unbounded allocation.
_KEY_BUFFER_CAP = 256


class DllWrapperSeedKeyProvider(AbstractSeedKeyProvider):
    """Adapts a native `key_fn` into the AbstractSeedKeyProvider interface.

    The provider itself is pure and trivially testable: inject any callable
    (a real ctypes-backed function in production, a fake in tests). All the
    fragile native loading lives in `build_ctypes_key_fn`, kept separate so
    the safety behaviour can be unit-tested without a DLL present.
    """

    def __init__(self, *, family: str, key_fn: KeyFn,
                 security_level: int = 0x01) -> None:
        self.ecu_family = (family or "").upper()
        self.security_level = security_level
        self._key_fn = key_fn

    def compute_key(self, seed: bytes, *, vin: str | None = None) -> bytes:
        if not seed:
            raise ValueError("Empty seed")
        try:
            key = self._key_fn(bytes(seed))
        except SeedKeyUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 — any native failure → refuse
            raise SeedKeyUnavailable(
                f"Native seed-key library failed for family "
                f"'{self.ecu_family}': {e}. Refusing to return a fabricated "
                f"key against real hardware."
            ) from e
        if not key:
            raise SeedKeyUnavailable(
                f"Native seed-key library returned an empty key for family "
                f"'{self.ecu_family}'. Refusing to proceed."
            )
        return bytes(key)


def build_ctypes_key_fn(dll_path: str, export_name: str, *,
                        convention: str = "stdcall",
                        key_cap: int = _KEY_BUFFER_CAP) -> KeyFn:
    """Build a `KeyFn` that calls `export_name` in the native `dll_path`.

    Loads the library lazily *inside* the returned callable so that simply
    constructing/registering the provider never raises on a machine where the
    DLL is absent (e.g. the macOS dev box) — it only fails, loudly and safely,
    when an actual ISN read is attempted.

    `convention`:
        "stdcall" → ctypes.WinDLL  (Windows EDIABAS/E-Sys DLLs; default)
        "cdecl"   → ctypes.CDLL    (cross-platform / .so / .dylib)
    """
    conv = (convention or "stdcall").lower()
    _loader_cache: dict[str, Callable[..., int]] = {}

    def _resolve_native() -> Callable[..., int]:
        cached = _loader_cache.get("fn")
        if cached is not None:
            return cached
        if not dll_path or not os.path.exists(dll_path):
            raise SeedKeyUnavailable(
                f"Seed-key library not found at '{dll_path}'. Set "
                f"BMW_ECU_SEEDKEY_DLL_PATH to the installed factory DLL."
            )
        if conv == "stdcall":
            if not hasattr(ctypes, "WinDLL"):
                raise SeedKeyUnavailable(
                    "stdcall (WinDLL) requested but unavailable on this OS. "
                    "Run ISN reads on the Windows shop machine, or set "
                    "BMW_ECU_SEEDKEY_DLL_CONV=cdecl for a cdecl library."
                )
            lib = ctypes.WinDLL(dll_path)  # type: ignore[attr-defined]
        else:
            lib = ctypes.CDLL(dll_path)
        try:
            fn = getattr(lib, export_name)
        except AttributeError as e:
            raise SeedKeyUnavailable(
                f"Export '{export_name}' not found in '{dll_path}'."
            ) from e
        fn.restype = ctypes.c_int
        fn.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte),  # seed
            ctypes.c_int,                    # seed_len
            ctypes.POINTER(ctypes.c_ubyte),  # key_out
            ctypes.c_int,                    # key_out_cap
            ctypes.POINTER(ctypes.c_int),    # key_len_out
        ]
        _loader_cache["fn"] = fn
        return fn

    def key_fn(seed: bytes) -> bytes:
        fn = _resolve_native()
        seed_buf = (ctypes.c_ubyte * len(seed)).from_buffer_copy(seed)
        key_buf = (ctypes.c_ubyte * key_cap)()
        key_len = ctypes.c_int(0)
        rc = fn(seed_buf, len(seed), key_buf, key_cap, ctypes.byref(key_len))
        if rc != 0:
            raise SeedKeyUnavailable(
                f"Native seed-key call returned status {rc} (non-zero). "
                f"No key produced."
            )
        n = key_len.value
        if n <= 0 or n > key_cap:
            raise SeedKeyUnavailable(
                f"Native seed-key call reported invalid key length {n}."
            )
        return bytes(key_buf[:n])

    return key_fn


def register_dll_seed_key_provider_from_env() -> Optional[DllWrapperSeedKeyProvider]:
    """Register a DLL-backed provider from env vars, if configured.

    Returns the registered provider, or None when no DLL path is set. Never
    raises — a misconfigured backend simply leaves the registry untouched so
    the system falls back to the refuse-to-fake `UnavailableSeedKeyProvider`.
    """
    dll_path = os.environ.get("BMW_ECU_SEEDKEY_DLL_PATH", "").strip()
    if not dll_path:
        return None
    export = os.environ.get("BMW_ECU_SEEDKEY_DLL_EXPORT", "CalculateKey").strip()
    family = os.environ.get("BMW_ECU_SEEDKEY_DLL_FAMILY", "MEVD17").strip()
    convention = os.environ.get("BMW_ECU_SEEDKEY_DLL_CONV", "stdcall").strip()
    try:
        level = int(os.environ.get("BMW_ECU_SEEDKEY_DLL_LEVEL", "1"), 0)
    except ValueError:
        level = 0x01
    try:
        key_fn = build_ctypes_key_fn(dll_path, export, convention=convention)
        provider = DllWrapperSeedKeyProvider(
            family=family, key_fn=key_fn, security_level=level)
        register_seed_key_provider(provider, family=family)
        return provider
    except Exception:  # noqa: BLE001 — never let backend wiring kill a request
        return None
