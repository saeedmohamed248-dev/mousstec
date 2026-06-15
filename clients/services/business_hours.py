"""
Shim — the canonical location is now `support.services.business_hours`.

Kept so callers in clients/views/chat_views.py and the docstring example
in clients/services/business_hours.py keep working without edits.
See docs/ARCHITECTURE.md Wave 2 Phase 2A.
"""
from support.services.business_hours import *  # noqa: F401, F403
