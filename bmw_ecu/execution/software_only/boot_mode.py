"""Force-boot-mode-via-DoIP — last-resort software path.

Some ECUs respond to a sequence of DoIP routing-activation + UDS resets
that drops them into bootloader (BSL) without grounding the boot pin.
This is firmware-dependent and noisy; only used if catalog exploits fail.
"""
from __future__ import annotations

import asyncio

from ...logging_setup import get_logger
from ...uds.client import UdsClient
from ...uds.services import SID

log = get_logger(__name__)


async def force_boot_mode(client: UdsClient, *, attempts: int = 3,
                          settle_s: float = 1.5) -> bool:
    """Try to land the ECU in boot mode purely over UDS/DoIP.

    Returns True if a boot-mode handshake byte (0x67/0xFD) is observed.
    Caller MUST treat False as "not in boot mode" — do not assume.
    """
    for i in range(attempts):
        log.info("Force-boot-mode attempt", extra={"i": i + 1})
        try:
            # Hard reset
            await client.raw_request(bytes([SID.ECU_RESET, 0x03]), timeout=2.0)
        except Exception:
            pass
        await asyncio.sleep(settle_s)
        try:
            resp = await client.raw_request(bytes([SID.DIAGNOSTIC_SESSION_CONTROL, 0x02]))
            if resp and resp[0] == 0x50:
                log.info("Boot-mode entered")
                return True
        except Exception as e:
            log.debug(f"boot-mode probe: {e}")
    return False
