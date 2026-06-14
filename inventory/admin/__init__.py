from django.contrib import admin
from decimal import Decimal
from django.urls import reverse
from django.shortcuts import redirect
from django.utils.safestring import mark_safe
from django.utils.html import format_html
from django.db.models import Sum, F, Max, Avg, Count
from django.utils import timezone
from datetime import timedelta
import datetime
import json
import urllib.parse
from django.contrib import messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from import_export.admin import ImportExportModelAdmin 
from django.utils.translation import gettext_lazy as _ 
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django_tenants.utils import schema_context 

# 🟢 استدعاء الجداول الأساسية للمنظومة التشغيلية
from ..models import (Branch, Product, Inventory, PurchaseInvoice, SaleInvoice,
                     PurchaseInvoiceItem, SaleInvoiceItem, StockTransfer,
                     Treasury, ExpenseCategory, FinancialTransaction, EmployeeProfile,
                     Customer, Vendor, Vehicle,
                     ServiceCatalog, SaleInvoiceServiceItem, VehicleInspection,
                     MaintenanceContract,
                     AuditLog, ChartOfAccount, AccountingEntry,
                     InventoryMovement, StockAlert, ImportSession,
                     ScrapDismantlingJob, ScrapDismantlingYield,
                     B2BListingRequest)

# استدعاء جداول الإمبراطورية لربط سوق التجار المركزي (B2B)
try:
    from clients.models import GlobalB2BMarketplace, Client
except ImportError:
    GlobalB2BMarketplace = None

import logging
logger = logging.getLogger('mouss_tec_core')


# Base mixins live in their own submodule and are re-exported here so the
# rest of this file (and any external imports) see them unchanged.
from .mixins import *  # noqa: F401, F403

from .organization import *  # noqa: F401, F403
from .customers import *  # noqa: F401, F403
from .catalog import *  # noqa: F401, F403

from .invoices import *  # noqa: F401, F403
from .finance import *  # noqa: F401, F403
from .dashboard import *  # noqa: F401, F403

from .audit import *  # noqa: F401, F403
from .accounting import *  # noqa: F401, F403
from .b2b import *  # noqa: F401, F403
