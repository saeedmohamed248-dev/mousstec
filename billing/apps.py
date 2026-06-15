"""
Billing app — payment gateway integration, manual receipts, invoice processing.

Phase 2A of Wave 2 (see docs/ARCHITECTURE.md). Owns the Paymob HMAC
verification + iframe creation, and the suite of payment-flow tests.
Models (PlatformInvoice, ManualPaymentReceipt, AIBonusGrant,
TenantDesignTopUp) still live in `clients.models.billing` until
Phase 2B.
"""
from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'billing'
    verbose_name = 'Billing & Payment Processing'
