"""
🧪 Tests for the repair_atlas.tasks.verify_repair_answer Celery task.

This is the background scoring pass — the tech already sees their
answer, this task only adds the confidence + tier + doubts after the
fact, and auto-promotes high-confidence answers into VerifiedKnowledge.

We mock `score_answer` so the LLM never fires; the contract under
test is the persistence + auto-promotion side effects.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model

from inventory.tests.base import ERPTenantTestCase
from repair_atlas.models import (
    AnswerSource, ConfidenceTier, VerificationStatus,
    RepairAnswer, RepairMode, RepairQuery, RepairSession,
    VerifiedKnowledge,
)
from repair_atlas.tasks import verify_repair_answer


User = get_user_model()


class VerifyRepairAnswerTaskTests(ERPTenantTestCase):
    def setUp(self):
        super().setUp()
        from django.db import connection
        self.schema_name = connection.schema_name
        self.user = User.objects.create_user(
            username='task_tech', password='pw', email='tt@x.com',
        )
        self.sess = RepairSession.objects.create(
            user=self.user, brand='Toyota', model_name='Corolla', year=2018,
        )
        self.query = RepairQuery.objects.create(
            session=self.sess, mode=RepairMode.DISASSEMBLY,
            part_or_system='دينمو', question_text='إزاي أفك الدينمو؟',
        )
        self.answer = RepairAnswer.objects.create(
            query=self.query, body_markdown='1. افصل البطارية...',
            source=AnswerSource.LLM,
            review_status=VerificationStatus.PENDING,
            confidence_tier=ConfidenceTier.UNKNOWN,
        )

    @patch('repair_atlas.services.repair_coach.score_answer')
    def test_high_tier_score_promotes_to_kb(self, mock_score):
        mock_score.return_value = {
            'confidence': 92, 'tier': 'high',
            'doubts': [], 'auto_promoted': True,
            'sanity_ok': True, 'tie_decision': None,
        }

        verify_repair_answer(self.schema_name, self.answer.id)

        self.answer.refresh_from_db()
        self.assertEqual(self.answer.confidence_score, 92)
        self.assertEqual(self.answer.confidence_tier, ConfidenceTier.HIGH)
        self.assertTrue(self.answer.auto_promoted)
        # source + review_status upgrade only when auto-promoted
        self.assertEqual(self.answer.source, AnswerSource.AI_AUTO_VERIFIED)
        self.assertEqual(self.answer.review_status, VerificationStatus.APPROVED)
        # KB entry minted with the vehicle context from the session
        kb = VerifiedKnowledge.objects.filter(brand_norm='toyota').first()
        self.assertIsNotNone(kb)
        self.assertEqual(kb.mode, RepairMode.DISASSEMBLY)
        self.assertIn('دينمو', kb.part_or_system_norm)

    @patch('repair_atlas.services.repair_coach.score_answer')
    def test_medium_tier_score_does_not_promote(self, mock_score):
        mock_score.return_value = {
            'confidence': 70, 'tier': 'medium',
            'doubts': ['الـ pinout مش مؤكد'], 'auto_promoted': False,
            'sanity_ok': True, 'tie_decision': None,
        }

        verify_repair_answer(self.schema_name, self.answer.id)

        self.answer.refresh_from_db()
        self.assertEqual(self.answer.confidence_tier, ConfidenceTier.MEDIUM)
        self.assertFalse(self.answer.auto_promoted)
        # source stays LLM (no auto-promotion)
        self.assertEqual(self.answer.source, AnswerSource.LLM)
        self.assertEqual(self.answer.review_status, VerificationStatus.PENDING)
        self.assertEqual(self.answer.verifier_doubts, ['الـ pinout مش مؤكد'])
        self.assertEqual(VerifiedKnowledge.objects.count(), 0)

    @patch('repair_atlas.services.repair_coach.score_answer')
    def test_low_tier_score_keeps_pending(self, mock_score):
        mock_score.return_value = {
            'confidence': 40, 'tier': 'low',
            'doubts': ['الرقم غلط', 'خطوة مفقودة'],
            'auto_promoted': False,
            'sanity_ok': False, 'tie_decision': None,
        }

        verify_repair_answer(self.schema_name, self.answer.id)

        self.answer.refresh_from_db()
        self.assertEqual(self.answer.confidence_tier, ConfidenceTier.LOW)
        self.assertEqual(len(self.answer.verifier_doubts), 2)
        self.assertEqual(VerifiedKnowledge.objects.count(), 0)

    @patch('repair_atlas.services.repair_coach.score_answer')
    def test_task_is_safe_when_answer_id_missing(self, mock_score):
        """Stale enqueue — answer deleted by the time worker picks up."""
        ghost_id = 9_999_999
        # Must not raise; logs a warning and returns.
        verify_repair_answer(self.schema_name, ghost_id)
        mock_score.assert_not_called()

    @patch('repair_atlas.services.repair_coach.score_answer')
    def test_does_not_overwrite_answer_text(self, mock_score):
        """The whole point of the background pass is to score the answer
        the tech already saw, not to rewrite it under their feet."""
        original = self.answer.body_markdown
        mock_score.return_value = {
            'confidence': 95, 'tier': 'high',
            'doubts': [], 'auto_promoted': True,
            'sanity_ok': True, 'tie_decision': None,
        }
        verify_repair_answer(self.schema_name, self.answer.id)
        self.answer.refresh_from_db()
        self.assertEqual(self.answer.body_markdown, original)
