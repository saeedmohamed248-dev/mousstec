"""Checksum helpers used by FlashEngine + PayloadValidator."""
from __future__ import annotations

import zlib


def compute_checksum(payload: bytes, *, algo: str = "crc32") -> int:
    if algo == "crc32":
        return zlib.crc32(payload)
    if algo == "sum16":
        return sum(payload) & 0xFFFF
    if algo == "xor8":
        x = 0
        for b in payload:
            x ^= b
        return x
    raise ValueError(f"Unknown checksum algo: {algo}")
