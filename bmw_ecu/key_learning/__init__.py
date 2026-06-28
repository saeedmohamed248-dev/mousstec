"""Bench Key Programming + CAS3/CAS3+ Recovery.

Hardware-less workflow: the orchestrator pushes interactive prompts to
the chatbot UI ("connect CAN-H to pin 18 of the FEM"), waits for the
technician to confirm the wiring, then drives the underlying UDS or
EEPROM-dump path through the Smart Harness abstraction. Every step is
serialised in a state machine so a tab refresh / backend restart can
resume from the last confirmed wiring state.

Modules
-------
profiles        — per-ECU constants (pinout, bus speed, ISN region, etc.)
smart_harness   — ABC for the bench wiring; production talks to the
                  Mousstec Breakout Box, tests use MockSmartHarness.
eeprom_dump     — parse & validate raw EEPROM dumps (M35080 / 95128).
isn_extraction  — pull the 32-byte ISN out of a parsed dump.
key_generation  — allocate a free key slot + emit fob data.
bench_orchestrator — the wizard state machine + chatbot prompts.
"""
from __future__ import annotations

from .bench_orchestrator import (
    BenchOrchestrator,
    BenchState,
    BenchData,
    BenchEvent,
    BenchPrompt,
    IllegalBenchTransition,
)
from .profiles import (
    KEY_LEARNING_PROFILES,
    KeyLearningProfile,
    ModuleFamily,
    get_profile,
)
from .smart_harness import (
    AbstractSmartHarness,
    HarnessConnection,
    HarnessFailure,
    MockSmartHarness,
)
from .eeprom_dump import (
    EepromDump,
    EepromParseError,
    parse_dump,
)
from .isn_extraction import (
    extract_isn_from_dump,
)
from .key_generation import (
    AbstractKeyGenBackend,
    KeyFob,
    KeyGenUnavailable,
    KeySlotState,
    LocalStubKeyGen,
    allocate_key_slot,
    generate_key_fob,
    generate_working_key_fob,
    KeyAllocationError,
    register_keygen_backend,
    resolve_keygen_backend,
)

__all__ = [
    "BenchOrchestrator",
    "BenchState",
    "BenchData",
    "BenchEvent",
    "BenchPrompt",
    "IllegalBenchTransition",
    "KEY_LEARNING_PROFILES",
    "KeyLearningProfile",
    "ModuleFamily",
    "get_profile",
    "AbstractSmartHarness",
    "HarnessConnection",
    "HarnessFailure",
    "MockSmartHarness",
    "EepromDump",
    "EepromParseError",
    "parse_dump",
    "extract_isn_from_dump",
    "KeyFob",
    "KeySlotState",
    "allocate_key_slot",
    "generate_key_fob",
    "generate_working_key_fob",
    "KeyAllocationError",
    "AbstractKeyGenBackend",
    "LocalStubKeyGen",
    "KeyGenUnavailable",
    "register_keygen_backend",
    "resolve_keygen_backend",
]
