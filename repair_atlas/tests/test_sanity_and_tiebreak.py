"""Tests for the new Sanity Sweep + Tie-Breaker layers."""
from __future__ import annotations

import json
from unittest.mock import patch

from django.test import TestCase

from repair_atlas.services.sanity import sweep
from repair_atlas.services.tie_breaker import tie_break
from repair_atlas.services.repair_coach import coach_reply


# ============================================================================
# Sanity Sweep — لا LLM، فحص حتمي
# ============================================================================
class SanitySweepTest(TestCase):
    def test_reasonable_torque_passes(self):
        r = sweep('اربط المسمار بعزم 8 Nm')
        self.assertTrue(r.ok)

    def test_impossible_torque_fails(self):
        r = sweep('اربط المسمار بعزم 5000 Nm')
        self.assertFalse(r.ok)
        self.assertEqual(len(r.failures), 1)

    def test_reasonable_voltage_passes(self):
        r = sweep('الـ Pin 3 لازم يدّيك 12 V')
        self.assertTrue(r.ok)

    def test_insane_voltage_fails(self):
        r = sweep('قياس 9999 V على البطارية')
        self.assertFalse(r.ok)

    def test_arabic_unit_recognized(self):
        r = sweep('عزم التربيط 10 نيوتن متر')
        self.assertTrue(r.ok)
        r2 = sweep('عزم التربيط 7000 نيوتن متر')
        self.assertFalse(r2.ok)

    def test_kohm_multiplied(self):
        r = sweep('المقاومة 5 kΩ')
        self.assertTrue(r.ok)  # 5000 Ω in range
        r2 = sweep('المقاومة 9999 kΩ')
        self.assertFalse(r2.ok)  # 9,999,000 Ω out of range

    def test_wire_gauge_range(self):
        self.assertTrue(sweep('سلك 1.5 mm²').ok)
        self.assertTrue(sweep('cable 35 mm2').ok)
        self.assertFalse(sweep('سلك 250 mm²').ok)

    def test_empty_text_is_ok(self):
        self.assertTrue(sweep('').ok)
        self.assertTrue(sweep('  ').ok)

    def test_multiple_failures_collected(self):
        r = sweep('عزم 5000 Nm والمقاومة 10 ميجا أوم')
        # the first one definitely fails
        self.assertFalse(r.ok)
        self.assertGreaterEqual(len(r.failures), 1)


# ============================================================================
# Tie-Breaker (V3)
# ============================================================================
class TieBreakerTest(TestCase):
    @patch('repair_atlas.services.tie_breaker.call_llm_layer')
    def test_approve_decision(self, mock_llm):
        mock_llm.return_value = json.dumps({
            'decision': 'approve', 'critical_issue': None,
            'reasoning': 'الادعاءات مدعمة بمصادر',
        })
        out = tie_break(
            question='q', answer='a', mode='disassembly',
            vehicle={'brand': 'Toyota'}, v2_doubts=[], v2_confidence=75,
        )
        self.assertEqual(out['decision'], 'approve')

    @patch('repair_atlas.services.tie_breaker.call_llm_layer')
    def test_overrule_carries_critical_issue(self, mock_llm):
        mock_llm.return_value = json.dumps({
            'decision': 'overrule',
            'critical_issue': 'لم يحذر من فصل البطارية',
            'reasoning': 'خطر صعق كهربائي',
        })
        out = tie_break(
            question='q', answer='a', mode='install',
            vehicle={}, v2_doubts=['x'], v2_confidence=70,
        )
        self.assertEqual(out['decision'], 'overrule')
        self.assertIn('البطارية', out['critical_issue'])

    @patch('repair_atlas.services.tie_breaker.call_llm_layer')
    def test_invalid_decision_falls_back_to_sustain(self, mock_llm):
        mock_llm.return_value = json.dumps({
            'decision': 'whatever', 'critical_issue': None, 'reasoning': '',
        })
        out = tie_break(question='q', answer='a', mode='wiring',
                         vehicle={}, v2_doubts=[], v2_confidence=70)
        self.assertEqual(out['decision'], 'sustain')

    @patch('repair_atlas.services.tie_breaker.call_llm_layer')
    def test_crash_falls_back_to_sustain(self, mock_llm):
        mock_llm.side_effect = RuntimeError('boom')
        out = tie_break(question='q', answer='a', mode='wiring',
                         vehicle={}, v2_doubts=[], v2_confidence=70)
        self.assertEqual(out['decision'], 'sustain')


