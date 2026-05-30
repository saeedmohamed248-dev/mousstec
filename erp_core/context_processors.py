"""
Template context processors for Mouss Tec ERP.
"""
from django.db import connection


def tenant_context(request):
    """Inject tenant-related context into all templates."""
    return {
        'is_public_schema': connection.schema_name == 'public',
    }
