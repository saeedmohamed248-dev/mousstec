"""ECU connector pin maps.

One map per ECU profile. Keys match `EcuProfile.name`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PinMap:
    ecu_name: str
    v12: int                  # main 12V supply pin
    gnd: int                  # ground pin
    can_high: int
    can_low: int
    boot: int                 # ground this to force BSL
    kline: int | None = None


PIN_MAPS: dict[str, PinMap] = {
    "FEM_F30": PinMap(
        ecu_name="FEM_F30", v12=1, gnd=2, can_high=15, can_low=16, boot=18,
    ),
    "FEM_F30_POST_2014": PinMap(
        ecu_name="FEM_F30_POST_2014", v12=1, gnd=2, can_high=15, can_low=16, boot=18,
    ),
    "MEVD17_2_9": PinMap(
        ecu_name="MEVD17_2_9", v12=87, gnd=88, can_high=39, can_low=49, boot=24,
        kline=63,
    ),
}


def lookup(ecu_name: str) -> PinMap:
    if ecu_name not in PIN_MAPS:
        raise KeyError(f"No pin map for {ecu_name}")
    return PIN_MAPS[ecu_name]
