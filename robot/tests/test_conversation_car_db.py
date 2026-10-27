"""The robot keeps the customer's car through the conversation, and answers
what isn't about a part — the same words with the net down.

«عندكم قطع غيار بي ام؟» → «أيوه» (the shop's own reply); «طيب عندك طرمبة
مية؟» right after offers the BMW pumps, not the Mercedes one; after the
F30's, «والفلتر بتاعها؟» is the F30's filter. «السلام عليكم»، «شكراً يا
باشا»، «إنت مين؟»، «قول تاني» get an answer instead of «ملقتش القطعة دي».
A greeting, a goodbye, or the camera greeting someone new starts over.
Runs on a tenant schema; the offline brain is asked the same and must say
the server's words.
"""

import time
from unittest import mock

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import memory, services, views
from robot.brain import talk
from robot.brain.knowledge import Knowledge
from robot.models import RobotDevice, RobotReply

BMW_REPLY = "أيوه، إحنا متخصصين في قطع غيار BMW وميني."


class _Shop(ERPTenantTestCase):

    def setUp(self):
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="الرئيسي")
        self.device = RobotDevice(name="موس", device_uid="esp-car-context", branch=self.branch)
        self.token = self.device.issue_token()
        self.device.save()
        memory.forget(self.device)
        self.addCleanup(memory.forget, self.device)
        self.pumps, self.filters = {}, {}
        for model, price, number in (("E90", "1800", "11517586925"), ("F30", "2100", "11518635089"),
                                     ("W204", "1500", "A2712000801")):
            self.pumps[model] = make_product(name="طرمبة مية", part_number=number, car_model=model,
                                             retail_price=price)
            make_inventory(self.pumps[model], self.branch, quantity=2)
        for model, price, number in (("E90", "450", "11427953125"), ("F30", "520", "11428575211"),
                                     ("W204", "380", "A2711800009")):
            self.filters[model] = make_product(name="فلتر زيت", part_number=number, car_model=model,
                                               retail_price=price)
            make_inventory(self.filters[model], self.branch, quantity=3)
        RobotReply.objects.create(topic="cars", questions="عندكم قطع غيار بي ام", answer=BMW_REPLY)
        RobotReply.objects.create(topic="hours", questions="مواعيدكم", answer="من 9 الصبح لحد 10 بالليل.")

    def say(self, text):
        """What voice() does: answer, then keep the exchange for the next one."""
        with mock.patch.object(services, "ai_reply", return_value=""), \
                mock.patch.object(services, "shelf_location", return_value=""), \
                mock.patch.object(services, "maybe_raise_procurement_signal"):
            _intent, reply, _payload = views._handle_voice(text, self.device, None)
        memory.note_turn(self.device, text, reply)
        return reply


class CustomersCarTests(_Shop):

    def test_the_bmw_conversation(self):
        self.assertEqual(self.say("عندكم قطع غيار بي ام؟"), BMW_REPLY)
        # 🐛 كان بيعرض طرمبة المرسيدس كمان: «عندكم بي ام» اتقالت ومحدش افتكرها
        self.assertEqual(self.say("طيب عندك طرمبة مية؟"),
                         "عندي أكتر من صنف: طرمبة مية (E90)، طرمبة مية (F30). تقصد أنهي؟")
        self.assertIn("2100", self.say("التانية"))
        self.assertIn("2100", self.say("وبكام يا باشا؟"))     # 🐛 «يا باشا» كانت بتخليها سؤال جديد
        # 🐛 كان بيعرض فلاتر كل العربيات: عربية العميل (F30) اتنسيت
        self.assertEqual(self.say("طب والفلتر بتاعها؟"), "أيوه، فلتر زيت موجود، عندي منه 3 حتت، بسعر 520 جنيه.")
        self.assertEqual(memory.car(self.device), {"make": "bmw", "models": ["F30"]})

    def test_a_make_said_with_the_question(self):
        self.assertIn("1500", self.say("عربيتي مرسيدس، عندك طرمبة مية؟"))
        self.assertIn("380", self.say("وفلتر زيت؟"))

    def test_the_words_said_beat_the_car_remembered(self):
        self.say("عندك طرمبة مية F30؟")
        self.assertIn("1800", self.say("طب طرمبة مية E90؟"))
        self.assertEqual(memory.car(self.device)["models"], ["E90"])
        self.assertIn("450", self.say("والفلتر؟"))

    def test_no_car_known_asks_as_before(self):
        self.assertEqual(self.say("عندك طرمبة مية؟"),
                         "عندي أكتر من صنف: طرمبة مية (E90)، طرمبة مية (F30)، طرمبة مية (W204). تقصد أنهي؟")

    def test_a_new_customer_starts_over(self):
        self.say("عندك طرمبة مية F30؟")
        self.assertEqual(self.say("مع السلامة"), talk.BYE_BACK)
        self.assertEqual(memory.car(self.device), {})
        self.say("عندك طرمبة مية F30؟")
        self.assertIn("وعليكم السلام", self.say("السلام عليكم"))
        self.assertEqual((memory.car(self.device), memory.talked_about(self.device)), ({}, None))

    def test_the_camera_greeting_someone_new_forgets_after_a_pause(self):
        self.say("عندك طرمبة مية F30؟")
        memory.fresh_start(self.device)                         # mid-talk: kept
        self.assertEqual(memory.car(self.device)["models"], ["F30"])
        with mock.patch.object(memory.time, "time", return_value=time.time() + memory.QUIET + 1):
            memory.fresh_start(self.device)
        self.assertEqual(memory.car(self.device), {})

    def test_yes_takes_the_one_part_offered(self):
        shocks = make_product(name="مساعدين خلفي فيرنا", car_model="Verna", retail_price="950")
        make_inventory(shocks, self.branch, quantity=1)
        self.assertEqual(self.say("عندك مساعد؟"), "تقصد مساعدين خلفي فيرنا (Verna)؟ قول اسمها كامل أو رقمها.")
        self.assertIn("950", self.say("أيوه"))                  # كان «ملقتش القطعة دي»


