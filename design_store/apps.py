"""
Design Store app — AI design generation, brand profile, conversations.

Phase 2A of Wave 2 (see docs/ARCHITECTURE.md). Hosts the AI persistence
+ conversation services and the design store pipeline tests. Models
still live in clients.models.design_store until Phase 2B.
"""
from django.apps import AppConfig


class DesignStoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'design_store'
    verbose_name = 'AI Design Store'
