"""Pluggable KeyGen backend + stub guard (#3)."""
from __future__ import annotations

import unittest

from bmw_ecu.key_learning import (
    KeyFob,
    KeyGenUnavailable,
    LocalStubKeyGen,
    generate_key_fob,
    generate_working_key_fob,
    register_keygen_backend,
    resolve_keygen_backend,
)
from bmw_ecu.key_learning import key_generation as kg


class _FakeCloudKeyGen(LocalStubKeyGen):
    """Pretends to produce real working keys."""
    produces_real_keys = True

    def generate(self, *, isn, slot_index, family_code, seed=None) -> KeyFob:
        fob = super().generate(isn=isn, slot_index=slot_index,
                               family_code=family_code, seed=seed)
        return KeyFob(family_code=fob.family_code, slot_index=fob.slot_index,
                      fcc_id=fob.fcc_id, payload=fob.payload, real=True)


_ISN = bytes(range(32))


class KeyGenSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = kg._KEYGEN_BACKEND
        kg._KEYGEN_BACKEND = None

    def tearDown(self) -> None:
        kg._KEYGEN_BACKEND = self._saved

    def test_local_stub_payload_is_marked_not_real(self) -> None:
        fob = generate_key_fob(isn=_ISN, slot_index=1, family_code="FEM")
        self.assertFalse(fob.real)
        self.assertEqual(len(fob.payload), 32)

    def test_default_backend_is_stub(self) -> None:
        self.assertIsInstance(resolve_keygen_backend(), LocalStubKeyGen)

    def test_working_key_refuses_stub_by_default(self) -> None:
        with self.assertRaises(KeyGenUnavailable):
            generate_working_key_fob(isn=_ISN, slot_index=1, family_code="FEM")

    def test_working_key_allows_stub_when_opted_in(self) -> None:
        fob = generate_working_key_fob(isn=_ISN, slot_index=1,
                                       family_code="FEM", allow_stub=True)
        self.assertFalse(fob.real)

    def test_registered_real_backend_yields_working_key(self) -> None:
        register_keygen_backend(_FakeCloudKeyGen())
        fob = generate_working_key_fob(isn=_ISN, slot_index=1, family_code="FEM")
        self.assertTrue(fob.real)
        self.assertEqual(len(fob.payload), 32)


if __name__ == "__main__":
    unittest.main()
