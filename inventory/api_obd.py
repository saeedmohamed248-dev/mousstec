"""
Shim — the canonical location is now `workshop.api_obd`.

Kept so `from .api_obd import ReceiveOBDDataView` in inventory/urls.py:9
keeps working without edits. See docs/ARCHITECTURE.md Wave 2 Phase 2A.
"""
from workshop.api_obd import *  # noqa: F401, F403
from workshop.api_obd import ReceiveOBDDataView  # noqa: F401
