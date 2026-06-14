from django.db import models, transaction, connection
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from simple_history.models import HistoricalRecords
from django.utils.translation import gettext_lazy as _
from decimal import Decimal
from datetime import timedelta
from django.contrib.auth.models import User
from django.db.models import F, Sum, Q, ExpressionWrapper, DecimalField

import uuid
import logging

logger = logging.getLogger('mouss_tec_core')

# 🔀 Domain submodules — re-exported below so external imports
# (`from inventory.models import X`) keep working unchanged.
from .organization import *  # noqa: F401, F403
from .catalog import *  # noqa: F401, F403
from .customers import *  # noqa: F401, F403
from .finance import *  # noqa: F401, F403
from .invoices import *  # noqa: F401, F403
from .operations import *  # noqa: F401, F403
from .diagnostics import *  # noqa: F401, F403

# Underscore-prefixed helpers that `import *` skips but historical
# migrations reference by full path.
from .diagnostics import _diag_photo_upload_path  # noqa: F401