class EgyptianFollowUpsTests(_Shop):
    """Other ways an Egyptian customer asks about the part just named."""

    def test_the_same_part_however_its_asked(self):
        self.say("عندك طرمبة مية F30؟")
        for said, expected in (("عاملة كام يا ريس؟", "2100"), ("لسه موجودة؟", "2100"),
                               ("صيني ولا ياباني؟", "ماركة BMW"), ("تمشي على F30؟", "بيركب على F30"),
                               ("فيه أوفر؟", talk.NO_CHEAPER), ("طب وريني", "مش متسجل مكانه")):
            with self.subTest(said):
                self.assertIn(expected, self.say(said))

    def test_a_part_by_its_shop_name(self):
        horn = make_product(name="كلاكس", car_model="E90", retail_price="350")
        make_inventory(horn, self.branch, quantity=1)
        self.assertIn("350", self.say("عندك زمارة لعربيتي E90؟"))


class SmallTalkTests(_Shop):

    def test_what_isnt_about_a_part(self):
        for said, reply in (
                ("السلام عليكم", "وعليكم السلام، تحت أمرك، محتاج قطعة إيه؟"),
                ("صباح الخير يا باشا", "صباح النور، تحت أمرك، محتاج قطعة إيه؟"),
                ("ازيك عامل ايه", "الحمد لله، تمام. تحت أمرك، محتاج قطعة إيه؟"),
                ("شكراً يا ريس", talk.THANKS_BACK),
                ("لا شكراً", talk.NO_PROBLEM),
                ("إنت مين؟", f"أنا موس، {talk.SHOP_ROBOT} {talk.CAN}"),
                ("ممكن تساعدني؟", talk.CAN),
                ("عايز أكلم حد", talk.ASK_STAFF_HERE),
                ("ثانية واحدة", talk.WAITING),
                ("هاخدها يا باشا", talk.TO_CASHIER),
                ("تمام", talk.AT_ORDER)):
            with self.subTest(said):
                self.assertEqual(self.say(said), reply)

    def test_say_it_again(self):
        self.assertEqual(self.say("قول تاني"), talk.NOTHING_YET)
        first = self.say("عندك طرمبة مية E90؟")
        self.assertEqual(self.say("معلش مسمعتش"), first)

    def test_a_greeting_then_the_question(self):
        self.assertEqual(self.say("السلام عليكم، عندك طرمبة مية E90؟"),
                         "وعليكم السلام، أيوه، طرمبة مية موجود، عندي منه حتتين، بسعر 1800 جنيه.")

    def test_a_question_with_thanks_in_it_is_still_a_question(self):
        self.assertIn("1800", self.say("شكراً، وعندك طرمبة مية E90؟"))


class ShopRepliesInEgyptianTests(_Shop):

    def test_a_written_reply_answers_its_topics_usual_questions(self):
        for said in ("بتقفلوا الساعه كام يا باشا؟", "فاتحين لحد امتى؟", "الجمعة فاتحين؟"):
            with self.subTest(said):
                self.assertEqual(self.say(said), "من 9 الصبح لحد 10 بالليل.")

    def test_a_topic_nobody_wrote_says_nothing_made_up(self):
        RobotReply.objects.filter(topic="hours").delete()
        self.assertNotIn("الصبح", self.say("بتقفلوا الساعه كام؟"))

    def test_every_suggested_topic_is_a_choice_and_fits_the_page(self):
        from robot import replies
        topics = dict(RobotReply.TOPIC)
        for topic, questions in replies.SUGGESTED:
            with self.subTest(topic):
                self.assertIn(topic, topics)
                self.assertLessEqual(len(questions), replies.MOST_QUESTIONS)


class OfflineSaysTheSameTests(_Shop):

    def test_the_conversation_word_for_word(self):
        offline = Knowledge(services.offline_catalog(self.branch))
        now = 0.0
        for question in ("عندكم قطع غيار بي ام؟", "طيب عندك طرمبة مية؟", "التانية", "وبكام يا باشا؟",
                         "طب والفلتر بتاعها؟", "عليه ضمان؟", "قول تاني", "شكراً", "مع السلامة",
                         "السلام عليكم، عندك طرمبة مية لمرسيدس؟", "إنت مين؟", "بتقفلوا الساعه كام؟",
                         "ازيك", "عندك طرمبة مية؟"):
            now += 5
            with self.subTest(question):
                server = self.say(question)
                self.assertEqual(offline.answer("يا موس " + question, now=now).text, server)

    def test_the_brain_has_the_clips_for_it(self):
        offline = Knowledge(services.offline_catalog(self.branch))
        clips = set(offline.clip_texts())
        for phrase in (talk.W_SALAM, talk.THANKS_BACK, talk.CAN, talk.BYE_BACK, "أنا موس،"):
            self.assertIn(phrase, clips)
        reply = offline.answer("يا موس السلام عليكم", now=1.0)
        self.assertTrue(set(reply.segments) <= clips, reply.segments)
