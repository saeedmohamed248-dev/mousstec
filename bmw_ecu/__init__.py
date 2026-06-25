"""
bmw_ecu — Mousstec BMW/Mini ECU Diagnostics & Manipulation Subsystem.

Production-grade async UDS/DoIP toolkit for F/G series BMW + Mini.
Built around an "un-brickable" failsafe layer: every write is preceded by
battery + backup checks and followed by checksum verification + auto-rollback.

⚠️  Proprietary Seed-Key + ISN crypto are NOT shipped here. The
    `uds.seed_key_providers.MockSeedKeyProvider` is a deterministic stub for
    unit tests only. Plug a real provider before touching a physical ECU.
"""
from __future__ import annotations

__version__ = "0.1.0-scaffold"
default_app_config = "bmw_ecu.apps.BmwEcuConfig"
