"""🗺️ خريطة قاعدة البيانات (`docs/DATABASE.md`) وقواعد الـ multi-tenant على مستوى الجداول.

* الخريطة متولّدة من الموديلات — لو حد غيّر موديل ومانسخش `make db-map` الاختبار يقع
  (عشان المبرمج الجديد يلاقي الخريطة صح دايماً).
* جدول في `public` (SHARED_APPS بس) مايقدرش يعمل FK لجدول موجود جوه schema الشركة بس:
  الصف ده مش موجود في public أصلاً ⇒ القيد بيبوظ أو العلاقة بتشاور على شركة غلط.
"""
from django.apps import apps
from django.conf import settings
from django.test import SimpleTestCase

from tenancy.management.commands import db_map


def _placement(model):
    name = apps.get_app_config(model._meta.app_label).name
    return name in settings.SHARED_APPS, name in settings.TENANT_APPS


class DatabaseMapTests(SimpleTestCase):

    def test_docs_database_md_is_up_to_date(self):
        current = db_map.OUT.read_text(encoding='utf-8') if db_map.OUT.exists() else ''
        self.assertEqual(current, db_map.build(),
                         'docs/DATABASE.md مش متحدّث — شغّل: make db-map واعمل commit')

    def test_every_project_model_is_listed(self):
        text = db_map.build()
        for cfg in apps.get_app_configs():
            if not db_map._is_project_app(cfg.label, cfg.name):
                continue
            for model in cfg.get_models():
                self.assertIn(f'`{model._meta.db_table}`', text, model._meta.label)

    def test_public_tables_never_point_into_a_tenant_schema(self):
        bad = []
        for model in apps.get_models():
            shared, tenant = _placement(model)
            if not shared or tenant:
                continue
            for field in model._meta.get_fields():
                rel = getattr(field, 'related_model', None)
                if rel is None or field.auto_created:
                    continue
                if not (field.many_to_one or field.one_to_one or field.many_to_many):
                    continue
                r_shared, r_tenant = _placement(rel)
                if r_tenant and not r_shared:
                    bad.append(f'{model._meta.label}.{field.name} → {rel._meta.label}')
        self.assertEqual(bad, [])
