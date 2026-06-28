"""Thin wrapper isolating Django ORM access for the hardware catalog.

Kept at the package root (mirroring _db_diagrams) so circular imports
between models.py and autodetect.* never form. The dynamic hardware
catalog calls this DB-first, then falls back to its bundled seed.
"""
from __future__ import annotations


def fetch_hardware_profile(hardware_id: str):
    """Return a HardwareProfile rebuilt from the DB row, or None.

    Never raises: a missing table (queried outside a tenant request or
    before migrate) degrades to None so the bundled catalog can answer.
    """
    from django.db.utils import OperationalError, ProgrammingError

    from .autodetect.ecu_hardware_catalog import BenchPinout, HardwareProfile
    from .models import EcuHardwareProfile

    try:
        row = EcuHardwareProfile.objects.get(hardware_id=hardware_id)
    except EcuHardwareProfile.DoesNotExist:
        return None
    except (ProgrammingError, OperationalError):
        return None

    pinout = BenchPinout(
        power_pin=row.power_pin,
        ground_pin=row.ground_pin,
        boot_pin=row.boot_pin,
        can_h_pin=row.can_h_pin,
        can_l_pin=row.can_l_pin,
        k_line_pin=row.k_line_pin,
        pcb_image_url=row.pcb_image_url,
        boot_image_url=row.boot_image_url,
        callouts=row.callouts or [],
    )
    return HardwareProfile(
        hardware_id=row.hardware_id,
        ecu_name=row.ecu_name,
        board_revision=row.board_revision,
        family=row.family,
        protocol=row.protocol,
        pinout=pinout,
        physical_steps_ar=list(row.physical_steps_ar or []),
        physical_steps_en=list(row.physical_steps_en or []),
        notes=row.notes,
        verified=row.verified,
    )
