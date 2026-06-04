"""
💬 أمر إداري: تنظيف محادثات التصميم المهجورة

يحوّل المحادثات اللي مفيش نشاط فيها لمدة DESIGN_CHAT_IDLE_MINUTES إلى
stage='abandoned' عشان الـ DB يفضل نضيف ومـ active counter ميـ inflate-ش.

الاستخدام:
    python manage.py prune_stale_design_chats
    python manage.py prune_stale_design_chats --idle-minutes 120
    python manage.py prune_stale_design_chats --dry-run

يتشغّل عادة عبر cron / Celery Beat كل ساعة.
يتصرّف بأمان لو اتشغل بالتوازي أو مرات كتيرة (idempotent).
"""
import json

from django.conf import settings
from django.core.management.base import BaseCommand

from clients.services.design_chat import prune_stale_conversations


class Command(BaseCommand):
    help = 'تحويل محادثات تصميم خاملة إلى abandoned (default: > DESIGN_CHAT_IDLE_MINUTES)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--idle-minutes',
            type=int,
            default=None,
            help='Override settings.DESIGN_CHAT_IDLE_MINUTES (default = 60)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would be abandoned without mutating',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Emit machine-readable JSON instead of text (for cron logs)',
        )

    def handle(self, *args, **options):
        idle = options.get('idle_minutes')
        dry = bool(options.get('dry_run'))
        as_json = bool(options.get('json'))

        effective_idle = (
            idle if idle is not None
            else int(getattr(settings, 'DESIGN_CHAT_IDLE_MINUTES', 60))
        )

        result = prune_stale_conversations(idle_minutes=idle, dry_run=dry)

        if as_json:
            self.stdout.write(json.dumps(result, ensure_ascii=False))
            return

        verb = 'would abandon' if dry else 'abandoned'
        self.stdout.write(self.style.SUCCESS(
            f'💬 [DESIGN CHAT PRUNE] {verb} {result["abandoned"] or result["inspected"]} '
            f'conversations idle > {effective_idle}min'
        ))
        self.stdout.write(
            f'   By stage: planning={result["by_stage"]["planning"]}, '
            f'generated={result["by_stage"]["generated"]}, '
            f'refining={result["by_stage"]["refining"]}'
        )
        self.stdout.write(f'   Cutoff: {result["cutoff"]}')
        if dry:
            self.stdout.write(self.style.WARNING(
                '   (dry-run — no rows were modified)'
            ))
