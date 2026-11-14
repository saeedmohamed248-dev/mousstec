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
HEARD = {"عندك كمبروسور": "كمبروسر تكييف", "بكام الدبرياش": "دبرياج كامل", "عندك فلترا الزي": "فلتر زيت", "بكام طلمبه ميه": "طرمبة مياه", "تيل فرامن امامي": "تيل فرامل أمامي",
         "عايز طنبوره فرامل": "طنبورة فرامل", "المقس الامامي": "مقص أمامي", "كويل اشعاع": "كويل اشعال",
         "حساس اكسيجين": "حساس اكسجين", "ردياتير ميه": "رديتر مياه", "جوان وش سلندل": "جوان وش سلندر",
         "كمبروسر تكيف": "كمبروسر تكييف"}
TALK = ["عايز اسجل فاتوره بيع", "ازيك عامل ايه", "عند قطع غيار", "الجو حر النهارده", "عندك شكمان",
        "فين اقرب بنزينه", "ساكن فين", "عايز اروح", "انا جاي من بعيد", "الكهربا قطعت", "هات واحد شاي",
        "عندك مكان اركن", "حسابي كام", "فلوس"]


# The shop's own names: the chassis, the engine and the maker's name in each.
LONG = ["خرطوم علوي من موتور لمبرد الزيت F25 N20 - RAPRO", "حساس ساعة الزيت - F30 - BST", "فلتر زيت MINI - JP",
        "فلتر زيت - BOSCH - E90", "زيت موتور - SHELL", "طرمبة مياه كهرباء N20 F30 - PIERBURG",
        "تيل فرامل أمامي F10 - TEXTAR", "تيل فرامل خلفي E90 - ATE", "كويل اشعال N20 N55 - BOSCH",
        "مساعد أمامي يمين E90 - SACHS", "مقص أمامي سفلي شمال F30 - LEMFORDER", "رديتر مياه E60 N52 - NISSENS",
        "كمبروسر تكييف F10 N20 - DENSO", "جوان وش سلندر N46 - ELRING", "دبرياج كامل MINI R56 - LUK",
        "حساس اكسجين أمامي N20 - BOSCH", "طنبورة فرامل أمامي F30 - ZIMMERMANN", "سير مكنة N52 - CONTI",
        "بوجيه N20 - NGK", "قاعدة فلتر زيت N52 E90 - VAICO", "خرطوم رديتر سفلي E90 N46 - RAPRO"]
LONG_HEARD = {"بكام خلطوم": "خرطوم", "عايز كمبروسور": "كمبروسر تكييف", "بكام الدبرياش": "دبرياج كامل",
              "عندك حصاص اكسيجين": "حساس اكسجين", "بكام المقس": "مقص أمامي", "كويل اشعاع": "كويل اشعال",
              "طنبوره فرامن": "طنبورة فرامل"}


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

    def test_the_shops_long_names_are_scored_by_the_words_people_say(self):
        # 🐛 «most of the name has to be said» counted «F25», «N20», «RAPRO» too: with the shop's
        # own names a part a letter off was still «ملقتش القطعة دي».
        k = knowledge.Knowledge({"parts": [{"part_number": f"L{i}", "name": name, "retail_price": 100, "stock": 2}
                                           for i, name in enumerate(LONG)]})
        for heard, name in LONG_HEARD.items():
            with self.subTest(heard):
                reply = k.answer(f"يا موس {heard}", now=10.0)
                k._start_over()
                self.assertIn(name, reply.text)
                self.assertNotIn("جنيه", reply.text)                     # offered, or asked which: never priced
        for said in TALK:
            with self.subTest(said):
                keys = [w for w in knowledge.part_words(said) if len(w) >= 2 and w not in knowledge._QUESTION_WORDS]
                self.assertEqual(nearby.nearest(keys, k.parts, knowledge._forms), [])
        self.assertEqual(sorted(nearby.spoken({"خرطوم", "علوي", "من", "f25", "n20", "rapro"})), ["خرطوم", "علوي"])
        self.assertEqual(set(nearby.spoken({"e90", "jp"})), {"e90", "jp"})   # no word of it said: all of it

    def test_as_it_sounds(self):
        self.assertEqual(nearby.sounds("طلمبة"), nearby.sounds("تلمبه"))
        self.assertEqual(nearby.alike("طلمبه", "طرمبه"), nearby.NEAR)
        self.assertEqual(nearby.alike("زي", "زيت"), nearby.START)
        self.assertEqual(nearby.alike("زيت", "بنزين"), 0.0)
        self.assertEqual(nearby.off_by("فرامن", "فرامل", 1), 1)
        self.assertEqual(nearby.off_by("فلتر", "كمبروسر", 2), 3)          # stops counting past `most`

    # ---- on the robot: offered, never answered as if it were heard
    def test_offered_then_priced_on_a_yes(self):
        reply = self.k.answer("يا موس بكام الدبرياش", now=10.0)          # the catalog's search finds nothing
        self.assertEqual(reply.text, f"{knowledge.MEAN} دبرياج كامل؟ {knowledge.SAY_FULL}")
        self.assertNotIn("جنيه", reply.text)                             # no price for a guess
        reply = self.k.answer("أيوه", now=12.0)
        self.assertIn("دبرياج كامل", reply.text)
        self.assertIn("260 جنيه", reply.text)

    def test_one_of_several_is_asked_about_when_the_rest_of_it_is_nearest(self):
        # three parts have «أمامي»: before, all three were read out.
        reply = self.k.answer("يا موس المقس الامامي", now=10.0)
        self.assertEqual(reply.text, f"{knowledge.MEAN} مقص أمامي؟ {knowledge.SAY_FULL}")
        reply = self.k.answer("يا موس عندك فلتر", now=40.0)               # nothing to tell them apart: asked, as before
        self.assertIn(knowledge.WHICH, reply.text)

    def test_said_right_it_is_answered_at_once_as_before(self):
        reply = self.k.answer("يا موس بكام طرمبة مياه", now=10.0)
        self.assertIn("130 جنيه", reply.text)

    def test_what_names_no_part_is_still_not_found(self):
        reply = self.k.answer("يا موس عندك شكمان", now=10.0)
        self.assertEqual(reply.text, knowledge.NOT_FOUND)

    def test_every_offer_has_its_clips(self):
        reply = self.k.answer("يا موس عندك كمبروسور", now=10.0)
        self.assertTrue(set(reply.segments) <= set(self.k.clip_texts()), reply.segments)

    def test_online_a_mishearing_still_goes_to_a_teacher(self):
        # `understands` decides what a teacher hears: a near miss isn't «understood».
        self.assertFalse(self.k.understands("يا موس بكام الدبرياش", now=10.0))
        self.assertTrue(self.k.understands("يا موس بكام طرمبة مياه", now=10.0))
