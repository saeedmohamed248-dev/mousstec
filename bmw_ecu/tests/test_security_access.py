"""End-to-end Security Access against MockEcu + MockSeedKeyProvider."""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.mocks import MockEcu, MockTransport
from bmw_ecu.uds import MockSeedKeyProvider, SecurityAccess, UdsClient
from bmw_ecu.uds.services import DiagSession


class SecurityAccessTests(unittest.TestCase):
    def test_seed_key_handshake(self) -> None:
        async def run() -> None:
            ecu = MockEcu()
            transport = MockTransport(ecu)
            await transport.open()
            client = UdsClient(transport, ecu_addr=0x40, session_name="test")
            await client.diagnostic_session_control(DiagSession.EXTENDED)
            sa = SecurityAccess(client, MockSeedKeyProvider())
            await sa.unlock(vin=ecu.vin)
            # Now an ISN read must succeed.
            isn = await client.read_data_by_identifier(0xF1A0)
            self.assertEqual(len(isn), 32)

        asyncio.run(run())

    def test_isn_locked_before_unlock(self) -> None:
        from bmw_ecu.exceptions import UdsNegativeResponse

        async def run() -> None:
            ecu = MockEcu()
            transport = MockTransport(ecu)
            await transport.open()
            client = UdsClient(transport, ecu_addr=0x40)
            await client.diagnostic_session_control(DiagSession.EXTENDED)
            with self.assertRaises(UdsNegativeResponse) as ctx:
                await client.read_data_by_identifier(0xF1A0)
            self.assertEqual(ctx.exception.nrc, 0x33)  # SECURITY_ACCESS_DENIED

        asyncio.run(run())
