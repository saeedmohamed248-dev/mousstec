"""
clients.views — package facade.

Historically a single 4936-line module. Being split incrementally into
focused submodules (auth, subscription, webhook, b2b, admin, ai,
marketplace, design). During the migration this package re-exports every
public name from the original module so that `urls.py` (which references
`client_views.<name>`) keeps working unchanged.

Once every name has been moved to its dedicated submodule, `_legacy` is
deleted.
"""
from ._legacy import *  # noqa: F401,F403

# Explicit re-exports for Phase 5 Brand Memory endpoints — the wildcard
# above did not surface these names reliably under Daphne boot, causing
# AttributeError in erp_core/urls.py and a 502.
from ._legacy import (  # noqa: F401
    brand_profile_view,
    brand_profile_delete_logo,
    brand_profile_page,
)
from ._legacy import brand_profile_view, brand_profile_delete_logo, brand_profile_page

# 💬 Phase N — Conversational Design Builder endpoints (N.3)
from .design_chat_views import (  # noqa: F401
    design_chat_start,
    design_chat_message,
    design_chat_undo,
    design_chat_finalize,
    design_chat_state,
    design_chat_page,
)