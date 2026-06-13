"""
🛡️ Plan-based quota enforcement signals
=======================================
Hooks ``pre_save`` on User / Branch / Treasury so the tenant's
``total_allowed_*`` limits (Plan max + purchased extras) are enforced
on **every** creation path — Django admin, REST endpoints, the
``manage.py shell``, fixtures, ``loaddata``, etc.

Why pre_save instead of a view-layer decorator
---------------------------------------------
* The Branch admin already had an inline check (``inventory/admin.py``)
  but only on the admin form — every other entry point (DRF, custom
  views, shell) could create branches unchecked. Same is true for User
  and Treasury today: no enforcement at all.
* Signals run regardless of who calls ``save()``. One implementation,
  zero leaks.

Skip rules
----------
* Updates (``instance.pk`` already set) are never blocked — only the
  *creation* of an extra row.
* ``public`` schema activity (the platform-owner shell) is exempt.
* If the tenant's ``total_allowed_*`` returns 0/None, treat as
  unlimited (matches existing ``max_repair_cards`` convention where
  ``0 = unlimited``).
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models.signals import pre_save
from django.dispatch import receiver

logger = logging.getLogger('mouss_tec_core')


def _current_tenant():
    """Return the tenant for the current schema, or None for public."""
    tenant = getattr(connection, 'tenant', None)
    if tenant is None:
        return None
    if getattr(tenant, 'schema_name', 'public') == 'public':
        return None
    return tenant


def _enforce(*, instance, model_cls, allowed_attr: str, label_ar: str, upgrade_hint: str):
    """Generic quota check used by the three pre_save handlers.

    Raises ``ValidationError`` (caught by Django admin and DRF) when
    creating the row would push the tenant past its plan limit.
    """
    # Updates are always allowed.
    if instance.pk:
        return

    tenant = _current_tenant()
    if tenant is None:
        return  # public schema or no tenant context — let it through

    allowed = getattr(tenant, allowed_attr, 0) or 0
    if allowed <= 0:
        return  # 0 = unlimited per existing convention

    current = model_cls.objects.count()
    if current >= allowed:
        logger.warning(
            f"[QUOTA] tenant={tenant.schema_name} blocked {model_cls.__name__} "
            f"creation: {current}/{allowed} (allowed_attr={allowed_attr})"
        )
        raise ValidationError(
            f"🚫 وصلت الحد الأقصى لـ{label_ar} في باقتك ({allowed}). "
            f"{upgrade_hint}"
        )


def _enforce_feature(*, instance, feature_code: str, label_ar: str, upgrade_hint: str):
    """Block creation when the tenant's plan doesn't include the feature.

    Boolean entitlement guard — pairs with the quantitative ``_enforce``
    above. Updates pass through; only the *first* creation of the row is
    checked. Public schema is exempt.
    """
    if instance.pk:
        return

    tenant = _current_tenant()
    if tenant is None:
        return

    # Local import: EntitlementService imports from clients.models which
    # may not be fully loaded at module top during apps registry boot.
    from clients.services.entitlements import EntitlementService

    if EntitlementService.has(tenant, feature_code):
        return

    logger.warning(
        f"[ENTITLEMENT] tenant={tenant.schema_name} blocked "
        f"{instance.__class__.__name__} creation: feature '{feature_code}' "
        f"not in plan"
    )
    raise ValidationError(
        f"🔒 ميزة {label_ar} غير متاحة في باقتك الحالية. {upgrade_hint}"
    )


# ─────────────────────────────────────────────────────────────────────
# Lazy connect — we can't import inventory.models at module top because
# the apps registry isn't ready yet. We wire receivers as soon as the
# AppConfig.ready() pulls this module in.
# ─────────────────────────────────────────────────────────────────────
def _connect():
    from django.contrib.auth import get_user_model
    from inventory.models import Branch, MaintenanceContract, Treasury

    User = get_user_model()

    @receiver(pre_save, sender=User, dispatch_uid='quota_enforce_user')
    def _quota_user(sender, instance, **kwargs):
        _enforce(
            instance=instance, model_cls=User,
            allowed_attr='total_allowed_users',
            label_ar='المستخدمين',
            upgrade_hint='اشترِ مستخدمين إضافيين أو ترقّى لباقة أعلى.',
        )

    @receiver(pre_save, sender=Branch, dispatch_uid='quota_enforce_branch')
    def _quota_branch(sender, instance, **kwargs):
        _enforce(
            instance=instance, model_cls=Branch,
            allowed_attr='total_allowed_branches',
            label_ar='الفروع',
            upgrade_hint='اشترِ فرع إضافي أو ترقّى لباقة أعلى.',
        )

    @receiver(pre_save, sender=Treasury, dispatch_uid='quota_enforce_treasury')
    def _quota_treasury(sender, instance, **kwargs):
        _enforce(
            instance=instance, model_cls=Treasury,
            allowed_attr='total_allowed_treasuries',
            label_ar='الخزائن',
            upgrade_hint='اشترِ خزنة إضافية أو ترقّى لباقة أعلى.',
        )

    # ── Boolean-entitlement guards ───────────────────────────────────
    @receiver(pre_save, sender=MaintenanceContract,
              dispatch_uid='entitlement_fleet_contracts')
    def _ent_fleet_contracts(sender, instance, **kwargs):
        _enforce_feature(
            instance=instance,
            feature_code='workshop_fleet_contracts',
            label_ar='عقود أساطيل الصيانة',
            upgrade_hint='ميزة Empire — ترقّى لاستخدامها.',
        )

    # DesignerWorkLog lives in the printing app — connect lazily so the
    # signal still fires if printing app is installed.
    try:
        from printing.models import DesignerWorkLog

        @receiver(pre_save, sender=DesignerWorkLog,
                  dispatch_uid='entitlement_designer_worklog')
        def _ent_designer_worklog(sender, instance, **kwargs):
            _enforce_feature(
                instance=instance,
                feature_code='print_designer_worklog',
                label_ar='سجل أعمال المصممين',
                upgrade_hint='متاح في Pro + Enterprise — ترقّى للاستخدام.',
            )
    except ImportError:
        logger.debug("[QUOTA] printing app not installed; skipping DesignerWorkLog guard")

    logger.debug("[QUOTA] pre_save receivers wired (quantitative + boolean)")


_connect()
