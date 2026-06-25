"""Network Management (OSEK/AUTOSAR-style) wake-up frames.

EPS, FEM, and some peripherals go to sleep on the bus. Before any UDS
session you broadcast NM frames for ~500ms to keep them awake.
"""
from __future__ import annotations

import asyncio

from ..logging_setup import get_logger
from .base import AbstractTransport

log = get_logger(__name__)

# BMW-style NM frame CAN IDs (functional broadcast). Real CAN IDs differ
# per platform; these are placeholders the integrator should override per FA.
DEFAULT_NM_IDS: tuple[int, ...] = (0x5E0, 0x5E1, 0x5F0)


async def wake_modules(transport: AbstractTransport,
                       nm_ids: tuple[int, ...] = DEFAULT_NM_IDS,
                       duration_s: float = 0.5,
                       interval_s: float = 0.02) -> None:
    """Broadcast NM heartbeats for `duration_s` seconds.

    Safe to call before opening a UDS session — no module state is modified.
    """
    log.info("NM wake-up start", extra={"ids": [hex(i) for i in nm_ids], "duration": duration_s})
    loop = asyncio.get_running_loop()
    end = loop.time() + duration_s
    payload = bytes([0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])  # NM alive frame
    while loop.time() < end:
        for nid in nm_ids:
            try:
                await transport.send(nid, payload)
            except Exception as e:  # NM is best-effort; log and continue
                log.warning("NM send failed", extra={"id": hex(nid), "err": str(e)})
        await asyncio.sleep(interval_s)
    log.info("NM wake-up done")
