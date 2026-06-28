"""Key slot allocation + key-fob payload generation.

Generates the 32-byte slot payload that the orchestrator burns into
the CAS/FEM/BDC after a successful ISN extraction. The actual
cryptographic mixing of (ISN, slot_seed) is intentionally out of scope
here — we expose a deterministic *contract* so the cloud "Mousstec
KeyGen" can later replace the local stub without touching call sites.

  generate_key_fob(*, isn, slot_index, family_code) -> KeyFob

  KeyFob.payload  → 32 bytes ready to write into the CAS/FEM slot
  KeyFob.fcc_id   → 6-byte fob remote ID for the dealer DB
  KeyFob.created  → naive timestamp for the audit row
"""
from __future__ import annotations

import abc
import hashlib
import importlib
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable, Optional


class KeyAllocationError(RuntimeError):
    """Raised when no slot can be allocated for the requested module."""


class KeyGenUnavailable(RuntimeError):
    """A real working key was requested but no real KeyGen backend exists."""


class KeySlotState(str, Enum):
    FREE = "free"
    OCCUPIED = "occupied"
    DEALER_RESERVED = "dealer_reserved"


# CAS3+ reserves slot 0 for the dealer's master key. Aftermarket keys
# may only be written into slots 1..key_count-1. Other families have no
# such restriction — the dict maps families that need special handling.
_DEALER_RESERVED_SLOTS: dict[str, frozenset[int]] = {
    "CAS3+": frozenset({0}),
}


def allocate_key_slot(*, family_code: str,
                      occupied: Iterable[int],
                      key_count: int,
                      preferred: Optional[int] = None) -> int:
    """Pick the next FREE slot for `family_code`.

    Allocation order:
      1. `preferred` if it's free + not dealer-reserved.
      2. Lowest-numbered free, non-reserved slot.
    """
    occupied_set = set(occupied)
    reserved = _DEALER_RESERVED_SLOTS.get(family_code, frozenset())

    if preferred is not None:
        if preferred in reserved:
            raise KeyAllocationError(
                f"slot {preferred} is dealer-reserved on {family_code}",
            )
        if preferred in occupied_set:
            raise KeyAllocationError(
                f"slot {preferred} is already occupied",
            )
        if preferred < 0 or preferred >= key_count:
            raise KeyAllocationError(
                f"slot {preferred} out of range [0, {key_count})",
            )
        return preferred

    for slot in range(key_count):
        if slot in reserved:
            continue
        if slot not in occupied_set:
            return slot
    raise KeyAllocationError(
        f"no free key slots on {family_code} (all {key_count} slots used)",
    )


@dataclass(frozen=True)
class KeyFob:
    family_code: str
    slot_index: int
    fcc_id: str         # 12-char hex, what dealer software calls the remote ID
    payload: bytes      # 32-byte block ready to flash into the slot
    created: datetime = field(default_factory=datetime.utcnow)
    # False = a deterministic STUB payload (will NOT start a real car). True
    # only when a licensed/cloud KeyGen produced a working immobiliser key.
    real: bool = False


def generate_key_fob(*, isn: bytes, slot_index: int, family_code: str,
                     seed: Optional[bytes] = None) -> KeyFob:
    """Deterministically derive a 32-byte slot payload from
    (ISN, slot_index, family_code, seed).

    The Mousstec cloud KeyGen will eventually replace this with a
    server-signed payload. Until then we generate one locally using
    SHA-256 so:
      • Calls with identical (isn, slot, family, seed) produce identical
        payloads — useful for retry / idempotency.
      • Bench tests can pin the seed and assert exact bytes.
      • Production code without a seed gets a fresh random one so two
        workshops can never accidentally burn the same key on two
        different VINs.
    """
    if not (32 == len(isn)):
        raise KeyAllocationError(f"ISN must be 32 bytes, got {len(isn)}")
    if slot_index < 0 or slot_index > 0xFF:
        raise KeyAllocationError(f"slot_index {slot_index} out of single-byte range")

    seed = seed if seed is not None else secrets.token_bytes(16)
    h = hashlib.sha256()
    h.update(b"MOUSSTEC-KEYGEN-v1")
    h.update(family_code.encode("ascii"))
    h.update(bytes([slot_index]))
    h.update(isn)
    h.update(seed)
    payload = h.digest()                       # 32 bytes
    fcc_id = h.hexdigest()[:12].upper()        # 6 bytes → 12 hex chars

    return KeyFob(
        family_code=family_code,
        slot_index=slot_index,
        fcc_id=fcc_id,
        payload=payload,
        real=False,            # stub payload — not a working immobiliser key
    )


# --- Pluggable KeyGen backend (#3) -----------------------------------------
# The real cryptographic derivation of a working key from (ISN, slot) is
# proprietary and is NOT shipped here. We expose a clean seam so a licensed /
# cloud "Mousstec KeyGen" can be registered without touching call sites; until
# then the local stub returns a deterministic NON-working payload (real=False).
class AbstractKeyGenBackend(abc.ABC):
    produces_real_keys: bool = False

    @abc.abstractmethod
    def generate(self, *, isn: bytes, slot_index: int, family_code: str,
                 seed: Optional[bytes] = None) -> KeyFob: ...


class LocalStubKeyGen(AbstractKeyGenBackend):
    """Deterministic SHA-256 stub. Useful for tests/idempotency only."""
    produces_real_keys = False

    def generate(self, *, isn: bytes, slot_index: int, family_code: str,
                 seed: Optional[bytes] = None) -> KeyFob:
        return generate_key_fob(isn=isn, slot_index=slot_index,
                                family_code=family_code, seed=seed)


_KEYGEN_BACKEND: Optional[AbstractKeyGenBackend] = None
_KEYGEN_LOADED = False


def register_keygen_backend(backend: AbstractKeyGenBackend) -> None:
    global _KEYGEN_BACKEND
    _KEYGEN_BACKEND = backend


def _load_keygen_from_env() -> None:
    global _KEYGEN_LOADED
    if _KEYGEN_LOADED:
        return
    _KEYGEN_LOADED = True
    mod = os.environ.get("BMW_ECU_KEYGEN_BACKEND", "").strip()
    if not mod:
        return
    try:
        importlib.import_module(mod)  # expected to call register_keygen_backend
    except Exception:  # noqa: BLE001
        pass


def resolve_keygen_backend() -> AbstractKeyGenBackend:
    _load_keygen_from_env()
    return _KEYGEN_BACKEND or LocalStubKeyGen()


def generate_working_key_fob(*, isn: bytes, slot_index: int, family_code: str,
                             seed: Optional[bytes] = None,
                             allow_stub: bool = False) -> KeyFob:
    """Produce a key fob that is REQUIRED to be a real working key.

    Refuses to hand back a stub payload (which would never start the car)
    unless `allow_stub=True` is passed deliberately for bench/testing.
    """
    backend = resolve_keygen_backend()
    fob = backend.generate(isn=isn, slot_index=slot_index,
                           family_code=family_code, seed=seed)
    if not fob.real and not allow_stub:
        raise KeyGenUnavailable(
            "No real KeyGen backend registered — refusing to emit a stub key "
            "that won't start the car. Register one via "
            "register_keygen_backend(...) or pass allow_stub=True for bench "
            "testing."
        )
    return fob
