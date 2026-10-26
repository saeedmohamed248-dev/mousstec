"""The shop's log of 26/10 on the server: the same questions as
robot/tests/test_shop_log_fixes.py, through /voice/'s own handling, and a
robot for every branch added and set up from the dashboard."""

import io
import zipfile
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import audio, customers, memory, services, views
from robot.brain.knowledge import Knowledge
from robot.models import RobotDevice, RobotReply

HOURS = "بنفتح كل يوم من 10 الصبح لحد 11 بالليل."


class ShopLogTests(ERPTenantTestCase):

    def setUp(self):
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="الفرع الرئيسي")
        self.device = RobotDevice(name="موس", device_uid="esp-log", branch=self.branch)
        self.device.issue_token()
        self.device.save()
        for number, name, price in (("OF-MINI", "فلتر زيت MINI - JP", "350"), ("OF-E90", "فلتر زيت - E90 - JP", "300"),
                                    ("OF-BOSCH", "فلتر زيت - BOSCH - E90", "420"),
                                    ("SENS-1", "حساس ساعة الزيت - F30 - BST", "1500")):
            make_inventory(make_product(name=name, part_number=number, retail_price=price, car_model=""),
                           self.branch, quantity=2)
        RobotReply.objects.create(topic="hours", questions="مواعيدكم\nمواعيد الشغل", answer=HOURS)
        RobotReply.objects.create(topic="phone", questions="رقمكم", answer="رقم الفرع 01125157767.")

    def say(self, text):
        with mock.patch.object(services, "ai_reply", return_value="رد من الذكاء الاصطناعي"), \
                mock.patch.object(services, "shelf_location", return_value=""), \
                mock.patch.object(services, "maybe_raise_procurement_signal"):
            return views._handle_voice(text, self.device, None)[:2]

    def test_the_hours_not_the_oil_clock_sensor(self):
        self.assertEqual(self.say("مواعيد الشغل بتاعتكو الساعة كام؟"), ("shop_reply", HOURS))

    def test_the_parts_number_is_not_the_branchs_phone(self):
        intent, reply = self.say("مش الرقم بتاعها")
        self.assertNotEqual(intent, "shop_reply")
        self.assertNotIn("0 1 1 2 5", reply)

    def test_mini_and_bosch_named_in_arabic(self):
        intent, reply = self.say("عندك فلتر زيت ميني كوبر؟")
        self.assertEqual(intent, "inventory_query")
        self.assertIn("فلتر زيت MINI - JP", reply)
        self.assertIn("350 جنيه", reply)
        self.assertIn("420 جنيه", self.say("فلتر الزيت بوش")[1])

    def test_the_time_and_a_wrong_guess(self):
        intent, reply = self.say("الساعة كام؟")
        self.assertEqual(intent, "chat")
        self.assertTrue(reply.startswith("الساعة "), reply)
        self.assertNotIn("حساس", reply)
        self.assertEqual(self.say("خطأ")[0], "chat")

    def test_just_asked_or_just_the_car(self):
        # «عندك» alone (Vosk lost the rest) went to the AI, which introduced itself as the shop.
        self.assertEqual(self.say("عندك"), ("chat", "تحت أمرك، محتاج قطعة إيه؟"))
        # «عندك فلتر زيت؟» → «اسم العربية؟» → «عربية إي. نينتي.»: the E90's filters, not the AI.
        memory.note_turn(self.device, "عندك فلتر زيت؟", "اه موجود، ممكن تقولي اسم العربية؟")
        intent, reply = self.say("عربية إي. نينتي.")
        self.assertEqual(intent, "inventory_query")
        self.assertIn("E90 - JP", reply)
        self.assertNotIn("MINI", reply)
        memory.forget(self.device)
        self.assertEqual(self.say("عربيتي اف تلاتين"), ("chat", "تمام، F30. محتاج قطعة إيه ليها؟"))

    def test_when_the_ai_cant_answer(self):
        with mock.patch.object(services, "ai_reply", return_value=""), \
                mock.patch.object(services, "shelf_location", return_value=""):
            said = views._handle_voice("بكلمه انجليزي على العربي", self.device, None)[:2]
            part = views._handle_voice("عندك كمبروسر تكييف", self.device, None)[:2]
        self.assertEqual(said, ("unknown", views.NOT_UNDERSTOOD))           # not «ملقتش القطعة»
        self.assertEqual(part[1], "ملقتش القطعة دي في المخزون. ممكن تقولي رقمها؟")

    def test_offline_the_same_words(self):
        offline = Knowledge(services.offline_catalog(self.branch))
        for question in ("مواعيد الشغل بتاعتكو الساعة كام؟", "عندك فلتر زيت ميني كوبر؟", "فلتر الزيت بوش"):
            with self.subTest(question):
                offline._start_over()
                self.assertEqual(offline.answer("يا موس " + question, now=0.0).text, self.say(question)[1])


