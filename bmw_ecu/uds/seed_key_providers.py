"""Seed-Key providers.

⚠️  CRITICAL — READ BEFORE TOUCHING A VEHICLE
============================================
The real BMW FEM / CAS / DME Seed-Key algorithms are proprietary and are
intentionally NOT shipped here. `MockSeedKeyProvider` is a deterministic
toy XOR scheme for unit tests only.

To use this subsystem against a physical ECU:
    1. Implement a subclass of `AbstractSeedKeyProvider`.
    2. Wire your licensed library / hardware dongle in `compute_key()`.
    3. Register it via `bmw_ecu.uds.SecurityAccess(provider=YourProvider())`.
"""
from __future__ import annotations

import abc
import importlib
import os
from typing import Final, Optional


class SeedKeyUnavailable(RuntimeError):
    """No licensed seed-key provider is installed for this ECU family.

    Raised at key-computation time (never at connect time) so that read-only
    flows still work — only the security-gated steps (ISN read, key write,
    flash) fail, and they fail loudly instead of bricking a car with a fake
    key.
    """


class AbstractSeedKeyProvider(abc.ABC):
    """One provider per ECU family + security level."""

    ecu_family: str
    security_level: int  # UDS sub-function (0x01, 0x03, 0x05, ...)

    @abc.abstractmethod
    def compute_key(self, seed: bytes, *, vin: str | None = None) -> bytes:
        """Return the key bytes for the given seed.

        Implementations should be pure functions (no I/O) wherever possible
        so they can be deterministically tested and cached.
        """


class MockSeedKeyProvider(AbstractSeedKeyProvider):
    """⚠️  NOT_FOR_PRODUCTION — deterministic XOR-based stub.

    Pairs with `bmw_ecu.mocks.ecu_simulator.MockEcu` which expects the same
    transformation. Lets the full security-access dance be unit-tested
    end-to-end without any real crypto.
    """

    ecu_family = "MOCK"
    security_level = 0x01

    _XOR_MASK: Final[bytes] = bytes(range(0xA0, 0xA0 + 32))  # 32-byte rolling mask

    def compute_key(self, seed: bytes, *, vin: str | None = None) -> bytes:
        if len(seed) == 0:
            raise ValueError("Empty seed")
        mask = self._XOR_MASK[: len(seed)]
        return bytes(s ^ m for s, m in zip(seed, mask))


class UnavailableSeedKeyProvider(AbstractSeedKeyProvider):
    """Safe placeholder for a real ECU family with no licensed provider.

    It carries the correct family + security level (so connect/seed-request
    behaviour is realistic) but REFUSES to invent a key. This is what keeps
    us from silently sending a wrong key to a real immobiliser.
    """

    def __init__(self, family: str, security_level: int = 0x01) -> None:
        self.ecu_family = family
        self.security_level = security_level

    def compute_key(self, seed: bytes, *, vin: str | None = None) -> bytes:
        raise SeedKeyUnavailable(
            f"No licensed seed-key provider registered for ECU family "
            f"'{self.ecu_family}'. Install one and register it via "
            f"bmw_ecu.uds.register_seed_key_provider(...). Refusing to "
            f"compute a fake key against real hardware."
        )


# --- Provider registry ------------------------------------------------------
# Real, licensed providers self-register here at startup (typically from a
# private backend module named in BMW_ECU_SEEDKEY_BACKEND). Nothing
# proprietary ships in this repo.
_REGISTRY: dict[str, AbstractSeedKeyProvider] = {}
_BACKEND_LOADED = False


def register_seed_key_provider(provider: AbstractSeedKeyProvider, *,
                               family: Optional[str] = None) -> None:
    """Register a licensed provider for an ECU family (e.g. 'FEM', 'MEVD17')."""
    fam = (family or getattr(provider, "ecu_family", "") or "").upper()
    if not fam:
        raise ValueError("Provider has no ecu_family and none was supplied")
    _REGISTRY[fam] = provider


def get_seed_key_provider(family: str) -> Optional[AbstractSeedKeyProvider]:
    return _REGISTRY.get((family or "").upper())


def load_backend_from_env() -> None:
    """Import the private seed-key backend module named in the environment.

    Set BMW_ECU_SEEDKEY_BACKEND=your_package.seedkey_backend ; that module is
    expected to call register_seed_key_provider(...) at import time. Idempotent
    and never raises — a missing/broken backend just leaves the registry empty
    so we fall back to UnavailableSeedKeyProvider (loud refusal on use).
    """
    global _BACKEND_LOADED
    if _BACKEND_LOADED:
        return
    _BACKEND_LOADED = True
    mod = os.environ.get("BMW_ECU_SEEDKEY_BACKEND", "").strip()
    if not mod:
        return
    try:
        importlib.import_module(mod)
    except Exception:  # noqa: BLE001 — never let a backend import kill a request
        pass


def resolve_seed_key_provider(*, family: str = "", security_level: int = 0x01,
                              simulator: bool = False
                              ) -> AbstractSeedKeyProvider:
    """Pick the right provider for this run.

      • simulator ON  → MockSeedKeyProvider (pairs with MockEcu).
      • real hardware → a registered licensed provider for `family`, else an
        UnavailableSeedKeyProvider that refuses to fake a key.
    """
    if simulator:
        return MockSeedKeyProvider()
    load_backend_from_env()
    provider = get_seed_key_provider(family)
    if provider is not None:
        return provider
    return UnavailableSeedKeyProvider(family or "UNKNOWN", security_level)
