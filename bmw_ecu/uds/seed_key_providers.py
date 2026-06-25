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
from typing import Final


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
