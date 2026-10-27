"""
🔒 جداول المنصة المشتركة مابتظهرش في لوحة أدمن أي شركة.

🐛 [FIX — تسريب بين الشركات]: صاحب كل شركة بيتعمل superuser في الـ schema بتاعته،
وأي جدول في تطبيق مشترك (SHARED_APPS) متسجّل في الأدمن كان بيظهر له بكل صفوف
**كل الشركات**: إعدادات الإعلانات وقنوات واتساب/ماسنجر (بتوكنات الربط)، سجلات
محادثات العملاء، محادثات غرف الـ AI، وجدول المهام المجدولة للمنصة كلها.

الحارس ده بيقفل أي ModelAdmin لموديل في تطبيق مشترك على schema المنصة (public) بس،
بغض النظر عن الصلاحيات — وبيتطبّق على أي جدول مشترك يتضاف بعد كده تلقائياً.
"""
from django.conf import settings
from django.contrib import admin
from django.db import connection

_PERMS = ('has_module_permission', 'has_view_permission', 'has_change_permission',
          'has_add_permission', 'has_delete_permission')


def _shared_only_apps():
    tenant = set(settings.TENANT_APPS)
    return {a for a in settings.SHARED_APPS if a not in tenant}


def _platform_only(fn):
    def check(request, *args, **kwargs):
        if getattr(connection, 'schema_name', 'public') != 'public':
            return False
        return fn(request, *args, **kwargs)
    check.platform_only = True
    return check


def lock_shared_models_to_platform(site=admin.site):
    shared = _shared_only_apps()
    locked = []
    for model, model_admin in site._registry.items():
        if model._meta.app_config.name not in shared:
            continue
        for name in _PERMS:
            current = getattr(model_admin, name)
            if not getattr(current, 'platform_only', False):
                setattr(model_admin, name, _platform_only(current))
        locked.append(model)
    return locked
