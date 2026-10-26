"""A part number read aloud, the way Vosk writes it: «واحد واحد اربعة اتنين…»
or in its printed groups «حداشر اتنين واربعين…». The brain finds the part with
the net down (robot/tests/test_brain_offline_db.py: the server says the same),
the voice entries read it as one, and a mobile said in groups is all there."""

import unittest
from unittest import mock

from robot import entries
from robot.brain import knowledge, talk

CATALOG = {
    "generated_at": "2026-10-26T09:00:00+02:00",
    "wake": {"name": "موس", "aliases": []},
    "parts": [
        {"part_number": "11427953125", "name": "فلتر زيت", "retail_price": 450.5, "stock": 3},
        {"part_number": "WP-E90", "name": "طرمبة مية", "car_model": "E90", "retail_price": 1800, "stock": 1,
         "oem": ["11517586925"], "additional": ["7586925"]},
    ],
}


class ReadAloudOfflineTests(unittest.TestCase):

    def setUp(self):
        self.k = knowledge.Knowledge(CATALOG)

    def test_digit_by_digit(self):
        r = self.k.answer("يا موس عندك واحد واحد اربعة اتنين سبعة تسعة خمسة تلاتة واحد اتنين خمسة؟", now=10.0)
        self.assertEqual(r.part.part_number, "11427953125")
        self.assertIn("بسعر 450 جنيه و50 قرش", r.text)

    def test_in_groups_egyptian_and_news_arabic(self):
        for said in ("يا موس بكام حداشر اتنين واربعين سبعة تسعمية تلاتة وخمسين مية خمسة وعشرين",
                     "يا موس رقمها أحد عشر اثنان وأربعون سبعة تسعمائة ثلاثة وخمسون مائة خمسة وعشرون"):
            with self.subTest(said):
                self.assertEqual(self.k.answer(said, now=10.0).part.part_number, "11427953125")

    def test_the_number_on_the_old_part_its_oem(self):
        r = self.k.answer("يا موس عندك واحد واحد خمسة واحد سبعة خمسة تمانية ستة تسعة اتنين خمسة؟", now=10.0)
        self.assertEqual(r.part.part_number, "WP-E90")
        self.assertEqual(self.k.answer("يا موس عندك 11517586925؟", now=50.0).part.part_number, "WP-E90")

    def test_a_piece_of_a_number_is_not_the_part(self):
        # «7586925» is a whole extra number of the pump; «758692» is in it, not it.
        self.assertEqual(self.k.answer("يا موس عندك 7586925؟", now=10.0).part.part_number, "WP-E90")
        self.assertIsNone(self.k.answer("يا موس عندك 758692؟", now=90.0).part)

    def test_understood_so_no_teacher_needed(self):
        self.assertTrue(self.k.understands("يا موس عندك واحد واحد اربعة اتنين سبعة تسعة خمسة تلاتة واحد اتنين خمسة"))


class ReadAloudEntriesTests(unittest.TestCase):

    def test_a_sale_reads_it_as_one(self):
        with mock.patch.object(entries, "_a_part_number", lambda code: code == "11427953125"):
            said = entries._said("بعت اتنين واحد واحد اربعة اتنين سبعة تسعة خمسة تلاتة واحد اتنين خمسة كاش")
            self.assertEqual(said, "بعت 2 11427953125 كاش")              # two of it: only 114… is a part
            self.assertEqual(entries._said("بعت واحد واحد اربعة اتنين سبعة تسعة خمسة تلاتة واحد اتنين خمسة"),
                             "بعت 11427953125")
        self.assertEqual(entries._counted("2 11427953125"), (2, "11427953125"))

    def test_a_part_number_is_never_a_count(self):
        self.assertEqual(entries._counted("11427953125 فلتر زيت"), (1, "11427953125 فلتر زيت"))
        self.assertEqual(entries._counted("12 فلتر زيت"), (12, "فلتر زيت"))


class MobileSaidTests(unittest.TestCase):

    def test_digit_by_digit_in_groups_and_written(self):
        for said in ("صفر واحد صفر واحد اتنين تلاتة اربعة خمسة ستة سبعة تمانية",
                     "رقمي صفر عشرة واحد اتنين تلاتة اربعة خمسة ستة سبعة تمانية",
                     "زيرو عشرة اتناشر تلاتة اربعة خمسة ستة سبعة تمانية",
                     "010 1234 5678", "+20 10 1234 5678",
                     "zero one zero one two three four five six seven eight"):
            with self.subTest(said):
                self.assertEqual(talk.mobile_in(said), "01012345678")

    def test_a_part_number_is_not_a_mobile_tried(self):
        self.assertFalse(talk.tried_a_number("عندك 11427953125؟"))
        self.assertTrue(talk.tried_a_number("صفر عشرة واحد اتنين تلاتة اربعة"))     # 0101234: too short
