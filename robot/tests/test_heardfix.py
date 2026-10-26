"""robot/brain/heardfix.py — what the robot's ears write, and what was said."""

import unittest

from robot.brain import heardfix


class LearnTests(unittest.TestCase):

    def test_a_word_or_a_few_that_sound_alike(self):
        for heard, said, lesson in (("عندك فلترا الزي", "عندك فلتر زيت", ("فلترا الزي", "فلتر زيت")),
                                    ("عندك بوجيهات", "عندك بوجيه", ("بوجيهات", "بوجيه")),
                                    ("كاوتشا", "كاوتش", ("كاوتشا", "كاوتش")),
                                    ("بكم الفلتر", "بكام الفلتر", ("بكم", "بكام"))):
            with self.subTest(heard):
                self.assertEqual(heardfix.learn(heard, said), [lesson])

    def test_never_a_number_a_little_word_or_something_else(self):
        for heard, said in (("سجل مصروف مية جنيه", "سجل مصروف ميتين جنيه"),   # money
                            ("بعت 2 فلتر", "بعت 3 فلتر"),
                            # a digit of a part number read aloud: «واحد» is a number too
                            ("رقمها واحد اربعه", "رقمها واحده اربعه"),
                            ("في طرمبه", "فيه طرمبه"),                       # little words
                            ("انظر الى كابوس", "عندك فلتر هوا"),             # it heard something else
                            ("عندك طرمبه بنزين", "عندك طرمبه مياه"),
                            ("عندك فلتر زيت", "عندك فلتر زيت"), ("", "عندك فلتر")):
            with self.subTest(heard):
                self.assertEqual(heardfix.learn(heard, said), [])


class ApplyTests(unittest.TestCase):

    FIXES = [{"heard": "فلترا الزي", "means": "فلتر زيت"}, {"heard": "فلترا", "means": "فلتر"}]

    def test_the_longest_lesson_first(self):
        self.assertEqual(heardfix.apply("يا موس عندك فلترا الزي", self.FIXES),
                         ("يا موس عندك فلتر زيت", [("فلترا الزي", "فلتر زيت")]))
        self.assertEqual(heardfix.apply("عندك فلترا هوا", self.FIXES)[0], "عندك فلتر هوا")

    def test_nothing_to_fix_leaves_it_as_it_was(self):
        self.assertEqual(heardfix.apply("عندك فلتر زيت؟", self.FIXES), ("عندك فلتر زيت؟", []))
        self.assertEqual(heardfix.apply("عندك فلترا", []), ("عندك فلترا", []))
