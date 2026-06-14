"""
B2B Marketplace app — blind bidding, parts marketplace, escrow ledger.

Phase 2A of Wave 2 (see docs/ARCHITECTURE.md). Owns the financial-custody
services (escrow), the bid-fitment matcher, and the tests that exercise
the bidding/escrow lifecycles. Models still live in
`clients.models.marketplace_b2b` until Phase 2B can migrate them with
`db_table` preserved on the live tenants.
"""
from django.apps import AppConfig


class MarketplaceB2BConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'marketplace_b2b'
    verbose_name = 'B2B Marketplace (Bidding, Parts, Escrow)'
