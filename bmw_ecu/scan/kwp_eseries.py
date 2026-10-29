"""Read-only KWP2000 (BMW-FAST) services for post-2007 E-series / MINI R56.

E-series and R5x MINI ECUs (FRM3, CAS3, MSV/MEVD DME, ...) speak KWP2000 over
D-CAN, not UDS — fault memory is SID 0x18 (not 0x19) and identification is
SID 0x1A (not 0x22 F1xx). Everything here is a READ or the one standard
ClearDiagnosticInformation; nothing writes coding, flash or immobiliser data.

The functions take a ``UdsClient`` only because it already gives us the
request/response round-trip, 0x78 "response pending" handling and negative
response decoding — those are identical in KWP2000.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..exceptions import UdsNegativeResponse
from ..uds.client import UdsClient
from .module_map import MODULE_CATALOG, EcuModule

# KWP2000 (ISO 14230-3) service IDs used here.
SID_READ_ECU_IDENT = 0x1A
SID_READ_DTC_BY_STATUS = 0x18
SID_CLEAR_DTC = 0x14

IDENT_BMW = 0x80   # BMW ident record (part number, HW/SW, supplier, ...)
IDENT_VIN = 0x90   # ISO 14230-3 identificationOption "VIN" — not every ECU has it

# Modules worth probing on a MINI R56 / post-2007 E-series D-CAN bus. These are
# CANDIDATES the scan tests, never asserted: whoever answers is what's there.
R56_CANDIDATE_CODES: tuple[str, ...] = (
    "jbe", "acsm", "dme", "egs", "dsc", "cas", "eps", "kombi", "rdc", "frm", "ihka",
)


def r56_candidates() -> list[EcuModule]:
    return [MODULE_CATALOG[c] for c in R56_CANDIDATE_CODES]


@dataclass
class EcuIdent:
    address: int
    raw: bytes
    strings: list[str] = field(default_factory=list)  # printable runs (part/SW numbers)

    def to_dict(self) -> dict:
        return {"address": f"0x{self.address:02X}", "raw": self.raw.hex(" "),
                "strings": self.strings}


@dataclass(frozen=True)
class KwpDtc:
    """One BMW fault-memory entry: 2-byte BMW fault code + status byte.

    BMW E-series fault codes are manufacturer codes shown in hex ("9CB2"),
    not SAE P/C/B/U codes, so we keep them as-is. The text for each code
    lives in the ECU's EDIABAS description file — not guessed here.
    """
    code: int
    status: int

    @property
    def code_hex(self) -> str:
        return f"{self.code:04X}"

    def to_dict(self) -> dict:
        return {"code": self.code_hex, "status": f"0x{self.status:02X}"}


def printable_runs(data: bytes, min_len: int = 4) -> list[str]:
    """ASCII runs inside an ident record (BMW part / SW numbers are ASCII)."""
    return [m.decode("ascii") for m in re.findall(rb"[\x20-\x7E]{%d,}" % min_len, data)]


def _expect_positive(resp: bytes, sid: int) -> bytes:
    if not resp or resp[0] != (sid | 0x40):
        raise ValueError(f"unexpected reply to SID 0x{sid:02X}: {resp.hex(' ')}")
    return resp


async def read_ident(client: UdsClient, *, timeout: float = 2.0) -> EcuIdent:
    """SID 0x1A 0x80 — the ECU's BMW ident record. Answers ⇒ the module is there."""
    resp = _expect_positive(
        await client.raw_request(bytes([SID_READ_ECU_IDENT, IDENT_BMW]), timeout=timeout),
        SID_READ_ECU_IDENT)
    body = resp[2:]
    return EcuIdent(address=client.ecu_addr, raw=body, strings=printable_runs(body))


def parse_vin(resp: bytes) -> Optional[str]:
    """Pull a 17-char VIN out of a 0x5A 0x90 reply; None if it isn't one."""
    m = re.search(rb"[A-HJ-NPR-Z0-9]{17}", resp[2:])
    return m.group(0).decode("ascii") if m else None


async def read_vin(client: UdsClient, *, timeout: float = 2.0) -> Optional[str]:
    """SID 0x1A 0x90. Returns None when the ECU doesn't support it (NRC)."""
    try:
        resp = await client.raw_request(bytes([SID_READ_ECU_IDENT, IDENT_VIN]),
                                        timeout=timeout)
    except UdsNegativeResponse:
        return None
    if not resp or resp[0] != (SID_READ_ECU_IDENT | 0x40):
        return None
    return parse_vin(resp)


def parse_dtcs(resp: bytes) -> list[KwpDtc]:
    """Decode a 0x58 reply: [0x58, count, (code_hi, code_lo, status) * count]."""
    _expect_positive(resp, SID_READ_DTC_BY_STATUS)
    if len(resp) < 2:
        return []
    count = resp[1]
    entries = resp[2:]
    out: list[KwpDtc] = []
    for i in range(count):
        chunk = entries[i * 3:i * 3 + 3]
        if len(chunk) < 3:
            break  # truncated reply — keep what's complete
        out.append(KwpDtc(code=(chunk[0] << 8) | chunk[1], status=chunk[2]))
    return out


async def read_dtcs(client: UdsClient, *, timeout: float = 3.0) -> list[KwpDtc]:
    """SID 0x18 0x02 FF FF — every stored fault, all groups."""
    resp = await client.raw_request(
        bytes([SID_READ_DTC_BY_STATUS, 0x02, 0xFF, 0xFF]), timeout=timeout)
    return parse_dtcs(resp)


async def clear_dtcs(client: UdsClient, *, timeout: float = 3.0) -> None:
    """SID 0x14 FF FF — clear the module's fault memory (no coding touched)."""
    _expect_positive(
        await client.raw_request(bytes([SID_CLEAR_DTC, 0xFF, 0xFF]), timeout=timeout),
        SID_CLEAR_DTC)