# ============================================================================
# End-to-end: 3-layer pipeline branching
# ============================================================================
class FullPipelineTest(TestCase):

    @patch('repair_atlas.services.repair_coach.call_llm_layer')
    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_insane_number_forces_revision_and_drops_confidence(
        self, mock_verify, mock_gen,
    ):
        # Generator first returns answer with insane torque.
        # After revise prompt, returns a clean one.
        mock_gen.side_effect = [
            'اربط بعزم 5000 Nm',          # bad
            'اربط بعزم 8 Nm',              # clean retry
        ]
        # V2 always says pass — sanity must override anyway on first answer
        mock_verify.return_value = json.dumps({
            'confidence': 90, 'verdict': 'pass', 'doubts': [],
        })
        out = coach_reply(
            'إزاي أركّب الحساس؟', mode='install',
            vehicle={'brand': 'Toyota'},
        )
        self.assertTrue(out['success'])
        self.assertEqual(out['revisions'], 1)
        self.assertTrue(out['sanity_ok'])  # clean after retry
        self.assertEqual(out['answer'], 'اربط بعزم 8 Nm')

    @patch('repair_atlas.services.repair_coach.call_llm_layer')
    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_persistent_insanity_caps_confidence_at_low_tier(
        self, mock_verify, mock_gen,
    ):
        # Both Generator outputs are insane → sanity stays failed
        mock_gen.side_effect = ['عزم 5000 Nm', 'لسه عزم 9999 Nm']
        mock_verify.return_value = json.dumps({
            'confidence': 92, 'verdict': 'pass', 'doubts': [],
        })
        out = coach_reply('q', mode='install', vehicle={'brand': 'Toyota'})
        self.assertFalse(out['sanity_ok'])
        # confidence got clamped to ≤45 → tier=low
        self.assertLessEqual(out['confidence'], 45)
        self.assertEqual(out['tier'], 'low')
        self.assertFalse(out['auto_promoted'])

    @patch('repair_atlas.services.tie_breaker.call_llm_layer')
    @patch('repair_atlas.services.repair_coach.call_llm_layer')
    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_medium_tier_triggers_tie_breaker_approve(
        self, mock_verify, mock_gen, mock_tb,
    ):
        mock_gen.return_value = 'رد متوسط بدون أرقام'
        mock_verify.return_value = json.dumps({
            'confidence': 75, 'verdict': 'pass', 'doubts': ['تعميم'],
        })
        mock_tb.return_value = json.dumps({
            'decision': 'approve', 'critical_issue': None,
            'reasoning': 'مدعّم بمصادر',
        })
        out = coach_reply('q', mode='disassembly', vehicle={'brand': 'BMW'})
        self.assertEqual(out['tie_decision'], 'approve')
        self.assertEqual(out['tier'], 'high')
        self.assertTrue(out['auto_promoted'])

    @patch('repair_atlas.services.tie_breaker.call_llm_layer')
    @patch('repair_atlas.services.repair_coach.call_llm_layer')
    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_medium_tier_overrule_demotes_to_low(
        self, mock_verify, mock_gen, mock_tb,
    ):
        mock_gen.return_value = 'رد متوسط'
        mock_verify.return_value = json.dumps({
            'confidence': 70, 'verdict': 'pass', 'doubts': [],
        })
        mock_tb.return_value = json.dumps({
            'decision': 'overrule',
            'critical_issue': 'لا يوجد تحذير من فصل البطارية',
            'reasoning': 'خطر',
        })
        out = coach_reply('q', mode='install', vehicle={'brand': 'BMW'})
        self.assertEqual(out['tie_decision'], 'overrule')
        self.assertEqual(out['tier'], 'low')
        self.assertFalse(out['auto_promoted'])
        self.assertTrue(any('عيب جوهري' in d for d in out['doubts']))
