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


class SaidAlikeTests(unittest.TestCase):

    def test_letters_an_egyptian_says_alike(self):
        # In the shop: «سلطة الزيت بوش» was «فلتر الزيت بوش» (ط/ت said alike).
        self.assertEqual(heardfix.learn("سلطة الزيت بوش", "فلتر الزيت بوش"), [("سلطه", "فلتر")])
        self.assertEqual(heardfix.learn("عندك طرمبه بنزين", "عندك طرمبه مياه"), [])   # still something else


class SentenceTests(unittest.TestCase):

    def test_a_sentence_and_what_was_said(self):
        self.assertEqual(heardfix.sentence_lesson("عندك سلطة الزيت؟", "عندك فلتر زيت؟"),
                         ("عندك سلطه الزيت", "عندك فلتر زيت؟"))
        self.assertEqual(heardfix.sentence_lesson("عندك فلتر زيت", "عندك فلتر زيت"),
                         ("عندك فلتر زيت", "عندك فلتر زيت"))                  # the ears were right

    def test_never_a_number(self):
        for heard, said in (("سجل مصروف مية جنيه", "سجل مصروف ميتين جنيه"), ("بعت 2 فلتر", "بعت 3 فلتر"),
                            ("عايز واحد فلتر", "عايز اتنين فلتر"), ("عندك واحد واحد اربعه", "عندك 114"),
                            ("رقمها واحد واحد اربعة اتنين سبعة", "رقمها واحد واحد اربعة اتنين سبعة"),
                            ("", "عندك فلتر"), ("عندك فلتر", "")):
            with self.subTest(heard):
                self.assertIsNone(heardfix.sentence_lesson(heard, said))

    def test_water_is_not_a_number(self):
        # «طرمبة مية» is a water pump — the shop's most asked part, learned like any other.
        self.assertEqual(heardfix.sentence_lesson("عندك طرنبة مية", "عندك طرمبة مية"),
                         ("عندك طرنبه ميه", "عندك طرمبة مية"))

    def test_looked_up_however_it_is_written(self):
        lessons = {heardfix.sentence_key("عندك سلطه الزيت"): "عندك فلتر زيت"}
        self.assertEqual(heardfix.sentence("عندك سلطة الزيت؟", lessons), "عندك فلتر زيت")
        self.assertIsNone(heardfix.sentence("عندك سلطة الزيت بوش", lessons))
        self.assertIsNone(heardfix.sentence("عندك سلطة الزيت", {}))


class TrustedWordsTests(unittest.TestCase):
    """The words these ears write right: no teacher for a sentence made of them."""

    RIGHT = [("عندك فلتر زيت", "عندك فلتر زيت؟")] * heardfix.TRUST_SEEN + [("بكام الطرمبة", "بكام الطرمبة")] * 3

    def test_written_right_often_enough(self):
        words = heardfix.trusted(self.RIGHT)
        self.assertEqual(set(words), {"عندك", "فلتر", "زيت"})                # «بكام» seen 3 times: not yet

    def test_one_slip_in_a_few_is_too_many(self):
        pairs = self.RIGHT + [("عندك سلطه الزيت", "عندك فلتر زيت")]
        words = heardfix.trusted(pairs)
        self.assertIn("عندك", words)
        self.assertNotIn("سلطه", words)
        pairs += [("فلتر زيت", "فانوس زيت")]                                # «فلتر» written for «فانوس»
        self.assertNotIn("فلتر", heardfix.trusted(pairs))

    def test_never_a_number(self):
        pairs = [("عندك تلاته فلتر", "عندك تلاته فلتر")] * heardfix.TRUST_SEEN
        self.assertNotIn("تلاته", heardfix.trusted(pairs))

    def test_ears_that_leave_words_out_trust_nothing(self):
        # Vosk writing «عندك فلتر» for «عندك فلتر زيت بوش»: every word it wrote was right, and still wrong.
        pairs = [("عندك فلتر", "عندك فلتر زيت بوش")] * heardfix.TRUST_SEEN
        self.assertEqual(heardfix.trusted(pairs), [])

    def test_a_sentence_of_them(self):
        words = {"عندك", "فلتر", "زيت", "يا"}
        self.assertTrue(heardfix.all_trusted("عندك فلتر زيت؟", words))
        self.assertFalse(heardfix.all_trusted("عندك فلتر زيت بوش", words))   # «بوش»: a teacher hears it
        self.assertFalse(heardfix.all_trusted("يا", words))                 # nothing asked
        self.assertFalse(heardfix.all_trusted("", words))
