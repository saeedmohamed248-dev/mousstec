"""«📱 كلّم موس» (robot/phone.py): the owner talks to the robot from a phone's browser — the
same answers as in the shop, and tasks by their role without a PIN (they're signed in)."""

from unittest import mock

from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse

from inventory.models import Branch, Inventory
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_employee, make_inventory, make_product
from robot import phone, services
from robot.models import RobotAlert, RobotDevice


@override_settings(ALLOWED_HOSTS=["*"])
class PhoneTalkTests(ERPTenantTestCase):

    def setUp(self):
        from hr.models import Employee

        cache.clear()
        self.addCleanup(cache.clear)
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        robot = RobotDevice(name="موس", device_uid="esp-shop", branch=self.branch)
        robot.issue_token()
        robot.save()
        self.oil = make_product(name="فلتر زيت", part_number="11427953125", retail_price="450")
        make_inventory(self.oil, self.branch, quantity=5)
        self.user, _profile = make_employee(username="boss", role="manager", branch=self.branch, first_name="سعيد")
        Employee.objects.get_or_create(user=self.user)
        self.client = Client()
        self.client.force_login(self.user)
        self.host = self.domain.domain
        for name in ("ai_reply", "shelf_location"):
            patcher = mock.patch.object(services, name, return_value="")
            patcher.start()
            self.addCleanup(patcher.stop)

    def say(self, text):
        resp = self.client.post(reverse("robot_ui:talk_ask"), {"text": text}, HTTP_HOST=self.host)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_the_page_opens_for_staff_only(self):
        page = self.client.get(reverse("robot_ui:talk"), HTTP_HOST=self.host)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, reverse("robot_ui:talk_ask"))
        self.assertContains(page, "كلّم موس")
        stranger = Client()
        self.assertEqual(stranger.get(reverse("robot_ui:talk"), HTTP_HOST=self.host).status_code, 302)
        from django.contrib.auth.models import User
        nobody = User.objects.create_user("not_staff", password="x")
        stranger.force_login(nobody)
        self.assertEqual(stranger.get(reverse("robot_ui:talk"), HTTP_HOST=self.host).status_code, 403)
        self.assertEqual(stranger.post(reverse("robot_ui:talk_ask"), {"text": "x"}, HTTP_HOST=self.host)
                         .status_code, 403)

    def test_a_question_answered_as_in_the_shop_no_name_needed(self):
        body = self.say("عندك فلتر زيت")
        self.assertIn("فلتر زيت", body["reply"])
        phone_robot = RobotDevice.objects.get(device_uid=f"phone-{self.user.pk}")
        self.assertEqual(phone_robot.branch, self.branch)
        dashboard = self.client.get(reverse("robot_ui:dashboard"), HTTP_HOST=self.host)
        self.assertNotIn(phone_robot, list(dashboard.context["devices"]))   # never listed with the robots

    def test_a_task_by_role_without_a_pin(self):
        body = self.say("ضيف 3 فلتر زيت للمخزون")
        self.assertIn("عندك منه 5", body["reply"])                         # no «قولي الرقم السري»
        for said in ("أيوه", "لا"):
            self.say(said)
        body = self.say("هنا")
        self.assertEqual(body["action"], "entry_done", body["reply"])
        self.assertEqual(Inventory.objects.get(product=self.oil, branch=self.branch).quantity, 8)

    def test_the_staff_are_never_called_from_a_phone(self):
        with mock.patch.object(services, "ai_reply", return_value="لحظة واحدة هسأل لك حد من زمايلي الموظفين."):
            body = self.say("بتعرف تركب الحاجات دي؟")
        self.assertNotIn(services.STAFF_CALL, body["reply"])
        self.assertFalse(RobotAlert.objects.exists())

    def test_a_recording_is_heard_on_the_server(self):
        with mock.patch.object(phone, "hear", return_value="عندك فلتر زيت") as heard:
            resp = self.client.post(reverse("robot_ui:talk_ask"),
                                    {"audio": _upload(b"webm-bytes")}, HTTP_HOST=self.host)
        body = resp.json()
        self.assertEqual(heard.call_args.args[1], b"webm-bytes")
        self.assertEqual(body["heard"], "عندك فلتر زيت")
        self.assertIn("فلتر زيت", body["reply"])
        with mock.patch.object(phone, "hear", return_value=""):
            body = self.client.post(reverse("robot_ui:talk_ask"), {"audio": _upload(b"x")},
                                    HTTP_HOST=self.host).json()
        self.assertIn("مسمعتكش", body["reply"])

    def test_its_voice_and_a_limit_a_minute(self):
        with mock.patch("robot.audio.synthesize_wav", return_value=b"RIFF-voice"):
            resp = self.client.post(reverse("robot_ui:talk_say"), {"text": "أيوه"}, HTTP_HOST=self.host)
        self.assertEqual((resp.status_code, resp["Content-Type"], resp.content), (200, "audio/wav", b"RIFF-voice"))
        with mock.patch.object(phone, "TURNS_A_MINUTE", 1):
            self.say("عندك فلتر زيت")
            resp = self.client.post(reverse("robot_ui:talk_ask"), {"text": "تاني"}, HTTP_HOST=self.host)
        self.assertEqual(resp.status_code, 429)


def _upload(data):
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile("voice", data, content_type="audio/webm")
