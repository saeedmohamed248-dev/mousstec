"""
Celery Tasks: BMW ECU — scheduled housekeeping.
"""

import logging

from celery import shared_task

logger = logging.getLogger('mouss_tec_core')


@shared_task(name='bmw_ecu.tasks.expire_stale_fee_authorizations')
def expire_stale_fee_authorizations():
    """
    كل ساعة: حجوزات رسوم الفحص (450 ج) اللي جلستها ماخلصتش خلال 24 ساعة
    بتتقفل «expired» — مفيش فلوس اتخصمت عليها (الخصم وقت capture بس).
    Multi-tenant: بيلف على كل الشركات (bmw_ecu في TENANT_APPS).
    """
    from django_tenants.utils import get_tenant_model, schema_context
    from bmw_ecu.services.billing_gate import expire_stale_authorizations

    total = 0
    for tenant in get_tenant_model().objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                total += expire_stale_authorizations()
        except Exception as e:
            logger.error("[BMW TASK] stale-auth reaper failed for '%s': %s", tenant.schema_name, e)
    return f"Expired stale fee authorizations: {total}"
