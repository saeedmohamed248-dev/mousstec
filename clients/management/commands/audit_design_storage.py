"""
🔍 Audit & repair CustomerDesign records.

Three independent jobs in one command:

1. **Ephemeral URL audit** — finds rows whose image_url still points at an
   AI-provider host (Together / Replicate / Stability / Kontext / Ideogram
   CDN). These expire after ~1h and the gallery card shows broken.

2. **Variant backfill** — for healthy rows that *do* live on our storage
   but lack the new thumb/preview WebP variants (records created before
   Phase 2), download the original and generate the two variants.

3. **Broken flag** — for rows we can't recover, prefix the title with
   "[BROKEN] " so the gallery can filter them out (no schema change).

Usage:
    python manage.py audit_design_storage                   # dry-run report
    python manage.py audit_design_storage --repair          # re-fetch ephemerals
    python manage.py audit_design_storage --backfill-variants
    python manage.py audit_design_storage --flag-broken
    python manage.py audit_design_storage --repair --backfill-variants --limit 50
"""
from __future__ import annotations

import logging
import re

from django.core.management.base import BaseCommand
from django.utils import timezone

from clients.models import CustomerDesign
from clients.services.design_persistence import (
    persist_image_with_variants,
    _is_already_persisted,
)

logger = logging.getLogger('mouss_tec_core')

EPHEMERAL_HOST_PATTERN = re.compile(
    r'https?://[^/]*('
    r'together\.ai|together\.xyz|togethercomputer'
    r'|replicate\.delivery|replicate\.com'
    r'|stability\.ai|api\.stability'
    r'|fal\.media|fal\.ai'
    r'|cdn\.openai\.com|oaidalleapiprodscus'
    r'|ideogram\.ai'
    r')',
    re.IGNORECASE,
)


def _is_ephemeral(url: str) -> bool:
    if not url:
        return False
    return bool(EPHEMERAL_HOST_PATTERN.search(url))


