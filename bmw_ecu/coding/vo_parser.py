"""Vehicle Order (FA) extraction from the live VCM via ENET/DoIP.

Extends `coding.fa_vo.parse_fa` (ASCII) with two production paths:
    - VCM read     → UDS ReadDataByIdentifier against the central
                     vehicle gateway, then auto-detect XML vs hex blob.
    - XML parse    → BMW psdz / E-Sys export shape.
    - Hex parse    → Compact ZUSB/i-step binary token stream.

Three entrypoints, one canonical `VehicleOrder` return shape so the rest
of the coding pipeline (initialize_replaced_module, FDL apply) doesn't
care where the FA came from.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

from ..exceptions import CodingError
from ..logging_setup import get_logger
from ..uds.client import UdsClient
from ..uds.services import BmwDID, DiagSession
from .fa_vo import VehicleOrder, parse_fa as parse_fa_ascii

log = get_logger(__name__)

# BMW VCM/gateway DIDs that return the FA on F/G chassis. Order is most-
# specific first; we fall through until one returns a non-empty payload.
_FA_DIDS: tuple[int, ...] = (
    0xF802,   # FA primary (gateway)
    0xCB00,   # FA backup region
    BmwDID.VIN,  # last-resort fingerprint
)

_XML_SNIFF_MAX_BYTES = 64


# ---------------------------------------------------------------------------
# 1. VCM read
# ---------------------------------------------------------------------------
async def read_vo_from_vcm(client: UdsClient, *,
                           extended_session: bool = True) -> VehicleOrder:
    """Read the live FA from the VCM and parse it.

    The VCM may return the FA in any of three encodings:
        XML  (psdz/E-Sys export, starts with `<?xml` or `<vehicleOrder`)
        ASCII (legacy workshop dumps, hyphen/space separated)
        Hex/binary (ZUSB token stream)
    Auto-detected via magic-byte sniff.

    Raises CodingError if every candidate DID returns empty or all parses
    fail — the caller should fall back to a wizard prompt asking the
    technician to paste the FA manually.
    """
    if extended_session:
        await client.diagnostic_session_control(DiagSession.EXTENDED)

    last_raw: Optional[bytes] = None
    for did in _FA_DIDS:
        try:
            raw = await client.read_data_by_identifier(did)
        except Exception as e:
            log.info("VCM FA read miss", extra={"did": hex(did), "err": str(e)})
            continue
        if not raw:
            continue
        last_raw = raw
        try:
            return _parse_any(raw)
        except CodingError:
            continue

    if last_raw is None:
        raise CodingError("VCM returned no FA on any known DID")
    raise CodingError(
        f"VCM returned {len(last_raw)} bytes but no parser accepted it",
        sample=last_raw[:32].hex(),
    )


def _parse_any(raw: bytes) -> VehicleOrder:
    sniff = raw[:_XML_SNIFF_MAX_BYTES].lstrip()
    if sniff.startswith(b"<"):
        return parse_vo_xml(raw)
    # ASCII heuristic: mostly printable + contains an FA token.
    try:
        text = raw.decode("ascii")
        if _FA_TOKEN_RE.search(text):
            return parse_fa_ascii(text)
    except UnicodeDecodeError:
        pass
    return parse_vo_hex(raw)


_FA_TOKEN_RE = re.compile(r"\b[SP]?\d{3}[A-Z]?\b")


# ---------------------------------------------------------------------------
# 2. XML
# ---------------------------------------------------------------------------
def parse_vo_xml(xml_bytes: bytes) -> VehicleOrder:
    """Parse BMW psdz / E-Sys vehicleOrder XML.

    Expected shape (simplified):
        <vehicleOrder>
            <typeKey>3F30</typeKey>
            <productionDate>0414</productionDate>
            <salapaList>
                <salapa code="205"/>
                <salapa code="322"/>
                ...
            </salapaList>
        </vehicleOrder>

    We accept attribute *or* element-text encoding of `salapa` for
    forwards-compat with E-Sys plugin variations.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise CodingError(f"Malformed FA XML: {e}") from e

    raw_str = xml_bytes.decode("utf-8", errors="replace")
    vo = VehicleOrder(raw=raw_str)

    # Element truth-value is "has children", not "is not None" — must be
    # explicit here.
    type_node = root.find(".//typeKey")
    if type_node is None:
        type_node = root.find(".//type_code")
    if type_node is not None and (type_node.text or "").strip():
        vo.type_code = type_node.text.strip().upper()

    e_node = root.find(".//productionDate")
    if e_node is None:
        e_node = root.find(".//e_word")
    if e_node is not None and (e_node.text or "").strip():
        vo.e_word = e_node.text.strip()

    for node in root.iter():
        # Element form: <salapa code="205"/>  or  <option code="S205A"/>
        code = node.attrib.get("code")
        if code:
            tok = _normalise_token(code)
            if tok:
                vo.options.add(tok)
                vo.salapa.add(tok)
            continue
        # Text form: <salapa>205</salapa>
        if node.tag.lower() in ("salapa", "option"):
            text = (node.text or "").strip()
            tok = _normalise_token(text)
            if tok:
                vo.options.add(tok)
                vo.salapa.add(tok)

    if not vo.options:
        raise CodingError("FA XML parsed but no option tokens recovered")
    return vo


