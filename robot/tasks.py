"""robot/tasks.py — periodic robot housekeeping (Celery Beat)."""

import logging

from celery import shared_task
from clients.services.modules import skip_if_disabled

logger = logging.getLogger('mouss_tec_core')


@shared_task(name='robot.tasks.raise_offline_alerts')
@skip_if_disabled('robot')   # 🧩 الموديول مقفول من لوحة المالك ⇒ المهمة بتقف
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


@shared_task(name='robot.tasks.prune_heard_audio')
@skip_if_disabled('robot')   # 🧩 الموديول مقفول من لوحة المالك ⇒ المهمة بتقف
def prune_heard_audio():
    """«🎓 تعلّم السمع»: the recordings older than ROBOT_HEARD_AUDIO_DAYS let go
    in every workshop, the words kept (robot/teachers.prune_audio)."""
    from django_tenants.utils import schema_context, get_tenant_model
    from robot import teachers

    total = 0
    for tenant in get_tenant_model().objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                total += teachers.prune_audio()
        except Exception as e:
            logger.error("[ROBOT TASK] heard audio prune failed for '%s': %s",
                         tenant.schema_name, e)
    return f"Robot recordings let go: {total}"
