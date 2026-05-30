"""
Structured logging filters for Mouss Tec ERP.
Adds tenant schema context to every log record automatically.
"""
import logging
from django.db import connection


class TenantContextFilter(logging.Filter):
    """
    Inject the current tenant schema name into every log record.
    This makes log lines filterable per-tenant without manual tagging.
    """

    def filter(self, record):
        try:
            record.tenant = getattr(connection, 'schema_name', 'unknown')
        except Exception:
            record.tenant = 'unknown'
        return True
