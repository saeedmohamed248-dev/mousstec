"""The robot talks shop (robot/workshop.py): wiring, a part's place, BMW/MINI upkeep — with the
system's own repair coach (Repair Atlas's «أسطى Mouss Tec»), said aloud — and general talk
that used to be cut short with «محتاج قطعة إيه؟»."""

from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import memory, services, views, workshop
from robot.brain import talk
from robot.models import RobotDevice


class TalkingShopTests(SimpleTestCase):

    def test_what_talking_shop_sounds_like(self):
        for said in ("ازاي اغير فلتر الزيت", "الضفيرة بتاعة الطرمبة ماشية فين في الـ F30",
                     "حساس الاكسجين بيعمل ايه", "العربية بتسخن ليه", "صيانة الـ N20 كل كام كيلو"):
            self.assertTrue(workshop.is_technical(said), said)
        for said in ("عندك فلتر زيت", "عندك حساس اكسجين؟", "بكام البوجيهات"):
            self.assertFalse(workshop.is_technical(said), said)       # a part to buy: the shop's answer
        self.assertTrue(workshop.asks_how("ازاي اغير فلتر الزيت"))
        self.assertFalse(workshop.asks_how("فلتر الزيت فين"))         # the shelf, maybe: the stock answers

    def test_its_mode(self):
        self.assertEqual(workshop.mode_of("الضفيرة بتاعة الطرمبة ماشية فين"), "wiring")
        self.assertEqual(workshop.mode_of("الحساس ده مكانه فين"), "locate")
        self.assertEqual(workshop.mode_of("ازاي اركب المساعدين"), "install")
        self.assertEqual(workshop.mode_of("ازاي افك الطرمبة"), "disassembly")
        self.assertEqual(workshop.mode_of("العربية بتسخن ليه"), "")

    def test_said_aloud(self):
        said = workshop.spoken("**أول حاجة** افصل البطارية.\n```mermaid\nflowchart LR\n```\n- وبعدين فك الغطا. أكمّلك؟")
        self.assertEqual(said, "أول حاجة افصل البطارية. وبعدين فك الغطا. أكمّلك؟")
        self.assertLessEqual(len(workshop.spoken("جملة طويلة. " * 200)), workshop.LONGEST)


class WorkshopVoiceTests(ERPTenantTestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-workshop", branch=branch)
        self.device.issue_token()
        self.device.save()
        make_inventory(make_product(name="فلتر زيت", part_number="11427953125", retail_price="450"), branch,
                       quantity=3)
        patcher = mock.patch.object(services, "shelf_location", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, text, said="**أول حاجة** افصل البطارية، وبعدين فك غطا الفلتر من فوق المكنة. أكمّلك؟"):
        asked = []

        def gemini(parts, **kwargs):
            asked.append(parts[0]["text"])
            return said
        with mock.patch("robot.audio.gemini_generate", side_effect=gemini):
            _intent, reply, payload = views._handle_voice(text, self.device)
        return reply, payload, asked

    def test_how_to_is_the_coachs_not_the_stocks(self):
        reply, payload, asked = self.ask("ازاي اغير فلتر الزيت")
        self.assertTrue(payload.get("workshop"), reply)
        self.assertEqual(reply, "أول حاجة افصل البطارية، وبعدين فك غطا الفلتر من فوق المكنة. أكمّلك؟")
        self.assertNotIn("موجود", reply)                              # not «أيوه، فلتر زيت موجود»
        self.assertIn("أسطى Mouss Tec", asked[0])                       # the system's own repair coach…
        self.assertIn("بصوت عالي في المحل", asked[0])                   # …said aloud
        self.assertNotIn(services.STAFF_CALL, reply)

    def test_wiring_with_the_car_and_the_shops_verified_knowledge(self):
        from repair_atlas.models import VerifiedKnowledge
        VerifiedKnowledge.objects.create(brand_norm="bmw", mode="wiring", part_or_system_norm="ضفيرة طرمبة",
                                         question_pattern="ضفيرة الطرمبة",
                                         answer_markdown="**الضفيرة** من فيوز F12 للطرمبة، سلك أحمر 2.5mm².")
        memory.hear_car(self.device, "عربيتي بي ام F30")
        reply, payload, asked = self.ask("الضفيرة بتاعة الطرمبة ماشية فين", said="من فيوز F12. أكمّلك؟")
        self.assertTrue(payload.get("workshop"))
        self.assertIn("bmw F30", asked[0])                              # the customer's car
        self.assertIn("الضفيرة من فيوز F12 للطرمبة", asked[0])            # the verified answer, to rely on

    def test_general_talk_gets_an_answer_not_which_part(self):
        reply, payload, _asked = self.ask("ايه رايك في الجو النهارده", said="الجو حلو النهارده، ربنا يديمها.")
        self.assertEqual(reply, "الجو حلو النهارده، ربنا يديمها.")
        reply, payload, _asked = self.ask("عندك")                       # (Vosk lost the rest): asked
        self.assertEqual(reply, talk.ASK_PART)

    def test_buying_a_part_is_still_the_shops_answer(self):
        reply, payload, asked = self.ask("عندك فلتر زيت")
        self.assertIn("فلتر زيت", reply)
        self.assertFalse(payload.get("workshop"))
        self.assertEqual(asked, [])                                    # no AI for it

    def test_the_engine_alone_answers_the_part_asked_before(self):
        # brain-0.9.10's log: «عندك فلتر زيت، إيه فسيتي؟» then «موتور إن توينتي» went to the AI,
        # which asked the car again — in English, once.
        reply, payload, asked = self.ask("عندك فلتر زيت ايه فسيتي")
        self.assertIn("فلتر زيت", reply)
        memory.note_turn(self.device, "عندك فلتر زيت ايه فسيتي", reply)    # (/voice/ keeps the turn)
        reply, payload, asked = self.ask("موتور إن توينتي")
        self.assertIn("فلتر زيت", reply)
        self.assertEqual(asked, [])                                    # no AI for it

    def test_an_english_answer_to_an_arabic_question_isnt_said(self):
        reply, payload, _asked = self.ask("ايه رايك في الجو النهارده",
                                          said="Could you please confirm the exact model year?")
        self.assertNotIn("Could", reply)
