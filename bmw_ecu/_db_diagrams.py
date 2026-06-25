"""Thin wrapper isolating Django ORM access for the pinout repository.

Kept at the package root (not inside execution/) so circular imports
between models.py and execution.* never form.
"""
from __future__ import annotations

from typing import Optional


def fetch_diagram(ecu_name: str):
    """Return a PinoutDiagram from the DB, or None."""
    from .execution.interactive_guided.pinout_repository import PinoutDiagram
    from .models import EcuPinoutDiagram
    try:
        row = EcuPinoutDiagram.objects.get(ecu_name=ecu_name)
    except EcuPinoutDiagram.DoesNotExist:
        return None
    return PinoutDiagram(
        ecu_name=row.ecu_name, image_url=row.image_url,
        callouts=row.callouts or [],
    )
