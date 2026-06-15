"""
Shim — the canonical location is now `design_store.services.design_persistence`.

Kept so clients/tasks.py and any other caller using
`from clients.services.design_persistence import ...` keeps working.
See docs/ARCHITECTURE.md Wave 2 Phase 2A.
"""
from design_store.services.design_persistence import *  # noqa: F401, F403
