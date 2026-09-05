"""Pure-logic tests for the strategist + content helpers (no DB, no network)."""
from django.test import SimpleTestCase

from social_ads.services import content_ai, image_gen, page_analysis, strategist


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


class ImageGenHelperTests(SimpleTestCase):
    def test_absolute_passthrough_for_http(self):
        self.assertEqual(image_gen._absolute("https://x.s3.amazonaws.com/a.png"),
                         "https://x.s3.amazonaws.com/a.png")

    def test_absolute_prefixes_relative(self):
        url = image_gen._absolute("/media/social_ads/a.png")
        self.assertTrue(url.startswith("https://"))
        self.assertTrue(url.endswith("/media/social_ads/a.png"))

    def test_absolute_empty(self):
        self.assertEqual(image_gen._absolute(""), "")

    def test_to_bytes_from_b64(self):
        import base64
        payload = base64.b64encode(b"PNGDATA").decode()
        self.assertEqual(image_gen._to_bytes({"b64_json": payload}), b"PNGDATA")

    def test_to_bytes_none_when_empty(self):
        self.assertIsNone(image_gen._to_bytes({}))


class PageAnalysisAngleTests(SimpleTestCase):
    def test_guess_angle_question(self):
        self.assertEqual(page_analysis._guess_angle("إيه رأيكم في العرض ده؟"), "سؤال_تفاعلي")

    def test_guess_angle_price(self):
        self.assertEqual(page_analysis._guess_angle("خصم 20% على كل المنتجات"), "عرض_سعري")

    def test_guess_angle_testimonial(self):
        self.assertEqual(page_analysis._guess_angle("رأي عميل: تجربة رائعة وشكراً ليكم"), "شهادة_عميل")

    def test_guess_angle_default_and_empty(self):
        self.assertEqual(page_analysis._guess_angle("معلومة مفيدة عن الصيانة"), "نصيحة")
        self.assertEqual(page_analysis._guess_angle(""), "غير_مصنّف")
