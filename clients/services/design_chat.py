"""
💬 Conversational Design Builder — service helpers (Phase N.5)
=====================================================================
Shared business logic used by both views and the management command:

  • annotate_designs_from_chat()    — "from chat" badge annotation
  • prune_stale_conversations()     — stale planning/refining → abandoned

The resume-banner lookup lives on the view side as
`clients.views.design_chat_views.find_resumable_conversation` — it uses
the longer DESIGN_CHAT_RESUME_HOURS window (24h) rather than the prune
DESIGN_CHAT_IDLE_MINUTES window (60min), so it intentionally surfaces
conversations that are still alive in DB but past the active-banner
heartbeat. Kept there since it's purely a presentation-layer concern.

Kept out of views so the management command can import the same logic
without dragging the request/response layer.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Exists, OuterRef, QuerySet
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')


# Stages eligible for stale-cleanup. 'finalized' and 'abandoned' are terminal.
PRUNABLE_STAGES = ('planning', 'generated', 'refining')


def annotate_designs_from_chat(qs: QuerySet) -> QuerySet:
    """Add `from_conversation` boolean to a CustomerDesign queryset.

    True if any DesignConversationTurn references the design as its
    `design_snapshot` — which is exactly how the chat orchestrator
    links generated/refined designs to their source turn.

    Uses an EXISTS subquery — index on design_snapshot_id keeps it cheap.
    """
    from clients.models import DesignConversationTurn

    return qs.annotate(
        from_conversation=Exists(
            DesignConversationTurn.objects
            .filter(design_snapshot=OuterRef('pk'))
        ),
    )


def prune_stale_conversations(
    *,
    idle_minutes: int | None = None,
    dry_run: bool = False,
    customer=None,
) -> dict:
    """Transition idle planning/generated/refining conversations to 'abandoned'.

    Args:
        idle_minutes: how many minutes of inactivity counts as stale.
                     Defaults to settings.DESIGN_CHAT_IDLE_MINUTES.
        dry_run:     if True, count what would be abandoned but don't mutate.
        customer:    optional — scope the prune to one customer's rows
                     (used by lazy cleanup in design_chat_start).

    Returns: {'inspected': int, 'abandoned': int, 'cutoff': iso_str,
              'dry_run': bool, 'by_stage': {'planning': N, ...}}

    Idempotent: re-runs are safe — already-abandoned rows aren't touched.
    """
    from clients.models import DesignConversation

    idle = int(idle_minutes if idle_minutes is not None
               else getattr(settings, 'DESIGN_CHAT_IDLE_MINUTES', 60))
    cutoff = timezone.now() - timedelta(minutes=idle)

    qs = DesignConversation.objects.filter(
        stage__in=PRUNABLE_STAGES,
        updated_at__lt=cutoff,
    )
    if customer is not None:
        qs = qs.filter(customer=customer)

    by_stage = {
        s: qs.filter(stage=s).count() for s in PRUNABLE_STAGES
    }
    inspected = sum(by_stage.values())

    if dry_run:
        return {
            'inspected': inspected,
            'abandoned': 0,
            'cutoff': cutoff.isoformat(),
            'dry_run': True,
            'by_stage': by_stage,
        }

    # Mass UPDATE — single SQL, no per-row overhead. We don't enumerate the
    # rows because abandonment is a pure state transition (no signals/audit
    # log per row currently — the by_stage breakdown is the audit trail).
    abandoned_count = qs.update(
        stage='abandoned',
        abandoned_at=timezone.now(),
        # Clear any lingering lock — stale conversation locks would block
        # nothing now but they're noise in the data.
        locked_until=None,
    )

    if abandoned_count:
        logger.info(
            f'[DESIGN CHAT PRUNE] abandoned {abandoned_count} stale '
            f'conversations (idle >{idle}min) by_stage={by_stage}'
        )
    return {
        'inspected': inspected,
        'abandoned': abandoned_count,
        'cutoff': cutoff.isoformat(),
        'dry_run': False,
        'by_stage': by_stage,
    }
