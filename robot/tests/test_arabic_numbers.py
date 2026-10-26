"""robot/brain/arabic_numbers.py — amounts and counts said in words, as digits.

Vosk writes what it hears in words, Egyptian or news Arabic; the books read
digits. A word that is something else too («مية» water, «تمن» a price, «ست»
a lady, «واحد» one of it) stays a word unless a number is being said.
"""

import unittest

from robot.brain.arabic_numbers import to_digits


class AmountsTests(unittest.TestCase):

    def test_egyptian_amounts(self):
        for said, written in (("مية وخمسين جنيه", "150 جنيه"), ("ميتين وخمسين", "250"),
                              ("ألفين وخمسمية", "2500"), ("ألف ونص", "1500"), ("مية وربع", "125"),
                              ("الفين ونص", "2500"), ("تلات تلاف", "3000"), ("خمستلاف ونص", "5500"),
                              ("خمسة وعشرين", "25"), ("خمستاشر", "15"), ("اتنين ألف وتلتمية", "2300"),
                              ("عشرين ألف", "20000"), ("مية ألف", "100000"), ("ألف وواحد", "1001"),
                              ("مليون ونص", "1500000"), ("خمس مية", "500")):
            with self.subTest(said):
                self.assertEqual(to_digits(said), written)

    def test_news_arabic_amounts(self):
        for said, written in (("مائتان وخمسون جنيها", "250 جنيها"), ("خمسمائة وخمسون", "550"),
                              ("مائتين", "200"), ("ثلاثة آلاف", "3000"), ("عشرة آلاف", "10000"),
                              ("خمسة عشر", "15"), ("أحد عشر", "11"), ("اثنا عشر", "12"),
                              ("ست عشرة", "16"), ("واحد وعشرين", "21")):
            with self.subTest(said):
                self.assertEqual(to_digits(said), written)

    def test_in_a_sentence(self):
        self.assertEqual(to_digits("سجل مصروف ميتين وخمسين جنيه بنزين"), "سجل مصروف 250 جنيه بنزين")
        self.assertEqual(to_digits("اشترينا تلاتة فلتر زيت بسعر خمسمية"),
                         "اشترينا 3 فلتر زيت بسعر 500")
        self.assertEqual(to_digits("تمن الفلتر ميتين"), "تمن الفلتر 200")
        self.assertEqual(to_digits("بعته بمية وخمسين،"), "بعته بـ 150،")

    def test_a_price_joined_to_ba(self):
        for said, written in (("بعت فلتر زيت بميتين", "بعت فلتر زيت بـ 200"),
                              ("اشترينا فلتر بخمسين جنيه", "اشترينا فلتر بـ 50 جنيه"),
                              ("بتلات تلاف", "بـ 3000"), ("بسعر مية", "بسعر 100")):
            with self.subTest(said):
                self.assertEqual(to_digits(said), written)

    def test_digits_stay_digits(self):
        self.assertEqual(to_digits("بعت 2 فلتر زيت بسعر 450"), "بعت 2 فلتر زيت بسعر 450")
        self.assertEqual(to_digits("2 ألف"), "2000")


class OtherWordsTests(unittest.TestCase):
    """Words that are a number only where a number is said."""

    def test_water_is_not_a_hundred(self):
        for said in ("بعت طرمبة مية", "بعت طرمبة مية وفلتر زيت", "بعت طرمبة مية و2 فلتر",
                     "سجل مصروف مية 50 جنيه"):                  # the water bill
            with self.subTest(said):
                self.assertIn("مية", to_digits(said))
        self.assertEqual(to_digits("مية جنيه"), "100 جنيه")
        self.assertEqual(to_digits("مصروف مية وخمسين"), "مصروف 150")

    def test_short_words_alone_are_words(self):
        for said in ("الست قالت خمس دقايق", "تمن الفلتر كام", "بست", "عشر دقايق", "بتمن", "نص ساعة",
                     "ربع كيلو"):
            with self.subTest(said):
                self.assertEqual(to_digits(said), said)
        self.assertEqual(to_digits("اتنين ونص"), "2 ونص")             # a half only of a hundred or more

    def test_one_of_it_is_not_a_digit(self):
        self.assertEqual(to_digits("بعت فلتر واحد"), "بعت فلتر واحد")
        self.assertEqual(to_digits("بعت واحد فلتر"), "بعت واحد فلتر")
        self.assertEqual(to_digits("واحد جنيه"), "1 جنيه")

    def test_nothing_said(self):
        self.assertEqual(to_digits(""), "")
        self.assertEqual(to_digits(None), "")
        self.assertEqual(to_digits("عندك فلتر زيت؟"), "عندك فلتر زيت؟")


