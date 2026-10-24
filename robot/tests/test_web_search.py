"""Gemini with Google Search: the request asks for the search tool, and the
pages the answer was built from come back with it (robot/audio.py)."""

from unittest import mock

from django.test import SimpleTestCase, override_settings

from robot import audio, services


class GroundedGeminiTests(SimpleTestCase):

    def _reply(self):
        return mock.Mock(status_code=200, json=lambda: {"candidates": [{
            "content": {"parts": [{"text": "**الـ N52** بياخد 6.5 لتر [1]."}]},
            "groundingMetadata": {"groundingChunks": [
                {"web": {"title": "bmw.com", "uri": "https://v/1"}},
                {"web": {"title": "bmw.com", "uri": "https://v/1"}},
                {"web": {"title": "forum.de", "uri": "https://v/2"}}]}}]})

    @override_settings(GEMINI_API_KEY="k")
    def test_the_search_tool_is_asked_for_and_the_sources_kept(self):
        with mock.patch("requests.post", return_value=self._reply()) as post:
            answer, sources = services.web_answer("الـ N52 بياخد كام لتر زيت؟")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["tools"], [{"google_search": {}}])
        self.assertEqual(answer, "الـ N52 بياخد 6.5 لتر.")                  # no markdown, no [1] said aloud
        self.assertEqual([s["title"] for s in sources], ["bmw.com", "forum.de"])

    @override_settings(GEMINI_API_KEY="")
    def test_no_key_no_answer(self):
        self.assertEqual(services.web_answer("أي حاجة"), ("", []))

    @override_settings(GEMINI_API_KEY="k")
    def test_without_tools_nothing_changes(self):
        with mock.patch("requests.post", return_value=self._reply()) as post:
            audio.gemini_generate([{"text": "x"}], what="reply")
        self.assertNotIn("tools", post.call_args.kwargs["json"])
