"""«علّم الروبوت»: on the robot's page, a question it couldn't answer gets
any reply the owner writes — not only a part's number — and a reply taught
is edited there. It's a «🗨️ الردود الجاهزة» reply: said by voice next time,
and by «مخ موس» with the net down."""

from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import services, views
from robot.models import RobotDevice, RobotKnowledge, RobotReply, RobotVoiceInteraction

FINE = "الحمد لله تمام، اتفضل أخدمك في إيه؟"


@override_settings(ALLOWED_HOSTS=["*"])
class TeachAnyReplyTests(ERPTenantTestCase):

    def setUp(self):
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-teach-replies", branch=self.branch)
        self.device.issue_token()
        self.device.save()
        for said in ("يا موس ازيك", "تعرف تسجل يا موس؟"):
            RobotVoiceInteraction.objects.create(device=self.device, transcript=said, intent="unknown",
                                                 reply_text="مش فاهم قصدك.", payload={"unresolved": True})
        boss = User.objects.create_user("teach_boss", password="x", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(boss)
        self.host = self.domain.domain

    def page(self):
        return self.client.get(reverse("robot_ui:dashboard"), HTTP_HOST=self.host)

    def teach(self, **data):
        return self.client.post(reverse("robot_ui:teach"), data, HTTP_HOST=self.host)

    def say(self, text):
        with mock.patch.object(services, "ai_reply", return_value="رد من الذكاء الاصطناعي"), \
                mock.patch.object(services, "shelf_location", return_value=""), \
                mock.patch.object(services, "maybe_raise_procurement_signal"):
            intent, reply, _payload = views._handle_voice(text, self.device, None)
        return intent, reply

    def test_any_reply_written_is_said_next_time(self):
        page = self.page()
        self.assertContains(page, 'value="ازيك"')                       # the question, without «يا موس»
        self.assertContains(page, 'value="تعرف تسجل"')
        self.assertContains(page, "كان رد:")                             # what it said, to be put right

        self.teach(phrase="يا موس ازيك", answer=f"  {FINE} ")
        reply = RobotReply.objects.get()
        self.assertEqual((reply.topic, reply.questions, reply.answer, reply.is_active, reply.branch),
                         ("other", "ازيك", FINE, True, None))
        self.assertContains(self.page(), "اتحفظ: لما حد يقول «ازيك» هيرد")

        self.assertEqual(self.say("ازيك"), ("shop_reply", FINE))      # what the robot hears once called
        self.assertIn({"topic": "other", "questions": ["ازيك"], "answer": FINE},
                      services.offline_catalog(self.branch)["replies"])   # and «مخ موس» offline

    def test_a_reply_taught_is_edited_on_the_same_row(self):
        self.teach(phrase="ازيك", answer=FINE)
        reply = RobotReply.objects.get()
        page = self.page()
        self.assertContains(page, f'name="reply_id" value="{reply.pk}"')
        self.assertContains(page, f">{FINE}</textarea>")                # its answer, ready to change

        self.teach(reply_id=reply.pk, phrase="ازيك", answer="تمام والحمد لله.")
        self.teach(reply_id=reply.pk, phrase="عامل ايه", answer="تمام والحمد لله.")
        reply.refresh_from_db()
        self.assertEqual(RobotReply.objects.count(), 1)
        self.assertEqual((reply.questions, reply.answer), ("ازيك\nعامل ايه", "تمام والحمد لله."))
        self.assertEqual(self.say("عامل ايه"), ("shop_reply", "تمام والحمد لله."))
        # The same question written again in the form on top: that reply changes, no second one.
        self.teach(phrase="يا موس، ازيك؟", answer="كله تمام.")
        self.assertEqual(RobotReply.objects.count(), 1)
        self.assertEqual(self.say("ازيك"), ("shop_reply", "كله تمام."))

    def test_a_word_for_a_part_is_still_taught_with_its_number(self):
        pump = make_product(name="طرمبة مية", part_number="11517586925", retail_price="1800")
        make_inventory(pump, self.branch, quantity=1)
        self.teach(phrase="الطرمبة الكهربا", part_number="11517586925")
        self.assertTrue(RobotKnowledge.objects.filter(product=pump).exists())
        self.assertFalse(RobotReply.objects.exists())

    def test_nothing_to_teach_or_too_long_says_why(self):
        self.teach(phrase="ازيك")
        self.assertContains(self.page(), "اكتب الرد اللي الروبوت هيقوله")
        self.teach(phrase="ازيك", answer="ا" * 501)
        self.assertContains(self.page(), "الرد طويل أوي")
        self.teach(phrase="يا موس", answer=FINE)                          # only its name: nothing asked
        self.assertContains(self.page(), "اكتب الناس بتقول إيه")
        self.assertFalse(RobotReply.objects.exists())
