"""Seed-Key provider registry + safe resolution (#1).

The whole point: on a real car we must NEVER send a fabricated key to an
immobiliser. So when no licensed provider is registered for the ECU family,
resolution yields an UnavailableSeedKeyProvider that REFUSES to compute a key
(raising SeedKeyUnavailable) rather than silently faking one. On the
simulator we still get the Mock so the test bench keeps working.
"""
from __future__ import annotations

import unittest

from bmw_ecu.uds import (
    MockSeedKeyProvider,
    SeedKeyUnavailable,
    UnavailableSeedKeyProvider,
    get_seed_key_provider,
    register_seed_key_provider,
    resolve_seed_key_provider,
)
from bmw_ecu.uds import seed_key_providers as skp


class _FakeLicensedProvider(MockSeedKeyProvider):
    """Stand-in for a real licensed provider in tests."""
    ecu_family = "FEM"
    security_level = 0x05


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        # Isolate the module-level registry between tests.
        self._saved = dict(skp._REGISTRY)
        skp._REGISTRY.clear()

    def tearDown(self) -> None:
        skp._REGISTRY.clear()
        skp._REGISTRY.update(self._saved)

    def test_simulator_always_gets_mock(self) -> None:
        p = resolve_seed_key_provider(family="FEM", security_level=0x05,
                                      simulator=True)
        self.assertIsInstance(p, MockSeedKeyProvider)

    def test_real_hw_without_provider_is_unavailable(self) -> None:
        p = resolve_seed_key_provider(family="FEM", security_level=0x05,
                                      simulator=False)
        self.assertIsInstance(p, UnavailableSeedKeyProvider)
        self.assertEqual(p.ecu_family, "FEM")
        self.assertEqual(p.security_level, 0x05)

    def test_unavailable_refuses_to_fake_a_key(self) -> None:
        p = UnavailableSeedKeyProvider("FEM", 0x05)
        with self.assertRaises(SeedKeyUnavailable):
            p.compute_key(b"\x01\x02\x03\x04", vin="WBA0")

    def test_registered_provider_is_used_on_real_hw(self) -> None:
        register_seed_key_provider(_FakeLicensedProvider())
        self.assertIsInstance(get_seed_key_provider("FEM"),
                              _FakeLicensedProvider)
        p = resolve_seed_key_provider(family="FEM", security_level=0x05,
                                      simulator=False)
        self.assertIsInstance(p, _FakeLicensedProvider)
        # And it actually computes a key (doesn't raise).
        self.assertTrue(p.compute_key(b"\x01\x02\x03\x04"))

    def test_register_is_case_insensitive_on_family(self) -> None:
        register_seed_key_provider(_FakeLicensedProvider(), family="fem")
        self.assertIsNotNone(get_seed_key_provider("FEM"))


class ProfileWiringTests(unittest.TestCase):
    def test_profiles_carry_family_and_level(self) -> None:
        from bmw_ecu.execution.ecu_profiles import KNOWN_PROFILES
        fem = KNOWN_PROFILES["FEM_F30"]
        self.assertEqual(fem.seed_key_family, "FEM")
        self.assertEqual(fem.isn_security_level, 0x05)
        dme = KNOWN_PROFILES["MEVD17_2_9"]
        self.assertEqual(dme.seed_key_family, "MEVD17")

    def test_mini_n18_routes_through_mevd17_family(self) -> None:
        # Mini Cooper S R56 (N18) shares the MEVD17 seed-key family + level
        # 0x01 with the N20 DME, so the SAME licensed provider unlocks both.
        from bmw_ecu.execution.ecu_profiles import KNOWN_PROFILES
        n18 = KNOWN_PROFILES["MEVD17_2_2_N18"]
        self.assertEqual(n18.seed_key_family, "MEVD17")
        self.assertEqual(n18.isn_security_level, 0x01)
        self.assertEqual(n18.engine, "N18")
        self.assertIn("R56", n18.chassis)
        # No fabricated bench pin shipped for the N18 board.
        self.assertIsNone(n18.boot_pin)

    def test_mini_n18_isn_refuses_fake_key_on_real_hw(self) -> None:
        # Without a licensed MEVD17 provider, ISN read on a real Mini must
        # refuse — never fake a key into the immobiliser.
        from bmw_ecu.execution.ecu_profiles import KNOWN_PROFILES
        n18 = KNOWN_PROFILES["MEVD17_2_2_N18"]
        p = resolve_seed_key_provider(
            family=n18.seed_key_family,
            security_level=n18.isn_security_level,
            simulator=False,
        )
        self.assertIsInstance(p, UnavailableSeedKeyProvider)
        with self.assertRaises(SeedKeyUnavailable):
            p.compute_key(b"\x11\x22\x33\x44")


if __name__ == "__main__":
    unittest.main()
