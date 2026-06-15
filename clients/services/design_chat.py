"""
Shim — the canonical location is now `design_store.services.design_chat`.

Kept so callers in clients/tasks.py and the brand design pipeline tests
that reference the old path keep working without edits. See
docs/ARCHITECTURE.md Wave 2 Phase 2A.
"""
from design_store.services.design_chat import *  # noqa: F401, F403
