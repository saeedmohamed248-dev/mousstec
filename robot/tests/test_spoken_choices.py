"""«تنضيف الأسامي»: a choice of parts is said the way a salesman says it, not as typed in the stock —
shorter, so it's voiced sooner too."""

import unittest

from robot import services


class SpokenChoicesTests(unittest.TestCase):

    def test_the_names_from_the_shops_log(self):
        names = ["طلمبه مياه b38 f30 f20 (F30 f20)", "طلمبه مياه إضافيه f30 b48 (F30)",
                 "طلمبه تبريد المياه الأضافية F30 - ORIGINAL (—)"]
        self.assertEqual(services.choices_said(names),
                         "عندي تلات أنواع طلمبه للـ F30: مياه B38 F20، مياه إضافيه B48، "
                         "تبريد المياه الأضافية أوريجينال. تقصد أنهي؟")

    def test_one_name_codes_capital_each_once_no_brackets(self):
        self.assertEqual(services.spoken_name("فلتر زيت n20 (N20) - oem"), "فلتر زيت N20 أوريجينال")

    def test_two_kinds_and_nothing_shared(self):
        self.assertEqual(services.choices_said(["فلتر زيت (E90)", "فلتر زيت (F30)"]),
                         "عندي نوعين فلتر زيت: E90، F30. تقصد أنهي؟")
        self.assertEqual(services.choices_said(["فانوس", "كشاف"]), "عندي نوعين: فانوس، كشاف. تقصد أنهي؟")

    def test_a_name_thats_all_of_anothers_is_said_whole(self):
        self.assertEqual(services.choices_said(["فلتر زيت", "فلتر زيت E90"]),
                         "عندي نوعين: فلتر زيت، فلتر زيت E90. تقصد أنهي؟")


class ThePartAskedForTests(unittest.TestCase):
    """«عندك فانوس خلفي X3؟» from the shop's log: brake pads, a wiper and shocks were offered."""

    def test_only_parts_named_by_it_are_offered(self):
        from robot.brain import knowledge

        k = knowledge.Knowledge({"parts": [
            {"part_number": "A", "name": "تيل خلفي TEXTAR E60 X3", "stock": 2, "retail_price": 100},
            {"part_number": "B", "name": "مساحة خلفي X3 E83 BOSCH", "stock": 2, "retail_price": 90},
            {"part_number": "C", "name": "فانوس خلفي X5", "stock": 1, "retail_price": 900},
        ]})
        part, choices = k.lookup("عندك فانوس خلفي X3")
        self.assertTrue(part is None or "فانوس" in part.name)
        self.assertTrue(all("فانوس" in c.name for c in choices))
        self.assertEqual(knowledge.the_part(["فانوس", "خلفي", "x3"]), "فانوس")
        self.assertEqual(knowledge.the_part(["x3", "مساعد"]), "مساعد")
