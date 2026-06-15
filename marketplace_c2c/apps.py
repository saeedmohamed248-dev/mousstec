"""
C2C Marketplace app — customer marketplace, service requests, disputes, trust.

Phase 2A of Wave 2 (see docs/ARCHITECTURE.md). Owns dispute resolution
and trust/KYC services + their tests. Models (MarketplaceCustomer,
UserVerification, ServiceRequest, TenderOffer, CustomerNotification) still
live in `clients.models.marketplace_c2c` until Phase 2B.
"""
from django.apps import AppConfig


class MarketplaceC2CConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'marketplace_c2c'
    verbose_name = 'C2C Marketplace (Customers, Tenders, Disputes)'
