"""🐛 مفتاح المخ الاحتياطي اتلزق في .env ومعاه كلام عربي/تنصيص ⇒ كل سؤال للروبوت كان
بيقع 500 (UnicodeEncodeError: 'latin-1' في robot/llm.py:_ask). دلوقتي بيتنضّف، ولو غلط بيتجاهل."""
from unittest import mock

from django.test import SimpleTestCase, override_settings

from robot import llm


class BackupKeyCleaningTests(SimpleTestCase):

    @override_settings(OPENAI_API_KEY=' "sk-abc123"\u200b ', TOGETHER_API_KEY='')
    def test_quotes_spaces_and_invisible_chars_are_stripped(self):
        self.assertEqual(llm._openai_key(), 'sk-abc123')

    @override_settings(OPENAI_API_KEY='sk-abc ضع المفتاح هنا', TOGETHER_API_KEY='')
    def test_a_key_with_arabic_text_is_ignored_not_a_crash(self):
        self.assertEqual(llm._openai_key(), '')
        self.assertFalse(llm.has_backup())
        self.assertEqual(llm.backup('system', 'بكام الفلتر؟'), '')

    def test_a_non_ascii_header_never_raises(self):
        with mock.patch('requests.post', side_effect=UnicodeEncodeError('latin-1', 'x', 0, 1, 'bad')):
            self.assertEqual(llm._ask('http://x', 'k', 'm', [], json_out=False, timeout=1, max_tokens=5), '')
