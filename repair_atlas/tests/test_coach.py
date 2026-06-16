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
        self.assertIn('Master Repair Coach', sent_messages[0]['content'])

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
