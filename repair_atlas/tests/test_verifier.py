"""Tests for the Self-Verifier loop."""
from __future__ import annotations

import json
from unittest.mock import patch

from django.test import TestCase

from repair_atlas.services.verifier import (
    verify_answer, tier_from_confidence,
    AUTO_PROMOTE_THRESHOLD, RETRY_FLOOR,
)
from repair_atlas.services.repair_coach import coach_reply


class TierTest(TestCase):
    def test_tier_high(self):
        self.assertEqual(tier_from_confidence(95), 'high')
        self.assertEqual(tier_from_confidence(AUTO_PROMOTE_THRESHOLD), 'high')

    def test_tier_medium(self):
        self.assertEqual(tier_from_confidence(70), 'medium')
        self.assertEqual(tier_from_confidence(RETRY_FLOOR), 'medium')

    def test_tier_low(self):
        self.assertEqual(tier_from_confidence(40), 'low')
        self.assertEqual(tier_from_confidence(0), 'low')


class VerifierParseTest(TestCase):
    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_clean_json_parsed(self, mock_llm):
        mock_llm.return_value = json.dumps({
            'confidence': 90,
            'verdict': 'pass',
            'doubts': [],
            'suggested_revision': None,
        })
        out = verify_answer(question='q', answer='a', mode='disassembly')
        self.assertEqual(out['confidence'], 90)
        self.assertEqual(out['verdict'], 'pass')
        self.assertEqual(out['doubts'], [])

    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_json_extracted_from_noisy_text(self, mock_llm):
        mock_llm.return_value = (
            'هاي JSON الفحص: {"confidence": 70, "verdict": "revise", '
            '"doubts": ["العزم غير محدد للموديل"], '
            '"suggested_revision": "نص محسّن"} انتهى.'
        )
        out = verify_answer(question='q', answer='a', mode='disassembly')
        self.assertEqual(out['confidence'], 70)
        self.assertEqual(out['verdict'], 'revise')
        self.assertEqual(len(out['doubts']), 1)
        self.assertEqual(out['suggested_revision'], 'نص محسّن')

    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_garbage_falls_back_to_medium(self, mock_llm):
        mock_llm.return_value = 'no json here at all'
        out = verify_answer(question='q', answer='a', mode='disassembly')
        # fallback returns confidence=50, verdict=revise
        self.assertEqual(out['confidence'], 50)
        self.assertEqual(out['verdict'], 'revise')

    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_confidence_clamped(self, mock_llm):
        mock_llm.return_value = json.dumps({
            'confidence': 9999, 'verdict': 'pass', 'doubts': [],
        })
        out = verify_answer(question='q', answer='a', mode='disassembly')
        self.assertEqual(out['confidence'], 100)

    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_invalid_verdict_inferred_from_confidence(self, mock_llm):
        mock_llm.return_value = json.dumps({
            'confidence': 92, 'verdict': 'whatever', 'doubts': [],
        })
        out = verify_answer(question='q', answer='a', mode='disassembly')
        self.assertEqual(out['verdict'], 'pass')

    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_crash_returns_conservative_fallback(self, mock_llm):
        mock_llm.side_effect = RuntimeError('boom')
        out = verify_answer(question='q', answer='a', mode='disassembly')
        self.assertEqual(out['confidence'], 50)
        self.assertEqual(out['verdict'], 'revise')


class CoachVerifierLoopTest(TestCase):
    """End-to-end branching in coach_reply based on Verifier verdict."""

    def _gen_then_verify(self, gen_text, verify_payload):
        """Helper — first call returns gen_text, second returns verify_payload as JSON."""
        responses = iter([gen_text, json.dumps(verify_payload)])
        def side_effect(*args, **kwargs):
            return next(responses)
        return side_effect

    @patch('repair_atlas.services.repair_coach.call_llm_layer')
    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_high_confidence_auto_promoted(self, mock_verify, mock_gen):
        mock_gen.return_value = 'خطوات الفك الكاملة...'
        mock_verify.return_value = json.dumps({
            'confidence': 92, 'verdict': 'pass', 'doubts': [],
        })
        out = coach_reply(
            'إزاي أفك الدينمو؟', mode='disassembly',
            vehicle={'brand': 'Hyundai', 'model_name': 'Elantra'},
        )
        self.assertTrue(out['success'])
        self.assertEqual(out['tier'], 'high')
        self.assertEqual(out['confidence'], 92)
        self.assertTrue(out['auto_promoted'])
        self.assertEqual(out['source'], 'ai_auto_verified')

    @patch('repair_atlas.services.repair_coach.call_llm_layer')
    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_low_confidence_flagged_not_promoted(self, mock_verify, mock_gen):
        mock_gen.return_value = 'إجابة عمومية بدون تفاصيل'
        mock_verify.return_value = json.dumps({
            'confidence': 40, 'verdict': 'reject',
            'doubts': ['الرد عام ومش محدد للموديل'],
        })
        out = coach_reply(
            'إزاي أفك الدينمو؟', mode='disassembly',
            vehicle={'brand': 'Hyundai'},
        )
        self.assertTrue(out['success'])
        self.assertEqual(out['tier'], 'low')
        self.assertFalse(out['auto_promoted'])
        self.assertEqual(out['source'], 'llm')
        self.assertEqual(len(out['doubts']), 1)

    @patch('repair_atlas.services.repair_coach.call_llm_layer')
    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_medium_revise_triggers_one_retry(self, mock_verify, mock_gen):
        # First Generator call → original answer
        # First Verifier call → revise with suggestion
        # Second Verifier call → pass after suggestion accepted
        mock_gen.return_value = 'رد أولي'
        verify_responses = iter([
            json.dumps({
                'confidence': 70, 'verdict': 'revise',
                'doubts': ['ينقصه عزم التربيط'],
                'suggested_revision': 'رد محسّن مع عزم 8 Nm',
            }),
            json.dumps({
                'confidence': 88, 'verdict': 'pass',
                'doubts': [], 'suggested_revision': None,
            }),
        ])
        mock_verify.side_effect = lambda *a, **kw: next(verify_responses)

        out = coach_reply(
            'إزاي أفك الحساس؟', mode='disassembly',
            vehicle={'brand': 'Toyota'},
        )
        self.assertTrue(out['success'])
        self.assertEqual(out['answer'], 'رد محسّن مع عزم 8 Nm')
        self.assertEqual(out['revisions'], 1)
        self.assertEqual(out['tier'], 'high')  # rose to high after revision
        self.assertTrue(out['auto_promoted'])
