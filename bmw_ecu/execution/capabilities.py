"""Workshop capabilities — what tools the technician has on hand right now.

The Manager intersects ECU requirements with these capabilities to choose
a strategy. Loaded from the tenant config; defaults are conservative.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkshopCapabilities:
    has_enet_cable: bool = True
    has_kdcan_cable: bool = False
    has_smart_breakout_box: bool = False     # Mousstec hardware
    has_bdm_probe: bool = False              # Xprog / KESS / Trasdata
    technician_skill_level: int = 2          # 1=junior, 2=mid, 3=senior, 4=master
    online_supervisor_available: bool = False

    def can_run_software_only(self) -> bool:
        return self.has_enet_cable or self.has_kdcan_cable

    def can_run_hardware_automation(self) -> bool:
        return self.has_smart_breakout_box

    def can_run_interactive_guided(self) -> bool:
        # Need at least one transport to read+inject the manually-extracted
        # ISN back into the FEM, and a tech competent enough to follow pinout.
        return (self.has_enet_cable or self.has_kdcan_cable) and self.technician_skill_level >= 2
