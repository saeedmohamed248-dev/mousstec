"""FA (Fahrzeugauftrag / Vehicle Order) parser.

BMW VO is an ASCII string of order codes ("S205A", "S322A", ...) listing
every option the car was built with. Modules use the active VO to know
which features to enable.

This parser is intentionally lenient — it accepts the canonical hyphen-
separated format and the older space-separated workshop format.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_FA_TOKEN = re.compile(r"\b([SP]?\d{3}[A-Z]?)\b")


@dataclass
class VehicleOrder:
    raw: str
    type_code: str = ""             # e.g. "3F30" (F30 chassis)
    options: set[str] = field(default_factory=set)
    e_word: str = ""                # Production date code
    salapa: set[str] = field(default_factory=set)

    def has(self, option: str) -> bool:
        return option.upper() in self.options


def parse_fa(raw: str) -> VehicleOrder:
    raw = raw.strip()
    vo = VehicleOrder(raw=raw)
    parts = re.split(r"[-\s,;]+", raw)
    if parts and re.match(r"^[A-Z0-9]{4,7}$", parts[0]):
        vo.type_code = parts[0]
        parts = parts[1:]
    for p in parts:
        if not p:
            continue
        m = _FA_TOKEN.match(p)
        if m:
            tok = m.group(1).upper()
            vo.options.add(tok)
            vo.salapa.add(tok)
        elif re.match(r"^\d{4}$", p):
            vo.e_word = p
    return vo
