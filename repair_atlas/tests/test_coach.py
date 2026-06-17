"""Sanity tests for repair_atlas — لا تستدعي LLM فعلاً، بس تختبر الـ wiring."""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from repair_atlas.services.repair_coach import (
    coach_reply, _extract_part_hint, _normalize,
)


class HelpersTest(TestCase):
    def test_normalize_strips_and_lowers(self):
        self.assertEqual(_normalize('  Elantra  '), 'elantra')
        self.assertEqual(_normalize(''), '')
        self.assertEqual(_normalize('  '), '')

    def test_extract_part_hint_finds_arabic_keyword(self):
        self.assertEqual(
            _extract_part_hint('إزاي أفك حساس طلمبة الزيت في Elantra'),
            'حساس',
        )

    def test_extract_part_hint_finds_english_keyword(self):
        self.assertEqual(
            _extract_part_hint('how to test ABS module on E90'),
            'ABS',
        )

    def test_extract_part_hint_empty_for_unknown(self):
        self.assertEqual(_extract_part_hint('عربيتي بتطلع صوت'), '')


class CoachReplyTest(TestCase):
    def test_empty_input_returns_failure(self):
        out = coach_reply('', mode='disassembly', vehicle={})
        self.assertFalse(out['success'])
        self.assertEqual(out['source'], 'none')

    @patch('repair_atlas.services.verifier.call_llm_layer')
    @patch('repair_atlas.services.repair_coach.call_llm_layer')
    def test_llm_path_returns_answer(self, mock_gen, mock_verify):
        mock_gen.return_value = '1. افصل البطارية. 2. فك الكونيكتور.'
        # Verifier returns low-ish confidence so the source stays 'llm'
        mock_verify.return_value = '{"confidence": 70, "verdict": "pass", "doubts": []}'
        out = coach_reply(
            'إزاي أفك دينمو السيارة؟',
            mode='disassembly',
            vehicle={'brand': 'Hyundai', 'model_name': 'Elantra'},
        )
        self.assertTrue(out['success'])
        self.assertIn(out['source'], {'llm', 'ai_auto_verified'})
        self.assertIn('افصل البطارية', out['answer'])
        sent_messages = mock_gen.call_args[0][0]
        self.assertEqual(sent_messages[0]['role'], 'system')
        self.assertIn('أسطى Mouss Tec', sent_messages[0]['content'])

    @patch('repair_atlas.services.repair_coach.call_llm_layer')
    def test_invalid_mode_falls_back_to_disassembly(self, mock_llm):
        mock_llm.return_value = 'ok'
        out = coach_reply('test', mode='not_a_real_mode', vehicle={})
        self.assertEqual(out['mode'], 'disassembly')

    @patch('repair_atlas.services.repair_coach.call_llm_layer')
    def test_llm_crash_is_handled_gracefully(self, mock_llm):
        mock_llm.side_effect = RuntimeError('boom')
        out = coach_reply('test', mode='disassembly', vehicle={})
        self.assertFalse(out['success'])
        self.assertEqual(out['source'], 'error')

    @patch('repair_atlas.services.verifier.call_llm_layer')
    @patch('repair_atlas.services.repair_coach.call_llm_layer')
    def test_fast_path_skips_verification(self, mock_gen, mock_verify):
        """verify=False: الفني بياخد رده فوراً من غير ما الـ Verifier يشتغل."""
        mock_gen.return_value = '1. افصل البطارية. 2. فك الكونيكتور.'
        out = coach_reply(
            'إزاي أفك الدينمو؟', mode='disassembly',
            vehicle={'brand': 'BMW'}, verify=False,
        )
        self.assertTrue(out['success'])
        self.assertTrue(out['verification_pending'])
        self.assertEqual(out['tier'], 'pending')
        self.assertIsNone(out['confidence'])
        # الـ Verifier ماشتغلش في المسار السريع
        mock_verify.assert_not_called()


class CoachReplyStreamTest(TestCase):
    @patch('repair_atlas.services.repair_coach.stream_llm_text')
    def test_stream_yields_deltas_then_done(self, mock_stream):
        from repair_atlas.services.repair_coach import coach_reply_stream
        mock_stream.return_value = iter(['افصل ', 'البطارية ', 'الأول.'])
        events = list(coach_reply_stream(
            'إزاي أفك الدينمو؟', mode='disassembly', vehicle={'brand': 'BMW'}))
        kinds = [e['type'] for e in events]
        # لازم في deltas وبعدها done
        self.assertIn('delta', kinds)
        self.assertEqual(kinds[-1], 'done')
        done = events[-1]['result']
        self.assertTrue(done['success'])
        self.assertTrue(done['verification_pending'])
        self.assertEqual(done['tier'], 'pending')
        self.assertIn('افصل البطارية الأول.', done['answer'])

    @patch('repair_atlas.services.repair_coach.coach_reply')
    @patch('repair_atlas.services.repair_coach.stream_llm_text')
    def test_stream_falls_back_when_streaming_crashes(self, mock_stream, mock_coach):
        from repair_atlas.services.repair_coach import coach_reply_stream
        mock_stream.side_effect = RuntimeError('no stream')
        mock_coach.return_value = {
            'success': True, 'answer': 'رد احتياطي', 'source': 'llm',
            'tier': 'pending', 'verification_pending': True,
        }
        events = list(coach_reply_stream('سؤال', mode='disassembly', vehicle={}))
        self.assertEqual(events[-1]['type'], 'done')
        self.assertIn('رد احتياطي', events[-1]['result']['answer'])
        mock_coach.assert_called_once()

    def test_stream_empty_question_errors(self):
        from repair_atlas.services.repair_coach import coach_reply_stream
        events = list(coach_reply_stream('', mode='disassembly', vehicle={}))
        self.assertEqual(events[0]['type'], 'error')


class ScoreAnswerTest(TestCase):
    @patch('repair_atlas.services.verifier.call_llm_layer')
    def test_score_answer_returns_tier_without_rewriting(self, mock_verify):
        from repair_atlas.services.repair_coach import score_answer
        mock_verify.return_value = (
            '{"confidence": 80, "verdict": "pass", "doubts": []}')
        scored = score_answer(
            question='إزاي أفك الدينمو؟',
            answer='1. افصل البطارية. 2. فك الكونيكتور.',
            mode='disassembly', vehicle={'brand': 'BMW'},
        )
        self.assertIn('confidence', scored)
        self.assertIn(scored['tier'], {'high', 'medium', 'low'})
        self.assertIn('doubts', scored)
        self.assertIn('auto_promoted', scored)
