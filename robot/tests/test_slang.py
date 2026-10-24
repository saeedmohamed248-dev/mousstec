"""robot/brain/slang.py — what a part is called in an Egyptian shop."""

import unittest

from robot.brain import slang


class SlangTests(unittest.TestCase):

    def test_one_part_in_its_spellings(self):
        for said, written in (("كاوتش", "اطار"), ("بوجيهات", "شمعه"), ("طلمبه", "طرمبه"),
                              ("ردياتير", "رديتر"), ("دبرياج", "كلتش"), ("رولمان", "بلي"),
                              ("شكمان", "اشطمان"), ("مارش", "سلف"), ("دينامو", "ترنيتر"),
                              ("اكصدام", "صدام"), ("فلترا", "فلتر")):
            with self.subTest(said):
                self.assertIn(written, slang.forms(said))

    def test_apart_what_a_mechanic_keeps_apart(self):
        self.assertNotIn("كشاف", slang.forms("فانوس"))       # a tail light or a head light
        self.assertNotIn("فرامل", slang.forms("تيل"))         # the pads, not every brake part
        self.assertEqual(slang.forms("طرمبه") & slang.forms("زيت"), set())

    def test_two_words_said_as_one_thing(self):
        self.assertEqual(slang.rewrite("عايز بريك باد امامي"), "عايز تيل امامي")
        self.assertEqual(slang.rewrite("اويل فلتر كورولا"), "فلتر زيت كورولا")
        self.assertEqual(slang.rewrite("بريكات"), "بريكات")     # only the whole phrase

    def test_the_way_a_shop_asks(self):
        for w in ("عامل", "باشا", "هندسه", "اسطي", "تجيبلي", "معلم"):
            self.assertIn(w, slang.FILLERS)


if __name__ == "__main__":
    unittest.main()
