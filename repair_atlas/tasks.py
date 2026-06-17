"""
🔄 Celery tasks for repair_atlas
================================
- verify_repair_answer: المراجعة الذاتية في الخلفية. الفني بياخد رده فوراً
  (مسار سريع: توليد + sanity)، وده بيراجع الرد (Verifier V2 + Tie-Breaker V3)
  ويحدّث الثقة/الـ tier على RepairAnswer من غير ما الفني يستنى.

ملاحظة multi-tenant: RepairAnswer جوّه schema الـ tenant، فبنشتغل داخل
``schema_context(schema_name)`` اللي بيتبعت من الـ view وقت الـ enqueue.
"""
from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger('mouss_tec_core')


@shared_task(name='repair_atlas.tasks.verify_repair_answer')
def verify_repair_answer(schema_name: str, answer_id: int):
    """يقيّم رد RepairAnswer جاهز ويحدّث الثقة/الـ tier/الشكوك.

    مبيغيّرش نص الرد — بس بيحط عليه درجة الثقة بعد المراجعة، فاللي الفني
    شايفه ميتبدّلش تحت إيده.
    """
    from django_tenants.utils import schema_context
    from .models import (
        RepairAnswer, ConfidenceTier, VerificationStatus, AnswerSource,
    )
    from .services.repair_coach import score_answer

    tier_to_choice = {
        'high': ConfidenceTier.HIGH,
        'medium': ConfidenceTier.MEDIUM,
        'low': ConfidenceTier.LOW,
    }

    try:
        with schema_context(schema_name):
            ans = (RepairAnswer.objects
                   .select_related('query', 'query__session')
                   .filter(id=answer_id).first())
            if not ans:
                logger.warning('[REPAIR_ATLAS] verify task: answer %s missing', answer_id)
                return
            q = ans.query
            sess = q.session
            vehicle = {
                'brand': sess.brand, 'model_name': sess.model_name,
                'year': sess.year, 'vin': sess.vin,
            }

            scored = score_answer(
                question=q.question_text, answer=ans.body_markdown,
                mode=q.mode, vehicle=vehicle,
            )

            ans.confidence_score = scored['confidence']
            ans.confidence_tier = tier_to_choice.get(
                scored['tier'], ConfidenceTier.LOW)
            ans.verifier_doubts = scored['doubts']
            ans.auto_promoted = scored['auto_promoted']
            fields = ['confidence_score', 'confidence_tier',
                      'verifier_doubts', 'auto_promoted']
            if scored['auto_promoted']:
                ans.source = AnswerSource.AI_AUTO_VERIFIED
                ans.review_status = VerificationStatus.APPROVED
                fields += ['source', 'review_status']
            ans.save(update_fields=fields)

            if scored['auto_promoted'] and ans.source != AnswerSource.VERIFIED:
                try:
                    from .views import _promote_to_kb_auto
                    _promote_to_kb_auto(ans)
                except Exception:
                    logger.debug('[REPAIR_ATLAS] auto-promote skipped', exc_info=True)

            logger.info(
                '[REPAIR_ATLAS] verified answer %s → %s (%s%%)',
                answer_id, scored['tier'], scored['confidence'],
            )
    except Exception:
        logger.exception('[REPAIR_ATLAS] verify_repair_answer failed for %s', answer_id)
