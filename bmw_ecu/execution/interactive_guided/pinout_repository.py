"""Pinout diagram lookup.

Backed by the EcuPinoutDiagram model (added in 0002 migration). Falls
back to a small bundled JSON if the DB is empty (so tests + demo work).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from asgiref.sync import sync_to_async


@dataclass(frozen=True)
class PinoutDiagram:
    ecu_name: str
    image_url: str
    callouts: list[dict]                # [{"pin": 18, "label": "BOOT", "color": "red"}, ...]


_BUNDLED: dict[str, PinoutDiagram] = {
    "FEM_F30": PinoutDiagram(
        ecu_name="FEM_F30",
        image_url="/static/bmw_ecu/pinouts/fem_f30.svg",
        callouts=[
            {"pin": 1, "label": "12V Supply (KL30)", "color": "red"},
            {"pin": 2, "label": "GND (KL31)", "color": "black"},
            {"pin": 15, "label": "CAN High (PT-CAN)", "color": "orange"},
            {"pin": 16, "label": "CAN Low (PT-CAN)", "color": "brown"},
            {"pin": 18, "label": "BOOT (ground to enter BSL)", "color": "yellow"},
        ],
    ),
    "MEVD17_2_9": PinoutDiagram(
        ecu_name="MEVD17_2_9",
        image_url="/static/bmw_ecu/pinouts/mevd17_29.svg",
        callouts=[
            {"pin": 87, "label": "12V (KL30)", "color": "red"},
            {"pin": 88, "label": "GND", "color": "black"},
            {"pin": 24, "label": "BOOT (BDM probe here)", "color": "yellow"},
            {"pin": 63, "label": "K-Line", "color": "green"},
        ],
    ),
}


class PinoutRepository:
    async def get(self, ecu_name: str) -> Optional[PinoutDiagram]:
        from ..._db_diagrams import fetch_diagram  # lazy import — wired in models commit
        diagram = await sync_to_async(fetch_diagram)(ecu_name)
        if diagram is not None:
            return diagram
        return _BUNDLED.get(ecu_name)