class ShopNameTests(ERPTenantTestCase):
    """«منورنا في Mouss Tec» to Fixit's customers: the shop's own name now."""

    def test_the_accounts_name(self):
        self.assertEqual(services.shop_name(), "Test Company")
        self.assertEqual(services.welcome(), "أهلاً بيك في Test Company")
        with mock.patch.object(audio, "gemini_generate", return_value="تمام.") as gen:
            services.ai_reply("عندك منتجات إيه")
        prompt = gen.call_args.args[0][0]["text"]
        self.assertIn("في محل «Test Company» لقطع غيار", prompt)
        self.assertNotIn("Mouss Tec", prompt)
        self.assertEqual(services.offline_catalog(Branch.objects.order_by("id").first()
                                                  or make_branch(name="المعادي"))["shop"], "Test Company")

    def test_a_new_customer_is_welcomed_to_the_shop(self):
        customer = mock.Mock(vip_tier="")
        customer.name = "أحمد"
        with mock.patch.object(customers, "last_purchase", return_value=(None, None)):
            greeting = customers.customer_greeting(customer, visit_count=1, notes={})["greeting"]
        self.assertIn("نورت Test Company", greeting)
        self.assertNotIn("Mouss Tec", greeting)


@override_settings(ALLOWED_HOSTS=["*"])
class RobotInEveryBranchTests(ERPTenantTestCase):

    def setUp(self):
        self.main = Branch.objects.order_by("id").first() or make_branch(name="الفرع الرئيسي")
        self.other = make_branch(name="فرع المعادي")
        boss = User.objects.create_user("robots_boss", password="x", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(boss)
        self.host = self.domain.domain

    def test_added_for_a_branch_and_its_brain_downloaded_once_with_its_key(self):
        page = self.client.get(reverse("robot_ui:dashboard"), HTTP_HOST=self.host)
        self.assertContains(page, "روبوت لفرع")
        resp = self.client.post(reverse("robot_ui:new_device"), {"branch": self.other.pk, "name": ""},
                                HTTP_HOST=self.host)
        device = RobotDevice.objects.get(branch=self.other)
        self.assertRedirects(resp, reverse("robot_ui:device_profile", args=[device.pk]), fetch_redirect_response=False)
        self.assertEqual(device.name, "روبوت فرع المعادي")
        profile = self.client.get(reverse("robot_ui:device_profile", args=[device.pk]), HTTP_HOST=self.host)
        self.assertContains(profile, "?key=1")
        url = reverse("robot_ui:brain_zip", args=[device.pk])
        first = self.client.get(url + "?key=1", HTTP_HOST=self.host)
        self.assertEqual(first["Content-Type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(first.content)) as zf:
            ini = zf.read("brain.ini").decode("utf-8")
        token = next(line.split("=", 1)[1].strip() for line in ini.splitlines() if line.startswith("token"))
        self.assertEqual(RobotDevice.hash_token(token), device.api_token)    # the key that works, for this robot
        self.assertIn(f"api_base = https://{self.host}/api/robot/v1", ini)
        again = self.client.get(url + "?key=1", HTTP_HOST=self.host)        # once only
        self.assertNotIn("brain.ini", zipfile.ZipFile(io.BytesIO(again.content)).namelist())
        profile = self.client.get(reverse("robot_ui:device_profile", args=[device.pk]), HTTP_HOST=self.host)
        self.assertNotContains(profile, "?key=1")

    def test_the_update_on_every_card_and_a_mistake_deleted(self):
        # «فين التحديث؟»: on the dashboard's card now, not only the robot's page; and a robot
        # registered twice by mistake (never connected) can go — one that connected can't.
        used = RobotDevice(name="موس", device_uid="esp-used", branch=self.main)
        used.issue_token()
        used.save()
        RobotDevice.objects.filter(pk=used.pk).update(last_seen_at=timezone.now())
        mistake = RobotDevice(name="روبوت زيادة", device_uid="esp-extra", branch=self.main)
        mistake.issue_token()
        mistake.save()
        page = self.client.get(reverse("robot_ui:dashboard"), HTTP_HOST=self.host)
        self.assertContains(page, reverse("robot_ui:brain_zip", args=[used.pk]))
        self.assertContains(page, reverse("robot_ui:delete_device", args=[mistake.pk]))
        self.assertNotContains(page, reverse("robot_ui:delete_device", args=[used.pk]))
        for device in (used, mistake):
            self.client.post(reverse("robot_ui:delete_device", args=[device.pk]), HTTP_HOST=self.host)
        self.assertEqual(list(RobotDevice.objects.values_list("device_uid", flat=True)), ["esp-used"])

    def test_a_rotated_key_comes_with_a_new_zip(self):
        device = RobotDevice(name="موس", device_uid="esp-rot", branch=self.main)
        device.issue_token()
        device.save()
        self.client.post(reverse("robot_ui:device_profile", args=[device.pk]),
                         {"name": "موس", "rotate_token": "on", "is_active": "on", "wake_name": "موس"},
                         HTTP_HOST=self.host)
        resp = self.client.get(reverse("robot_ui:brain_zip", args=[device.pk]) + "?key=1", HTTP_HOST=self.host)
        self.assertIn("brain.ini", zipfile.ZipFile(io.BytesIO(resp.content)).namelist())

    def test_the_branchs_phone_said_without_branch_twice(self):
        self.main.name, self.main.phone = "الفرع الرئيسي", "01125157767"
        self.main.save()
        page = self.client.get(reverse("robot_ui:replies"), HTTP_HOST=self.host)
        self.assertContains(page, "رقم الفرع الرئيسي: 01125157767.")
        self.assertNotContains(page, "فرع الفرع")
