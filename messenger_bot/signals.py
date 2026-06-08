"""
Real-time knowledge sync: keep the vector store in lockstep with SystemUpdate.

Embedding calls hit the network, so we run them after the DB commit succeeds
(`transaction.on_commit`) — that way an embedding failure can never roll back
the underlying row, and we don't double-vectorise inside an outer transaction
that later aborts.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import SystemUpdate
from .services.vector_store import get_store

logger = logging.getLogger(__name__)


@receiver(post_save, sender=SystemUpdate)
def sync_system_update_to_vector_store(sender, instance: SystemUpdate, **kwargs):
    doc_id = f"systemupdate:{instance.pk}"

    def _do_sync():
        try:
            store = get_store()
            if not instance.is_active:
                store.delete(doc_id)
                return
            store.upsert(
                doc_id=doc_id,
                text=instance.embedding_text,
                metadata={
                    "kind": instance.kind,
                    "title": instance.title,
                    "updated_at": instance.updated_at.isoformat() if instance.updated_at else None,
                },
            )
        except Exception as exc:  # never let signal errors break the save
            logger.exception("messenger_bot: failed to sync SystemUpdate %s: %s", instance.pk, exc)

    transaction.on_commit(_do_sync)


@receiver(post_delete, sender=SystemUpdate)
def remove_system_update_from_vector_store(sender, instance: SystemUpdate, **kwargs):
    doc_id = f"systemupdate:{instance.pk}"
    try:
        get_store().delete(doc_id)
    except Exception as exc:
        logger.exception("messenger_bot: failed to delete SystemUpdate %s: %s", instance.pk, exc)
