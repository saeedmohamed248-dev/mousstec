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
