"""UDS Security Access (SID 0x27) orchestrator.

Three-step dance:
    1. Request seed       (sub-function = level, odd: 0x01, 0x03, ...)
    2. Compute key        (delegated to a SeedKeyProvider)
    3. Send key           (sub-function = level + 1, even)
"""
from __future__ import annotations

from ..exceptions import SecurityAccessDenied, UdsNegativeResponse
from ..logging_setup import get_logger
from .client import UdsClient
from .seed_key_providers import AbstractSeedKeyProvider
from .services import SID

log = get_logger(__name__)


class SecurityAccess:
    def __init__(self, client: UdsClient, provider: AbstractSeedKeyProvider) -> None:
        self.client = client
        self.provider = provider

    async def unlock(self, *, vin: str | None = None) -> None:
        lvl = self.provider.security_level

        # Step 1: request seed
        seed_resp = await self.client.raw_request(bytes([SID.SECURITY_ACCESS, lvl]))
        # response: [0x67, lvl, seed...]
        if len(seed_resp) < 3 or seed_resp[0] != 0x67 or seed_resp[1] != lvl:
            raise SecurityAccessDenied(f"Bad seed response: {seed_resp.hex()}")
        seed = seed_resp[2:]
        log.info("Seed received", extra={"level": lvl, "len": len(seed)})

        # Step 2: compute key (pure function, off the wire)
        if all(b == 0 for b in seed):
            # ECU is already unlocked — spec says it returns all zeros.
            log.info("ECU already unlocked")
            return
        key = self.provider.compute_key(seed, vin=vin)

        # Step 3: send key
        try:
            ack = await self.client.raw_request(bytes([SID.SECURITY_ACCESS, lvl + 1]) + key)
        except UdsNegativeResponse as e:
            raise SecurityAccessDenied(
                f"Key rejected: NRC=0x{e.nrc:02X}", nrc=e.nrc,
            ) from e
        if len(ack) < 2 or ack[0] != 0x67 or ack[1] != lvl + 1:
            raise SecurityAccessDenied(f"Bad key ack: {ack.hex()}")
        log.info("Security access granted", extra={"level": lvl})
