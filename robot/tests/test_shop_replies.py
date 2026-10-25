"""robot/brain/replies.py — «الردود الجاهزة»: said when its question was asked,
never instead of a part."""

import unittest

from robot.brain import replies

HOURS = {"questions": ["مواعيدكم", "بتفتحوا امتى", "بتقفلوا الساعه كام"], "answer": "بنفتح من 10 لـ 11."}
WHERE = {"questions": ["العنوان فين", "المحل فين", "انتو فين", "عنوانكم"], "answer": "في المعادي."}
WARRANTY = {"questions": ["فيه ضمان", "الضمان كام"], "answer": "3 شهور على الجديد."}
SHOP = [HOURS, WHERE, WARRANTY]


class MatchTests(unittest.TestCase):

    def _said(self, text, shop=SHOP):
        found = replies.match(text, shop)
        return found and found[0]["answer"]

    def test_asked_the_way_people_ask(self):
        for said, reply in (("مواعيدكم ايه؟", HOURS), ("انتو بتفتحوا الساعة كام النهارده؟", HOURS),
                            ("بتقفلوا الساعه كام", HOURS), ("انتو فين", WHERE), ("انتو فين يا باشا", WHERE),
                            ("المحل فين بالظبط؟", WHERE), ("عنوانكم", WHERE), ("والعنوان؟", WHERE),
                            ("فيه ضمان؟", WARRANTY)):
            with self.subTest(said):
                self.assertEqual(self._said(said), reply["answer"])

    def test_a_part_question_is_not_a_reply(self):
        for said in ("عندك فلتر زيت؟", "فلتر الزيت فين", "بتفتحوا امتى وعندك فلتر زيت وطرمبة", "فين", "ممكن ايه",
                     "الساعه كام", ""):
            with self.subTest(said):
                self.assertIsNone(replies.match(said, SHOP))

    def test_the_rest_is_for_the_parts(self):
        reply, rest = replies.match("فيه ضمان على الطرمبة؟", SHOP)
        self.assertEqual((reply, rest), (WARRANTY, "الطرمبه"))     # the caller looks for a pump in it
        self.assertEqual(replies.match("مواعيدكم ايه؟", SHOP)[1], "")

    def test_never_a_reply_nobody_wrote(self):
        self.assertIsNone(replies.match("مواعيدكم", [{"questions": ["مواعيدكم"], "answer": "  "}]))

    def test_the_branchs_own_first(self):
        here = {"questions": ["العنوان فين"], "answer": "فرع مدينة نصر."}
        self.assertEqual(self._said("العنوان فين", [here, WHERE]), "فرع مدينة نصر.")

    def test_questions_one_per_line(self):
        self.assertEqual(replies.questions(" مواعيدكم \n\n بتفتحوا امتى\n"), ["مواعيدكم", "بتفتحوا امتى"])
