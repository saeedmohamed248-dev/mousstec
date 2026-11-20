"""
🗺️ خريطة قاعدة البيانات — `python manage.py db_map` (أو `make db-map`)
=====================================================================
بيطلع `docs/DATABASE.md` من الموديلات نفسها: كل تطبيق، هل جداوله في
public (مشتركة للمنصة) ولا جوه schema كل شركة، كل موديل وجدوله، وعلاقاته
بالتطبيقات التانية. كده المبرمج الجديد يعرف الجدول فين ومين بيعتمد عليه
من غير ما يقرا الكود كله.

    python manage.py db_map            # يكتب docs/DATABASE.md
    python manage.py db_map --check    # يفشل لو الملف مش متحدّث (بيستخدمه الاختبار)
    python manage.py db_map --stdout   # يطبع من غير ما يكتب
"""
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

OUT = Path(settings.BASE_DIR) / 'docs' / 'DATABASE.md'

# التطبيقات بتاعة المشروع (مش مكتبات Django/طرف تالت)
_THIRD_PARTY_PREFIXES = ('django', 'rest_framework', 'axes', 'simple_history', 'import_export',
                         'corsheaders', 'storages', 'channels', 'daphne', 'jazzmin', 'django_tenants',
                         'django_celery_beat')


def _is_project_app(label, name):
    return not name.startswith(_THIRD_PARTY_PREFIXES)


def _schema_of(name):
    shared = name in settings.SHARED_APPS
    tenant = name in settings.TENANT_APPS
    if shared and tenant:
        return 'public + كل شركة'
    if shared:
        return 'public (مشتركة للمنصة)'
    if tenant:
        return 'جوه schema كل شركة'
    return '—'


def build():
    configs = sorted((c for c in apps.get_app_configs() if _is_project_app(c.label, c.name)),
                     key=lambda c: c.label)
    out = [
        '# 🗺️ خريطة قاعدة البيانات',
        '',
        '> ⚙️ الملف ده **متولّد أوتوماتيك** من الموديلات — ماتعدّلوش بإيدك.',
        '> بعد أي تغيير في موديل: `make db-map` (أو `python manage.py db_map`) واعمل commit للنتيجة.',
        '> اختبار `tenancy.tests.test_db_map` بيفشل لو الملف مش متحدّث.',
        '',
        '## إزاي القاعدة متقسّمة',
        '',
        '- **PostgreSQL واحدة** فيها schema اسمها `public` للمنصة نفسها (الشركات، الباقات، الاشتراكات، السوق…)',
        '  و**schema لكل شركة** (django-tenants) فيها بيانات الشركة بس (مخزن، فواتير، خزائن، موظفين…).',
        '- التطبيق اللي في `SHARED_APPS` جداوله في `public`؛ اللي في `TENANT_APPS` جداوله بتتكرر جوه كل شركة.',
        '- `migrate` العادي مقفول — `make migrate` (`migrate_schemas --shared` ثم `--tenant` ثم `refresh_tenant_template`).',
        '- علاقة (FK) من جدول شركة لجدول في `public` مسموحة (زي `auth.User`)، والعكس **ممنوع** —',
        '  جدول المنصة مايقدرش يشاور على صف جوه schema شركة.',
        '',
        '## نظرة سريعة',
        '',
        '| التطبيق | مكان الجداول | عدد الموديلات |',
        '|---|---|---|',
    ]
    sections = []
    for cfg in configs:
        models = sorted(cfg.get_models(), key=lambda m: m.__name__)
        if not models:
            continue
        out.append(f'| `{cfg.label}` | {_schema_of(cfg.name)} | {len(models)} |')
        sec = [f'### `{cfg.label}` — {_schema_of(cfg.name)}', '',
               '| الموديل | الجدول | علاقات بتطبيقات تانية |', '|---|---|---|']
        for m in models:
            rels = sorted({
                f'{f.related_model._meta.app_label}.{f.related_model.__name__}'
                for f in m._meta.get_fields()
                if getattr(f, 'related_model', None) is not None
                and not f.auto_created
                and (f.many_to_one or f.one_to_one or f.many_to_many)
                and f.related_model._meta.app_label != cfg.label
            })
            sec.append(f'| `{m.__name__}` | `{m._meta.db_table}` | {", ".join(rels) or "—"} |')
        sections.append('\n'.join(sec))
    out += ['', '## الموديلات حسب التطبيق', '', '\n\n'.join(sections), '']
    return '\n'.join(out)


class Command(BaseCommand):
    help = 'يولّد docs/DATABASE.md (خريطة الجداول والعلاقات) من الموديلات.'

    def add_arguments(self, parser):
        parser.add_argument('--check', action='store_true', help='يفشل لو الملف مش متحدّث')
        parser.add_argument('--stdout', action='store_true', help='يطبع بدل ما يكتب')

    def handle(self, *args, check=False, stdout=False, **opts):
        text = build()
        if stdout:
            self.stdout.write(text)
            return
        if check:
            current = OUT.read_text(encoding='utf-8') if OUT.exists() else ''
            if current != text:
                raise CommandError('docs/DATABASE.md مش متحدّث — شغّل: make db-map')
            self.stdout.write('✅ docs/DATABASE.md متحدّث')
            return
        OUT.write_text(text, encoding='utf-8')
        self.stdout.write(f'✅ اتكتب {OUT.relative_to(settings.BASE_DIR)}')
