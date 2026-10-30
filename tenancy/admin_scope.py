"""
🧭 جداول الشركات مالهاش مكان في لوحة الدومين الرئيسي
=====================================================
🐛 [FIX]: الأدمن متسجّل لكل الموديلات على كل الدومينات — فلوحة mousstec.com/secure-portal
كانت بتعرض موديلات المخزون/الروبوت/التشخيص… وأي ضغطة عليها = 500 (الجداول في سكيمات
الشركات بس). بنخفيها على public (عكس PublicSchemaOnlyAdminMixin بتاع جداول المنصة).
"""
from __future__ import annotations

from django.conf import settings
from django.db import connection

_PERMS = ('has_module_permission', 'has_view_permission', 'has_add_permission',
          'has_change_permission', 'has_delete_permission')


def _tenant_only_labels():
    from django.apps import apps
    shared = set(getattr(settings, 'SHARED_APPS', ()))
    names = [n for n in getattr(settings, 'TENANT_APPS', ()) if n not in shared]
    labels = set()
    for cfg in apps.get_app_configs():
        if cfg.name in names:
            labels.add(cfg.label)
    return labels


def _guard(original):
    def wrapped(request, *args, **kwargs):
        if getattr(connection, 'schema_name', 'public') == 'public':
            return False
        return original(request, *args, **kwargs)
    return wrapped


def hide_tenant_models_on_public(site=None):
    """يتنادى بعد ما الأدمن يسجّل كل الموديلات (inventory.apps.ready)."""
    from django.contrib import admin
    site = site or admin.site
    labels = _tenant_only_labels()
    for model, model_admin in list(site._registry.items()):
        if model._meta.app_label not in labels or getattr(model_admin, '_tenant_scoped', False):
            continue
        for name in _PERMS:
            setattr(model_admin, name, _guard(getattr(model_admin, name)))
        model_admin._tenant_scoped = True
