"""robot/brain/talk.py — when a sentence follows on the last one, and the
words that answer it. No database: the rules alone (the server and the
brain offline both use them; test_memory_db.py asks both).
"""

from django.test import SimpleTestCase

from robot.brain import talk

PUMPS = [{"name": "طرمبة مية", "model": "E90"}, {"name": "طرمبة مية", "model": "F30"}]
FILTERS = [{"name": "فلتر زيت", "model": ""}, {"name": "فلتر هوا", "model": ""}]


class PickTests(SimpleTestCase):

    def test_an_answer_picks(self):
        for said, choices, picked in (
                ("E90", PUMPS, 0), ("التانية", PUMPS, 1), ("الأولانية", PUMPS, 0),
                ("طب و الـ F30", PUMPS, 1), ("طرمبة المية بتاعة E90", PUMPS, 0),
                ("the second one", PUMPS, 1), ("الهوا", FILTERS, 1), ("بتاع الزيت", FILTERS, 0)):
            with self.subTest(said):
                self.assertEqual(talk.pick_choice(said, choices), picked)

    def test_not_an_answer(self):
        for said, choices in (
                ("عندك فلتر زيت E90؟", PUMPS),      # a new question
                ("فيه تاني؟", PUMPS),               # «another one?», not «the second»
                ("G20", PUMPS),                     # none of them
                ("E90 ولا F30", PUMPS),             # both
                ("فلتر", FILTERS),                  # all of them
                ("فلتر بنزين", FILTERS),
                ("التالتة", PUMPS),                 # there's no third
                ("E90", [])):
            with self.subTest(said):
                self.assertIsNone(talk.pick_choice(said, choices))


class FollowUpTests(SimpleTestCase):

    def test_what_is_asked(self):
        for said, kind in (
                ("وبكام؟", "price"), ("بكام ده", "price"), ("فيه كام؟", "price"), ("موجود؟", "price"),
                ("وده؟", "price"), ("how much is it?", "price"),
                ("عليه ضمان؟", "warranty"), ("والضمان؟", "warranty"),
                ("ماركته إيه؟", "brand"), ("هو ده أصلي ولا تقليد؟", "brand"), ("جديد ولا مستعمل؟", "brand"),
                ("بيركب على E90؟", "fits"), ("وعلى F30؟", "fits"), ("وعلى X5؟", "fits"), ("ينفع لعربيتي؟", "fits"),
                ("does it fit E90?", "fits"),
                ("مكانها فين؟", "where"), ("حاطينه فين؟", "where"), ("where is it?", "where"),
                ("في أنهي فرع؟", "branches"), ("موجود في فرع تاني؟", "branches"), ("والفروع التانية؟", "branches"),
                ("which branch?", "branches"),
                ("فيه أرخص؟", "cheaper"), ("is there a cheaper one?", "cheaper"),
                ("فيه بديل؟", "alternatives"), ("فيه حاجة تانية؟", "alternatives"),
                ("فيه تاني؟", "alternatives"), ("فيه نوع تاني؟", "alternatives")):
            with self.subTest(said):
                self.assertEqual(talk.follow_up(said), kind)

    def test_not_a_follow_up(self):
        for said in ("عندك فلتر زيت؟", "وبكام طرمبة المية؟", "طيب", "لا", "شكرا", "ده بيتغير كل قد إيه؟", "",
                     "QQ999"):                     # a part number: a new question, not a car model
            with self.subTest(said):
                self.assertIsNone(talk.follow_up(said))


class AnswerTests(SimpleTestCase):

    def test_months(self):
        self.assertEqual([talk.months(n) for n in (1, 2, 3, 6, 12, 18, 24)],
                         ["شهر", "شهرين", "3 شهور", "6 شهور", "سنة", "18 شهر", "سنتين"])

    def test_what_it_fits_comes_from_the_catalog(self):
        self.assertEqual(talk.models_of(["f30", "E90"], "E90 / E91 2008"), ["F30", "E90", "E91"])
        self.assertEqual(talk.models_of(None, ""), [])
        self.assertEqual(talk.fits("x", [], ["E90"])[0],
                         "التوافق مش متسجل لـ x. اسأل حد من الموظفين ومعاك رقم الشاسيه.")

    def test_cheaper_compares_only_real_prices(self):
        alts = [("a", 600), ("b", 300), ("c", 0), ("d", 450)]
        said, clips, picked = talk.alternatives("cheaper", "x", 500, alts)
        self.assertEqual(said, "فيه كمان: b بسعر 300 جنيه، d بسعر 450 جنيه.")
        self.assertEqual(picked, [1, 3])
        self.assertEqual(clips, ["فيه كمان:", "b", "بسعر 300 جنيه", "d", "بسعر 450 جنيه"])
        self.assertEqual(talk.alternatives("cheaper", "x", 0, alts)[0], "مفيش حاجة أرخص منه موجودة دلوقتي.")
        # No price set: said last, without a price.
        self.assertEqual(talk.alternatives("alternatives", "x", 0, [("c", 0), ("b", 300)])[0],
                         "فيه كمان: b بسعر 300 جنيه، c.")

    def test_every_answer_is_cut_into_clips_the_bank_keeps(self):
        for text, clips in (talk.warranty("x", 12), talk.warranty("x", 0), talk.brand("x", "", "used", 0),
                            talk.where("x", "B3", 2), talk.where("x", "", 2), talk.where("x", "B3", 0)):
            with self.subTest(text):
                fixed = [c for c in clips if c != "x"]
                self.assertTrue(all(c in talk.PHRASES or c in ("سنة", "B3") or c.startswith("مش موجود")
                                    or c == "أيوه،" for c in fixed), clips)


class WebQuestionTests(SimpleTestCase):

    def test_a_question_for_the_internet(self):
        for said in ("دوّر على النت على مواصفات زيت 5W30", "ممكن تدورلي ع النت على سعر الدولار",
                     "شوفلي في جوجل N52 بيستهلك زيت قد إيه", "اسأل جوجل عن كود P0420",
                     "search online for E90 oil capacity", "لو سمحت ابحث في الانترنت عن الكود ده"):
            with self.subTest(said):
                self.assertTrue(talk.web_question(said))

    def test_a_question_for_the_shop(self):
        for said in ("دورلي على فلتر زيت", "ابحث عن فلتر زيت", "عندك فلتر زيت؟", "النت فاصل؟"):
            with self.subTest(said):
                self.assertFalse(talk.web_question(said))
        self.assertTrue(talk.about_the_stock("عندك كوتش؟"))
        self.assertFalse(talk.about_the_stock("إيه الفرق بين الزيت الصناعي والمعدني؟"))


class MobileTests(SimpleTestCase):

    def test_a_mobile_however_its_said(self):
        for said in ("010 1234 5678", "رقمي ٠١٠١٢٣٤٥٦٧٨", "+20 10 12345678",
                     "صفر واحد صفر واحد اتنين تلاتة اربعة خمسة ستة سبعة تمانية"):
            with self.subTest(said):
                self.assertEqual(talk.mobile_in(said), "01012345678")

    def test_a_wrong_mobile_or_not_a_mobile_at_all(self):
        self.assertTrue(talk.tried_a_number("010 123 456"))
        self.assertFalse(talk.tried_a_number("11427953125"))             # a part number
        self.assertFalse(talk.tried_a_number("فلتر 3"))
        self.assertTrue(talk.just_yes("أيوه بلغني"))
        self.assertFalse(talk.just_yes("لا شكرا"))
