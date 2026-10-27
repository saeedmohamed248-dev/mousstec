"""«🗨️ الردود الجاهزة» end to end: written on the robot's page, said by voice
before the parts and the AI, and by «مخ موس» offline in the same words."""

from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse
from rest_framework.test import APIRequestFactory

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import services, views
from robot.brain.knowledge import Knowledge
from robot.models import RobotDevice, RobotReply

HOURS = "بنفتح كل يوم من 10 الصبح لحد 11 بالليل."


class ShopRepliesTests(ERPTenantTestCase):

    def setUp(self):
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-replies", branch=self.branch)
        self.token = self.device.issue_token()
        self.device.save()
        oil = make_product(name="فلتر زيت", part_number="11427953125", retail_price="450")
        make_inventory(oil, self.branch, quantity=3)
        pump = make_product(name="طرمبة مية", part_number="11517586925", retail_price="1800")
        make_inventory(pump, self.branch, quantity=1)
        self.hours = RobotReply.objects.create(topic="hours", questions="مواعيدكم\nبتفتحوا امتى", answer=HOURS)
        RobotReply.objects.create(topic="warranty", questions="فيه ضمان", answer="كل الجديد عليه ضمان 3 شهور.")

    def say(self, text):
        with mock.patch.object(services, "ai_reply", return_value="رد من الذكاء الاصطناعي") as ai, \
                mock.patch.object(services, "shelf_location", return_value=""), \
                mock.patch.object(services, "maybe_raise_procurement_signal"):
            intent, reply, _payload = views._handle_voice(text, self.device, None)
        return intent, reply, ai

    def test_said_by_voice_not_left_to_the_ai(self):
        intent, reply, ai = self.say("انتو بتفتحوا امتى؟")
        self.assertEqual((intent, reply), ("shop_reply", HOURS))
        ai.assert_not_called()
        self.assertEqual(RobotReply.objects.get(pk=self.hours.pk).hit_count, 1)

    def test_a_part_is_still_a_part(self):
        self.assertEqual(self.say("عندك فلتر زيت؟")[0], "inventory_query")
        intent, reply, _ = self.say("فيه ضمان على الطرمبة؟")         # the pump, not the shop's warranty
        self.assertNotEqual(intent, "shop_reply")
        self.assertIn("طرمبة مية", reply)

    def test_off_or_another_branchs_reply_is_not_said(self):
        self.hours.is_active = False
        self.hours.save()
        self.assertNotEqual(self.say("مواعيدكم ايه")[0], "shop_reply")
        other = make_branch(name="فرع تاني") if Branch.objects.count() < 2 else Branch.objects.exclude(pk=self.branch.pk).first()
        RobotReply.objects.create(topic="address", questions="العنوان فين", answer="في فرع تاني.", branch=other)
        self.assertNotEqual(self.say("العنوان فين")[1], "في فرع تاني.")
        RobotReply.objects.create(topic="address", questions="العنوان فين", answer="في الشارع الرئيسي.")
        RobotReply.objects.create(topic="address", questions="العنوان فين", answer="فرعنا هنا.", branch=self.branch)
        self.assertEqual(self.say("العنوان فين")[1], "فرعنا هنا.")              # the branch's own first

    def test_a_phone_number_is_said_digit_by_digit(self):
        RobotReply.objects.create(topic="phone", questions="رقم التليفون", answer="رقمنا 01012345678 وعليه واتساب.")
        self.assertEqual(self.say("رقم التليفون ايه")[1], "رقمنا 0 1 0 1 2 3 4 5 6 7 8 وعليه واتساب.")

    def test_offline_in_the_servers_words(self):
        catalog = services.offline_catalog(self.branch)
        offline = Knowledge(catalog)
        for question in ("مواعيدكم ايه؟", "فيه ضمان؟", "فيه ضمان على الطرمبة؟", "عندك فلتر زيت؟"):
            with self.subTest(question):
                said = offline.answer("يا موس " + question, now=0.0)
                self.assertEqual(said.text, self.say(question)[1])
        self.assertIn(HOURS, offline.clip_texts())                     # its voice is kept for the net down
        self.assertEqual(offline.answer("يا موس مواعيدكم", now=0.0).intent, "shop_reply")

    def test_a_new_reply_reaches_the_robot(self):
        pull = lambda: views.sync_pull(APIRequestFactory().post(  # noqa: E731
            "/api/robot/v1/sync/pull/", HTTP_X_ROBOT_TOKEN=self.token))
        before = pull().headers["ETag"]
        RobotReply.objects.create(topic="delivery", questions="فيه توصيل", answer="أيوه، جوه القاهرة.")
        after = pull()
        self.assertNotEqual(after.headers["ETag"], before)
        self.assertIn("أيوه، جوه القاهرة.", [r["answer"] for r in after.data["replies"]])


    def test_what_the_robot_says_is_the_answer_not_its_topic(self):
        # 🐛 In the shop the robot said «hours» and «address»: the payload's own "reply"
        # (the topic) was written over the answer in /voice/'s response.
        request = APIRequestFactory().post("/api/robot/v1/voice/", {"transcript": "يا موس مواعيدكم"},
                                           format="json", HTTP_X_ROBOT_TOKEN=self.token)
        with mock.patch.object(services, "ai_reply", return_value=""):
            data = views.voice(request).data
        self.assertEqual((data["intent"], data["reply"], data["reply_topic"]), ("shop_reply", HOURS, "hours"))


@override_settings(ALLOWED_HOSTS=["*"])
class RepliesPageTests(ERPTenantTestCase):

    def setUp(self):
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.branch.location, self.branch.phone = "شارع 9، المعادي", "01012345678"
        self.branch.save()
        boss = User.objects.create_user("replies_boss", password="x", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(boss)
        self.host = self.domain.domain
        self.url = reverse("robot_ui:replies")

    def test_written_saved_and_removed_on_the_page(self):
        page = self.client.get(self.url, HTTP_HOST=self.host)
        self.assertContains(page, "بتفتحوا امتى")                          # the questions are suggested
        named = self.branch.name if self.branch.name.startswith(("فرع", "الفرع")) else f"فرع {self.branch.name}"
        self.assertContains(page, "عنوان %s: شارع 9، المعادي." % named)
        self.assertFalse(RobotReply.objects.exists())                      # a suggestion is never said
        empty = self.client.post(self.url, {"topic": "hours", "questions": "مواعيدكم", "answer": " "},
                                 HTTP_HOST=self.host)
        self.assertContains(empty, "اكتب الرد اللي الروبوت هيقوله.")
        self.client.post(self.url, {"topic": "hours", "questions": "مواعيدكم\n\nبتفتحوا امتى ",
                                    "answer": HOURS, "is_active": "1"}, HTTP_HOST=self.host)
        reply = RobotReply.objects.get()
        self.assertEqual((reply.topic, reply.questions, reply.answer, reply.is_active, reply.branch),
                         ("hours", "مواعيدكم\nبتفتحوا امتى", HOURS, True, None))
        page = self.client.get(self.url, HTTP_HOST=self.host)
        self.assertNotContains(page, "مواعيد الشغل")                       # its suggestion is gone
        self.client.post(self.url, {"reply_id": reply.pk, "remove": "1"}, HTTP_HOST=self.host)
        self.assertFalse(RobotReply.objects.exists())
