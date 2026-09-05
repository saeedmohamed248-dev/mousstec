"""Pure-logic tests for the strategist + content helpers (no DB, no network)."""
from django.test import SimpleTestCase

from social_ads.services import content_ai, strategist


class _FakeMemory:
    def __init__(self, best):
        self._best = best
        self.best_hours = []

    def best_angles(self, top_n=3):
        return self._best[:top_n]


class StrategistHelperTests(SimpleTestCase):
    def test_parse_times(self):
        self.assertEqual(strategist._parse_times(["09:30", "20:00"]), [(9, 30), (20, 0)])
        self.assertEqual(strategist._parse_times(["bad", "25:99", ""]), [(1, 39)])

    def test_angle_rotation_frontloads_winners(self):
        mem = _FakeMemory(["نصيحة", "عرض_سعري"])
        rot = strategist._angle_rotation(mem, 4)
        self.assertEqual(len(rot), 4)
        self.assertEqual(rot[0], "نصيحة")
        self.assertEqual(rot[1], "عرض_سعري")

    def test_angle_rotation_no_memory(self):
        rot = strategist._angle_rotation(_FakeMemory([]), 3)
        self.assertEqual(len(rot), 3)
        self.assertTrue(all(a for a in rot))


class ContentParseTests(SimpleTestCase):
    def test_parse_json_plain(self):
        self.assertEqual(content_ai._parse_json('{"caption":"hi"}'), {"caption": "hi"})

    def test_parse_json_fenced(self):
        raw = '```json\n{"caption":"مرحبا","hashtags":"#x"}\n```'
        self.assertEqual(content_ai._parse_json(raw), {"caption": "مرحبا", "hashtags": "#x"})

    def test_parse_json_embedded(self):
        raw = 'هذا هو الناتج: {"caption":"ok"} انتهى'
        self.assertEqual(content_ai._parse_json(raw), {"caption": "ok"})

    def test_parse_json_garbage(self):
        self.assertIsNone(content_ai._parse_json("no json here"))
        self.assertIsNone(content_ai._parse_json(""))

    def test_clean_truncates(self):
        self.assertEqual(content_ai._clean("  hello  ", 100), "hello")
        self.assertEqual(len(content_ai._clean("x" * 50, 10)), 10)
