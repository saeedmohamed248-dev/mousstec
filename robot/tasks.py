"""robot/tasks.py — periodic robot housekeeping (Celery Beat)."""

import logging

from celery import shared_task

logger = logging.getLogger('mouss_tec_core')


@shared_task(name='robot.tasks.raise_offline_alerts')
def raise_offline_alerts():
    """Alert the owner when a robot goes silent (once per outage).

    `back_online` is raised by the device's own next request; this catches
    the other half — a robot that stopped calling in at all. Multi-tenant
    aware: RobotDevice lives in each workshop's schema.
    """
    from django_tenants.utils import schema_context, get_tenant_model
    from robot import services

    total = 0
    for tenant in get_tenant_model().objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                total += services.raise_offline_alerts()
        except Exception as e:
            logger.error("[ROBOT TASK] offline check failed for '%s': %s",
                         tenant.schema_name, e)
    return f"Robot offline alerts raised: {total}"