class Command(BaseCommand):
    help = 'Audit CustomerDesign rows: re-fetch ephemerals, backfill variants, flag broken.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--repair', action='store_true',
            help='Attempt to download each ephemeral URL and re-host locally '
                 '(generates variants too).',
        )
        parser.add_argument(
            '--backfill-variants', action='store_true',
            help='For locally-hosted rows missing thumb/preview, generate '
                 'them from the original.',
        )
        parser.add_argument(
            '--flag-broken', action='store_true',
            help='For unrecoverable rows (404/timeout), prefix title with '
                 '"[BROKEN] " so the gallery hides them. Implied by --repair.',
        )
        parser.add_argument(
            '--limit', type=int, default=0,
            help='Process at most N records per job (0 = all).',
        )

    def handle(self, *args, **opts):
        repair = bool(opts['repair'])
        backfill = bool(opts['backfill_variants'])
        flag_broken = bool(opts['flag_broken'] or repair)
        limit = int(opts['limit'] or 0)

        if not (repair or backfill or flag_broken):
            self._report_dry_run(limit)
            return

        if repair:
            self._do_repair(limit, flag_broken)
        if backfill:
            self._do_backfill(limit)

    # ─── Dry run ──────────────────────────────────────────────────────
    def _report_dry_run(self, limit: int):
        eph_total = 0
        missing_thumb = 0
        for d in CustomerDesign.objects.exclude(image_url='').only(
            'pk', 'image_url', 'image_thumb_url'
        ).iterator(chunk_size=500):
            if _is_ephemeral(d.image_url):
                eph_total += 1
            elif not d.image_thumb_url:
                missing_thumb += 1

        self.stdout.write(self.style.WARNING(
            f'Ephemeral provider URLs:      {eph_total}'
        ))
        self.stdout.write(self.style.WARNING(
            f'Healthy rows missing thumb:   {missing_thumb}'
        ))
        self.stdout.write(
            '\nNext steps:\n'
            '  --repair              re-fetch ephemerals (variants generated too)\n'
            '  --backfill-variants   add thumb/preview to existing healthy rows\n'
            '  --flag-broken         hide unrecoverable rows from gallery'
        )

    # ─── Repair ephemerals ────────────────────────────────────────────
    def _do_repair(self, limit: int, flag_broken: bool):
        qs = CustomerDesign.objects.exclude(image_url='').exclude(
            title__startswith='[BROKEN] '
        ).order_by('created_at')
        candidates = []
        for d in qs.iterator(chunk_size=500):
            if _is_ephemeral(d.image_url):
                candidates.append(d)
                if limit and len(candidates) >= limit:
                    break

        self.stdout.write(self.style.WARNING(
            f'[REPAIR] Attempting recovery for {len(candidates)} rows.'
        ))
        recovered = broken = errored = 0
        for d in candidates:
            try:
                ok = self._try_persist(d, prefix='design_chat')
            except Exception as exc:  # noqa: BLE001
                errored += 1
                logger.exception(f'[AUDIT REPAIR] #{d.pk} unexpected: {exc}')
                continue
            if ok:
                recovered += 1
            else:
                broken += 1
                if flag_broken:
                    self._flag_broken(d)
        self.stdout.write(self.style.SUCCESS(
            f'[REPAIR] recovered={recovered} broken={broken} errored={errored}'
        ))

    # ─── Backfill variants ────────────────────────────────────────────
    def _do_backfill(self, limit: int):
        qs = CustomerDesign.objects.exclude(image_url='').filter(
            image_thumb_url='',
        ).exclude(title__startswith='[BROKEN] ').order_by('-created_at')
        # Skip ones still on ephemeral hosts — they need --repair instead.
        candidates = []
        for d in qs.iterator(chunk_size=500):
            if _is_ephemeral(d.image_url):
                continue
            candidates.append(d)
            if limit and len(candidates) >= limit:
                break

        self.stdout.write(self.style.WARNING(
            f'[BACKFILL] Generating variants for {len(candidates)} rows.'
        ))
        done = errored = 0
        for d in candidates:
            try:
                ok = self._try_persist(d, prefix='design_backfill')
            except Exception as exc:  # noqa: BLE001
                errored += 1
                logger.exception(f'[AUDIT BACKFILL] #{d.pk} unexpected: {exc}')
                continue
            if ok:
                done += 1
        self.stdout.write(self.style.SUCCESS(
            f'[BACKFILL] done={done} errored={errored}'
        ))

    # ─── Shared row-level operation ───────────────────────────────────
    def _try_persist(self, design: CustomerDesign, prefix: str) -> bool:
        """Re-fetch + variants. Returns True on success, False on dead URL."""
        try:
            result = persist_image_with_variants(
                request=None,
                customer=design.customer,
                provider_image_url=design.image_url,
                prefix=prefix,
            )
        except RuntimeError as e:
            self.stdout.write(self.style.NOTICE(
                f'  ✗ #{design.pk} unrecoverable: {e}'
            ))
            return False

        design.image_url = result['image_url'][:600]
        design.image_thumb_url = (result['thumb_url'] or '')[:600]
        design.image_preview_url = (result['preview_url'] or '')[:600]
        design.image_persisted_at = timezone.now()
        if result['size_bytes'] is not None:
            design.image_size_bytes = result['size_bytes']
        design.save(update_fields=[
            'image_url', 'image_thumb_url', 'image_preview_url',
            'image_persisted_at', 'image_size_bytes',
        ])
        self.stdout.write(self.style.SUCCESS(
            f'  ✓ #{design.pk} → variants generated'
        ))
        return True

    def _flag_broken(self, design: CustomerDesign):
        if not design.title.startswith('[BROKEN] '):
            design.title = ('[BROKEN] ' + design.title)[:200]
            design.save(update_fields=['title'])
        self.stdout.write(self.style.NOTICE(
            f'  ✗ #{design.pk} flagged broken.'
        ))
