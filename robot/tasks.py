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


@shared_task(name='robot.tasks.fblive_turn', ignore_result=True)
def fblive_turn(schema_name, device_id):
    """«📺 لايف فيسبوك»: one reading of the live's comments for one robot, answered on Facebook
    and aloud (robot/fblive.py) — in the background, so the robot's poll never waits on it."""
    from django_tenants.utils import schema_context
    from robot import fblive
    from robot.models import RobotDevice

    with schema_context(schema_name):
        device = RobotDevice.objects.filter(pk=device_id).first()
        if device is None:
            return 0
        try:
            return fblive.turn(device)
        except Exception as e:
            logger.error("[ROBOT TASK] fblive failed for robot %s in '%s': %s", device_id, schema_name, e)
            return 0
        finally:
            fblive.done(device)


@shared_task(name='robot.tasks.refresh_website_replies')
@skip_if_disabled('robot')
def refresh_website_replies():
    """«📥 من الموقع» once a week: what changed on each shop's site comes back as a draft to review
    (robot/website.py) — a reply the owner turned on is never touched."""
    from django_tenants.utils import schema_context, get_tenant_model
    from robot import website

    total = 0
    for tenant in get_tenant_model().objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                if website.remembered_url():
                    got = website.read()
                    total += got.get("added", 0) + got.get("changed", 0)
        except Exception as e:
            logger.error("[ROBOT TASK] website read failed for '%s': %s", tenant.schema_name, e)
    return f"Robot website replies drafted: {total}"
