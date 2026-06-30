"""Runtime hardware/simulator mode lock.

The bench policy: a server told to require hardware must NEVER fall back to
the simulator, even if a stale BMW_ECU_SIMULATOR is left in the environment.
Reliability over convenience — we don't serve fake ECU data on a real bench.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from bmw_ecu.api.runtime_mode import require_hardware, simulator_enabled


def _env(**kw):
    return mock.patch.dict(os.environ, kw, clear=True)


class RuntimeModeTests(unittest.TestCase):
    def test_default_is_real_mode(self) -> None:
        with _env():
            self.assertFalse(simulator_enabled())
            self.assertFalse(require_hardware())

    def test_simulator_opt_in_when_unlocked(self) -> None:
        with _env(BMW_ECU_SIMULATOR="1"):
            self.assertTrue(simulator_enabled())

    def test_truthy_variants(self) -> None:
        for v in ("1", "true", "YES", "On"):
            with _env(BMW_ECU_SIMULATOR=v):
                self.assertTrue(simulator_enabled(), v)
        for v in ("0", "false", "", "no"):
            with _env(BMW_ECU_SIMULATOR=v):
                self.assertFalse(simulator_enabled(), v)

    def test_hardware_lock_forces_real_even_with_stale_sim_flag(self) -> None:
        # The whole point: lock WINS over a leftover simulator flag.
        with _env(BMW_ECU_SIMULATOR="1", BMW_ECU_REQUIRE_HARDWARE="1"):
            self.assertTrue(require_hardware())
            self.assertFalse(simulator_enabled())

    def test_hardware_lock_alone_is_real(self) -> None:
        with _env(BMW_ECU_REQUIRE_HARDWARE="true"):
            self.assertFalse(simulator_enabled())


if __name__ == "__main__":
    unittest.main()
