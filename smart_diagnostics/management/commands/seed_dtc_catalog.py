"""
manage.py seed_dtc_catalog [--reset] [--file path/to/dtc_catalog.json]

Idempotent: uses update_or_create on DTC code. Runs in public schema.
"""
import json
import logging
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import connection

logger = logging.getLogger('mouss_tec_core')

DEFAULT_FIXTURE = Path(__file__).resolve().parent.parent.parent / 'fixtures' / 'dtc_catalog.json'

DEFAULT_GUIDED_STEPS = [
    {'step': 1, 'title': 'فحص بصري',
     'action': 'افحص الـ wiring/connector للمكوّن المرتبط بالكود.',
     'expected': 'لا توجد تأكلات/قطع.'},
    {'step': 2, 'title': 'قراءة Live Data',
     'action': 'راقب القراءات أثناء الـ idle ثم ارفع الـ RPM.',
     'expected': 'القراءات ضمن النطاق المرجعي.'},
    {'step': 3, 'title': 'فحص بالـ multimeter',
     'action': 'قِس المقاومة/الجهد على المكوّن طبقاً للمواصفات.',
     'expected': 'القيم ضمن المتسامح.'},
    {'step': 4, 'title': 'استبدال محتمل',
     'action': 'استبدل بالـ OEM المرجح ثم امسح الكود.',
     'expected': 'الكود لا يعود بعد دورة قيادة.'},
]


class Command(BaseCommand):
    help = 'Seed the DTCDefinition catalog from a JSON fixture (idempotent).'

    def add_arguments(self, parser):
        parser.add_argument('--file', default=str(DEFAULT_FIXTURE),
                            help='Path to JSON fixture')
        parser.add_argument('--reset', action='store_true',
                            help='Delete existing community-sourced entries first')

    def handle(self, *args, **opts):
        from diagnostics_catalog.models import DTCDefinition

        # Always operate in public schema (shared catalog).
        if connection.schema_name != 'public':
            connection.set_schema_to_public()

        fixture = Path(opts['file'])
        if not fixture.exists():
            self.stderr.write(self.style.ERROR(f'Fixture not found: {fixture}'))
            return

        if opts['reset']:
            n, _ = DTCDefinition.objects.filter(source='community').delete()
            self.stdout.write(self.style.WARNING(f'Deleted {n} community DTCs'))

        with fixture.open(encoding='utf-8') as f:
            entries = json.load(f)

        created = updated = 0
        for entry in entries:
            obj, was_created = DTCDefinition.objects.update_or_create(
                code=entry['code'].upper(),
                defaults={
                    'system': entry.get('system', 'P'),
                    'severity': entry.get('severity', 'medium'),
                    'short_description': entry['short'],
                    'full_description': entry.get('full', ''),
                    'likely_oem_parts': entry.get('parts', []),
                    'guided_steps': entry.get('guided_steps') or DEFAULT_GUIDED_STEPS,
                    'source': entry.get('source', 'community'),
                    'is_generic': entry.get('is_generic', True),
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'✅ DTC catalog seeded: {created} new, {updated} updated, total entries={len(entries)}'
        ))
