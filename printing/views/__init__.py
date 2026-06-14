"""
🤖 Printing AI Studio Views
==============================
AI-powered design generation and smart watermark for printing tenants.
Gated by TenantSubscription + AILimitTracker.
"""
import logging
import base64
import json
import re
from io import BytesIO
from decimal import Decimal
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db import connection
from django.db.models import Sum, Count, Avg, Q, F
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')



# 🔀 Feature submodules — re-exported below so URL conf keeps working.
from .utils import *  # noqa: F401, F403
from .ai_design import *  # noqa: F401, F403
from .copilot import *  # noqa: F401, F403
from .catalog import *  # noqa: F401, F403
from .ai_diagnostics import *  # noqa: F401, F403
from .studio import *  # noqa: F401, F403
from .finance import *  # noqa: F401, F403

# Underscore-prefixed helpers that `import *` skips.
from .utils import _get_tenant, _check_ai_access, _apply_watermark_to_url, _tenant_brand_context  # noqa: F401
from .copilot import _query_business_data, _get_system_knowledge_printing, _get_live_context_printing  # noqa: F401
