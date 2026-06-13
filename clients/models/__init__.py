from django.conf import settings
from django.db import models, transaction
from django_tenants.models import TenantMixin, DomainMixin
from clients.soft_delete import SoftDeleteMixin
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.db.models import F
from datetime import timedelta
from decimal import Decimal
import uuid
import logging

logger = logging.getLogger('mouss_tec_core')


# 🔀 Domain submodules — re-exported below so external imports
# (`from clients.models import X`) keep working unchanged.
from .tenancy import *  # noqa: F401, F403

from .marketplace_c2c import *  # noqa: F401, F403
from .marketplace_c2c import _verification_upload_path  # noqa: F401 — referenced by historical migrations
from .design_store import *  # noqa: F401, F403
from .marketplace_b2b import *  # noqa: F401, F403
from .marketplace_b2b import _validate_warranty_days  # noqa: F401 — referenced by historical migrations
from .monitoring import *  # noqa: F401, F403
from .support import *  # noqa: F401, F403
from .diagnostics import *  # noqa: F401, F403
from .billing import *  # noqa: F401, F403


# OBD device identity & secrets — defined in a separate module for clarity.
# Imported here so Django registers them under the `clients` app.
from clients.obd_device_models import OBDDevice, OBDDeviceNonce  # noqa: E402, F401

