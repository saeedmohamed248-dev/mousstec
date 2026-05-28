"""
Celery Tasks: HR Module — scheduled automation.
"""

import logging
from celery import shared_task

logger = logging.getLogger('mouss_tec_core')


@shared_task(name='hr.tasks.mark_absent_employees_daily')
def mark_absent_employees_daily():
    """
    Daily task — runs at end of business day.
    Marks all employees without clock-in as absent.
    Multi-tenant aware: iterates over all tenant schemas.
    """
    from django_tenants.utils import schema_context, get_tenant_model
    from hr.services.attendance_service import AttendanceService

    TenantModel = get_tenant_model()
    tenants = TenantModel.objects.exclude(schema_name='public')

    total = 0
    for tenant in tenants:
        try:
            with schema_context(tenant.schema_name):
                count = AttendanceService.mark_absent_employees()
                total += count
                if count:
                    logger.info(
                        "[HR TASK] Marked %s absent for tenant '%s'",
                        count, tenant.schema_name,
                    )
        except Exception as e:
            logger.error(
                "[HR TASK] Failed for tenant '%s': %s",
                tenant.schema_name, e,
            )

    return f"Total absent marked: {total}"