class PartNumbersReadAloudTests(unittest.TestCase):
    """A part number read aloud — digit by digit or in its printed groups — is
    one code (arabic_numbers.spoken_codes); amounts and counts are not."""

    def test_digit_by_digit_and_in_groups(self):
        from robot.brain.arabic_numbers import spoken_codes

        for said in ("رقمها واحد واحد اربعة اتنين سبعة تسعة خمسة تلاتة واحد اتنين خمسة",
                     "حداشر اتنين واربعين سبعة تسعمية تلاتة وخمسين مية خمسة وعشرين",
                     "عندك حداشر اتنين و اربعين سبعة تسعمية وتلاتة وخمسين مية وخمسة وعشرين؟",
                     "رقمها أحد عشر اثنان وأربعون سبعة تسعمائة ثلاثة وخمسون مائة خمسة وعشرون",
                     "عايز اتنين، رقمها واحد واحد اربعة اتنين سبعة تسعة خمسة تلاتة واحد اتنين خمسة"):
            with self.subTest(said):
                self.assertIn("11427953125", spoken_codes(said))
        # A pause in the middle: the two halves together too.
        self.assertIn("11427953125", spoken_codes("واحد واحد اربعة اتنين، يعني سبعة تسعة خمسة تلاتة واحد اتنين خمسة"))
        self.assertEqual(spoken_codes("صفر تلاتة وخمسين اتنين اتنين"), ["05322"])

    def test_amounts_and_counts_are_not_codes(self):
        from robot.brain.arabic_numbers import spoken_codes

        for said in ("عندك تلاتة فلتر؟", "بكام ميتين وخمسين", "بين ميتين تلاتمية", "تلات تلاف وخمسمية",
                     "11 42 7 953 125", "عندك فلتر زيت؟", ""):
            with self.subTest(said):
                self.assertEqual(spoken_codes(said), [])

    def test_one_one_is_two_digits_not_one_and_one(self):
        # «واحد» isn't «و» + «احد»: the money reading leaves a part number's ones alone.
        self.assertEqual(to_digits("واحد واحد اربعة"), "واحد واحد 4")
        self.assertEqual(to_digits("ألف وواحد"), "1001")

    def test_written_as_one_for_the_books(self):
        from robot.brain.arabic_numbers import codes_to_digits

        self.assertEqual(codes_to_digits("بعت 2 واحد واحد اربعة اتنين سبعة تسعة خمسة تلاتة واحد اتنين خمسة كاش"),
                         "بعت 2 11427953125 كاش")
        self.assertEqual(codes_to_digits("عندك حداشر اتنين واربعين سبعة تسعمية تلاتة وخمسين مية خمسة وعشرين؟"),
                         "عندك 11427953125؟")
        for said in ("بعت اتنين فلتر زيت بأربعمية وخمسين", "مصروف ميتين وخمسين جنيه بنزين", "11 42 7 953 125"):
            with self.subTest(said):
                self.assertEqual(codes_to_digits(said), said)        # amounts stay words, digits as written

    def test_a_count_said_right_before_it(self):
        from robot.brain.arabic_numbers import codes_to_digits, spoken_codes

        said = "بعت اتنين واحد واحد اربعة اتنين سبعة تسعة خمسة تلاتة واحد اتنين خمسة كاش"
        self.assertIn("11427953125", spoken_codes(said))
        # The parts tell which it is: 2 of 114…, or a part numbered 2114….
        self.assertEqual(codes_to_digits(said, known=lambda code: code == "11427953125"), "بعت 2 11427953125 كاش")
        self.assertEqual(codes_to_digits(said, known=lambda code: code == "211427953125"), "بعت 211427953125 كاش")
        self.assertEqual(codes_to_digits(said), "بعت 211427953125 كاش")

    def test_every_digit_said(self):
        from robot.brain.arabic_numbers import said_digits

        self.assertEqual(said_digits("صفر عشرة مية وتلاتة وعشرين اربعة خمسة ستة سبعة"), "0101234567")
        self.assertEqual(said_digits("010 1234 5678"), "01012345678")
        self.assertEqual(said_digits("عندك فلتر"), "")

    def test_a_number_however_it_is_said(self):
        from robot.brain.arabic_numbers import is_number_word

        for word in ("واحد", "وخمسين", "تلات", "ميه", "صفر", "زيرو", "12", "١٢", "حداشر"):
            with self.subTest(word):
                self.assertTrue(is_number_word(word))
        for word in ("فلتر", "و", "وكالة", "ورا", ""):
            with self.subTest(word):
                self.assertFalse(is_number_word(word))
