"""
🛍️ Design Store — marketplace customer AI-design endpoints.

All 14 design-store endpoints (browse, buy, generate, regenerate, refine,
download, watermark, send-to-print, chat history, send-to-marketplace).
Heavy AI lifting is delegated to ``_ai_pipeline._run_marketplace_image_pipeline``
so C1/C2/C3 all share the unified Brand + Smart Router + Composite +
Quality-Gate pipeline.

Extracted from ``_legacy.py`` (Step 4 of the incremental split). The
package facade (``clients/views/__init__.py``) preserves the public URL
surface — ``erp_core/urls.py`` continues to reference ``client_views.<name>``
unchanged.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie

from clients.models import (
    CustomerDesign,
    DesignPackage,
    DesignPurchase,
    MarketplaceCustomer,
)

from .._ai_pipeline import (
    _composite_brand_logo,
    _persist_remote_image,
    _resolve_brand_context,
    _resolve_quality_size,
    _run_marketplace_image_pipeline,
    _upscale_local_image,
)
from .._shared import (
    _build_customer_topup_cards,
    _marketplace_auth,
)

logger = logging.getLogger('mouss_tec_core')



# 🔀 Feature submodules — re-exported below so URL conf
# (`from clients.views.design_store_views import X`) keeps working.
from .navigation import *  # noqa: F401, F403
from .generate import *  # noqa: F401, F403
from .delivery import *  # noqa: F401, F403

# Underscore-prefixed helpers (skipped by `import *`).
from .navigation import _enforce_printing_sector  # noqa: F401