# ---------------------------------------------------------------------------
# 3. Hex / binary
# ---------------------------------------------------------------------------
def parse_vo_hex(blob: bytes) -> VehicleOrder:
    """Parse the compact ZUSB FA token stream.

    Layout (per BMW i-step encoding observed on F/G chassis):
        [0..3]   type code (ASCII, padded with NUL)
        [4..5]   production date (BCD: month, year)
        [6..7]   token count (big-endian uint16)
        [8..]    repeating 2-byte tokens. Each token = (kind << 12) | value
                 kind 0x0 = S-prefixed standard option
                 kind 0x1 = P-prefixed package
                 kind 0xF = sentinel / pad

    Returns a VehicleOrder with options expanded back to canonical "S###" /
    "P###" string form. Lenient — extra trailing bytes are ignored.
    """
    if len(blob) < 8:
        raise CodingError(f"FA hex blob too short: {len(blob)} bytes")
    vo = VehicleOrder(raw=blob.hex())
    vo.type_code = blob[0:4].rstrip(b"\x00").decode("ascii", errors="replace").upper()
    vo.e_word = f"{blob[4]:02X}{blob[5]:02X}"
    count = int.from_bytes(blob[6:8], "big")
    body = blob[8:]
    if len(body) < count * 2:
        raise CodingError(
            f"FA hex declares {count} tokens but body is {len(body)} bytes",
        )
    for i in range(count):
        word = int.from_bytes(body[i * 2:i * 2 + 2], "big")
        kind = (word >> 12) & 0xF
        value = word & 0x0FFF
        if kind == 0xF:
            continue
        prefix = {0x0: "S", 0x1: "P"}.get(kind, "")
        if not prefix:
            continue
        tok = f"{prefix}{value:03X}".upper()
        vo.options.add(tok)
        vo.salapa.add(tok)
    return vo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalise_token(raw: str) -> str:
    """Coerce '205', 'S205A', 's205a' all → 'S205A' canonical form."""
    if not raw:
        return ""
    s = raw.strip().upper()
    if not s:
        return ""
    if s[0] in "SP":
        return s
    if s.isdigit():
        # Assume S-prefixed standard option.
        return f"S{int(s):03d}"
    return s


def options_in_common(a: VehicleOrder,
                      b: VehicleOrder) -> set[str]:
    """Convenience for diff'ing FAs (e.g. donor car vs target car)."""
    return a.options & b.options


def options_diff(donor: VehicleOrder,
                 target: VehicleOrder) -> tuple[set[str], set[str]]:
    """Return (added, removed) going from donor → target FA."""
    return (target.options - donor.options, donor.options - target.options)
