"""🛡️ لوحة مالك المنصة — البوابة الأمنية (public schema + superuser) وتسجيل الأحداث."""
from __future__ import annotations

import logging

from django.contrib.auth.decorators import user_passes_test
from django.db import connection

from clients.models import (
    PlatformEvent,
)

logger = logging.getLogger('mouss_tec_core')


def _log_event(event_type, *, tenant=None, user=None, description=''):
    """تسجيل حدث في PlatformEvent بأمان — لا يفشل أبداً."""
    try:
        PlatformEvent.objects.create(
            event_type=event_type,
            tenant_schema=getattr(tenant, 'schema_name', '') or '',
            tenant_name=getattr(tenant, 'name', '') or '',
            user_name=getattr(user, 'username', '') or '',
            description=description[:500],
        )
    except Exception:
        logger.exception("PlatformEvent log failed (event_type=%s)", event_type)


# ─────────────────────────────────────────────────────────────────────
# 🛡️ Security gate — public schema + superuser only
# ─────────────────────────────────────────────────────────────────────
def _saas_admin_required(user):
    """يسمح بالـ superuser أو أي موظف عنده StaffRole في public schema."""
    if not (user.is_authenticated and connection.schema_name == 'public'):
        return False
    if user.is_superuser:
        return True
    return hasattr(user, 'staff_role')

saas_admin_required = user_passes_test(_saas_admin_required, login_url='/secure-portal/login/')
