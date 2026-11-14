"""robot/brain/nearby.py — the part that was nearly said: offline, Vosk writes a word a letter
off and the shelf's own part came back «ملقتش القطعة دي». The nearest part is offered («تقصد …؟»),
its price said only after «أيوه» — and nothing is offered for talk that names no part."""

import unittest

from robot.brain import knowledge, nearby

NAMES = ["فلتر زيت", "فلتر هواء", "فلتر بنزين", "طرمبة مياه", "طرمبة بنزين", "تيل فرامل أمامي", "تيل فرامل خلفي",
         "طنبورة فرامل", "مقص أمامي", "بوجيه", "سير مكنة", "كويل اشعال", "حساس اكسجين", "رديتر مياه",
         "مساعد أمامي", "جوان وش سلندر", "دبرياج كامل", "كمبروسر تكييف"]
CATALOG = {"parts": [{"part_number": f"PN{i:04d}", "name": name, "retail_price": 100 + i * 10, "stock": 3}
                     for i, name in enumerate(NAMES)]}
HEARD = {"عندك فلترا الزي": "فلتر زيت", "بكام طلمبه ميه": "طرمبة مياه", "تيل فرامن امامي": "تيل فرامل أمامي",
         "عايز طنبوره فرامل": "طنبورة فرامل", "المقس الامامي": "مقص أمامي", "كويل اشعاع": "كويل اشعال",
         "حساس اكسيجين": "حساس اكسجين", "ردياتير ميه": "رديتر مياه", "جوان وش سلندل": "جوان وش سلندر",
         "كمبروسر تكيف": "كمبروسر تكييف"}
TALK = ["عايز اسجل فاتوره بيع", "ازيك عامل ايه", "عند قطع غيار", "الجو حر النهارده", "عندك شكمان",
        "فين اقرب بنزينه", "ساكن فين"]


class NearbyTests(unittest.TestCase):

    def setUp(self):
        self.k = knowledge.Knowledge(CATALOG)

    def near(self, text):
        keys = [w for w in knowledge.part_words(text) if len(w) >= 2 and w not in knowledge._QUESTION_WORDS]
        return [p.name for p in nearby.nearest(keys, self.k.parts, knowledge._forms)]

    def test_a_letter_off_is_still_the_part(self):
        for heard, name in HEARD.items():
            with self.subTest(heard):
                self.assertEqual(self.near(heard)[:1], [name])

    def test_talk_names_no_part(self):
        for said in TALK:
            with self.subTest(said):
                self.assertEqual(self.near(said), [])

    def test_as_it_sounds(self):
        self.assertEqual(nearby.sounds("طلمبة"), nearby.sounds("تلمبه"))
        self.assertEqual(nearby.alike("طلمبه", "طرمبه"), nearby.NEAR)
        self.assertEqual(nearby.alike("زي", "زيت"), nearby.START)
        self.assertEqual(nearby.alike("زيت", "بنزين"), 0.0)
        self.assertEqual(nearby.off_by("فرامن", "فرامل", 1), 1)
        self.assertEqual(nearby.off_by("فلتر", "كمبروسر", 2), 3)          # stops counting past `most`

    # ---- on the robot: offered, never answered as if it were heard
    def test_offered_then_priced_on_a_yes(self):
        reply = self.k.answer("يا موس بكام طلمبه ميه", now=10.0)
        self.assertEqual(reply.text, f"{knowledge.MEAN} طرمبة مياه؟ {knowledge.SAY_FULL}")
        self.assertNotIn("جنيه", reply.text)                             # no price for a guess
        reply = self.k.answer("أيوه", now=12.0)
        self.assertIn("طرمبة مياه", reply.text)
        self.assertIn("130 جنيه", reply.text)

    def test_said_right_it_is_answered_at_once_as_before(self):
        reply = self.k.answer("يا موس بكام طرمبة مياه", now=10.0)
        self.assertIn("130 جنيه", reply.text)

    def test_what_names_no_part_is_still_not_found(self):
        reply = self.k.answer("يا موس عندك شكمان", now=10.0)
        self.assertEqual(reply.text, knowledge.NOT_FOUND)

    def test_online_a_mishearing_still_goes_to_a_teacher(self):
        # `understands` decides what a teacher hears: a near miss isn't «understood».
        self.assertFalse(self.k.understands("يا موس بكام طلمبه ميه", now=10.0))
        self.assertTrue(self.k.understands("يا موس بكام طرمبة مياه", now=10.0))
