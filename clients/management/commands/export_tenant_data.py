"""
📦 Tenant data export (data portability / "take your data").

بيصدّر كل بيانات tenant واحد كـ JSON (Django serialization) من الـ schema
بتاعته + الصفوف الخاصة بيه من الجداول المشتركة. بيطلع ملف .json.gz.

الاستخدام:
    python manage.py export_tenant_data <schema_name>
    python manage.py export_tenant_data acme --output /tmp/acme.json.gz

بيُستخدم من super-admin endpoint (super_admin_export_tenant) كمان.
"""
from __future__ import annotations

import gzip
import io
import logging
import os

from django.apps import apps
from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import schema_context

logger = logging.getLogger('mouss_tec_core')

# التطبيقات اللي جداولها في الـ tenant schema (نفس منطق TENANT_APPS العملية).
_TENANT_APP_LABELS = ['inventory', 'printing', 'hr', 'smart_diagnostics',
                      'repair_atlas', 'bmw_ecu']


def build_tenant_export(schema_name: str) -> bytes:
    """يرجّع محتوى JSON.gz لكل موديلات الـ tenant apps داخل schema معيّن."""
    from clients.models import Client
    tenant = Client.objects.filter(schema_name=schema_name).first()
    if tenant is None:
        raise CommandError(f'مفيش tenant بالـ schema «{schema_name}».')

    buf = io.StringIO()
    buf.write('[\n')
    first = True
    with schema_context(schema_name):
        for label in _TENANT_APP_LABELS:
            try:
                app_config = apps.get_app_config(label)
            except LookupError:
                continue
            for model in app_config.get_models():
                # simple_history + import_export جداول مساعدة — نتخطاها
                if model._meta.object_name.startswith('Historical'):
                    continue
                try:
                    qs = model._default_manager.all()
                    data = serializers.serialize('json', qs.iterator())
                except Exception as exc:
                    logger.warning('[EXPORT] skip %s.%s: %s', label,
                                   model._meta.object_name, exc)
                    continue
                # data هي "[...]" — نلصق العناصر جوه المصفوفة الكبيرة
                import json as _json
                rows = _json.loads(data)
                for row in rows:
                    if not first:
                        buf.write(',\n')
                    buf.write(_json.dumps(row, ensure_ascii=False))
                    first = False

    buf.write('\n]\n')
    raw = buf.getvalue().encode('utf-8')
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode='wb') as gz:
        gz.write(raw)
    return out.getvalue()


class Command(BaseCommand):
    help = 'تصدير كل بيانات tenant كـ JSON.gz (data portability)'

    def add_arguments(self, parser):
        parser.add_argument('schema_name')
        parser.add_argument('--output', default='')

    def handle(self, *args, **opts):
        schema = opts['schema_name']
        payload = build_tenant_export(schema)

        out = opts['output'] or os.path.join(
            str(settings.BASE_DIR), 'backups', f'tenant_export_{schema}.json.gz')
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'wb') as f:
            f.write(payload)
        size_kb = len(payload) / 1024
        self.stdout.write(self.style.SUCCESS(
            f'✅ تم تصدير «{schema}» → {out} ({size_kb:.1f} KB)'))
