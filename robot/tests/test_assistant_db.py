"""robot/assistant.py — the Quran and music in the shop, the staff's own
questions, and the sale that doesn't end at «مش متوفر».

The Quran/music commands obey only a recognized face with the role for it
(`media`) and only a caller that can play sound (`caps=media`: «مخ موس» on
a computer); the staff's numbers are the dashboard's own (ReportingService);
out of stock offers the same part under another number on this shelf, or in
another branch. Runs on a tenant schema like the inventory suite.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from rest_framework.test import APIRequestFactory

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import (
    make_branch, make_customer, make_financial_transaction, make_inventory, make_product,
    make_sale_invoice, make_treasury,
)
from robot import assistant, services, views
from robot.brain.knowledge import Knowledge
from robot.models import RobotDevice


def _staff(role):
    return SimpleNamespace(name="كريم", user=SimpleNamespace(
        is_superuser=False, is_active=True, employee_profile=SimpleNamespace(role=role)))


class AssistantDbTestCase(ERPTenantTestCase):

    def setUp(self):
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="الرئيسي")
        self.device = RobotDevice(name="موس", device_uid="esp-assistant", branch=self.branch)
        self.token = self.device.issue_token()
        self.device.save()

    def say(self, text, employee=None, caps=frozenset({"media"})):
        with mock.patch.object(services, "ai_reply", return_value=""), \
                mock.patch.object(services, "shelf_location", return_value=""), \
                mock.patch.object(services, "maybe_raise_procurement_signal"):
            return views._handle_voice(text, self.device, employee, caps=caps)


class MediaTests(AssistantDbTestCase):

    def test_the_manager_starts_the_quran(self):
        intent, reply, payload = self.say("شغّل قرآن", _staff("manager"))
        self.assertEqual((intent, reply), ("command", "حاضر، هشغّل القرآن."))
        self.assertEqual(payload["media"], {"do": "play", "what": "quran"})

    def test_calm_music_stop_and_volume(self):
        manager = _staff("manager")
        self.assertEqual(self.say("شغلي موسيقى هادية", manager)[2]["media"], {"do": "play", "what": "calm"})
        self.assertEqual(self.say("وقّف الموسيقى", manager)[2]["media"], {"do": "stop", "what": ""})
        self.assertEqual(self.say("علّي الصوت شوية", manager)[2]["media"], {"do": "volume", "what": "up"})
        self.assertEqual(self.say("وطّي الصوت", manager)[2]["media"], {"do": "volume", "what": "down"})

    def test_the_owner_can_and_a_cashier_cant(self):
        self.assertIn("media", self.say("شغل قران", _staff("owner"))[2])
        _intent, reply, payload = self.say("شغل قران", _staff("cashier"))
        self.assertEqual(payload["action"], "denied")
        self.assertIn("تشغيل القرآن والموسيقى", reply)

    def test_nobody_recognized_is_asked_to_look_at_the_camera(self):
        _intent, reply, payload = self.say("شغل قران")
        self.assertEqual(reply, assistant.WHO_ARE_YOU)
        self.assertNotIn("media", payload)

    def test_the_esp32_cant_play_so_it_doesnt_promise(self):
        _intent, reply, payload = self.say("شغل قران", _staff("manager"), caps=frozenset())
        self.assertEqual(reply, assistant.NO_SPEAKERS)
        self.assertNotIn("media", payload)

    def test_a_muffler_isnt_a_volume_command(self):
        # «على» normalizes to «علي»: «بسأل على كاتم صوت» was «علّي الصوت».
        muffler = make_product(name="كاتم صوت", retail_price="1200")
        make_inventory(muffler, self.branch, quantity=2)
        intent, reply, _ = self.say("بسأل على كاتم صوت", _staff("manager"))
        self.assertEqual(intent, "inventory_query")
        self.assertIn("بسعر 1200 جنيه", reply)

    def test_a_part_question_isnt_a_media_command(self):
        oil = make_product(name="فلتر زيت", retail_price="450")
        make_inventory(oil, self.branch, quantity=2)
        intent, reply, _ = self.say("عندك فلتر زيت؟", _staff("manager"))
        self.assertEqual(intent, "inventory_query")
        self.assertIsNone(assistant.media_command("وقف العربية قدام المحل"))

    def test_the_brain_gets_the_command_beside_the_spoken_reply(self):
        from hr.models import Employee

        from inventory.tests.factories import make_employee
        user, _profile = make_employee(username="manager-media", role="manager")
        manager, _ = Employee.objects.get_or_create(user=user)
        request = APIRequestFactory().post(
            "/api/robot/v1/voice/",
            {"transcript": "يا موس شغّل قرآن", "reply_audio": "pcm", "caps": "media"},
            HTTP_X_ROBOT_TOKEN=self.token)
        with mock.patch.object(views, "_voice_employee", return_value=manager), \
                mock.patch.object(views.audio_svc, "synthesize_wav", return_value=b"RIFFWAVE"):
            resp = views.voice(request)
        self.assertEqual(resp["Content-Type"], "audio/wav")
        self.assertEqual(resp["X-Robot-Media"], "play:quran")


class StaffQuestionTests(AssistantDbTestCase):

    def test_todays_sales_are_the_dashboards(self):
        oil = make_product(name="فلتر زيت", retail_price="450")
        make_inventory(oil, self.branch, quantity=10)
        treasury = make_treasury(self.branch)
        make_sale_invoice(make_customer(), self.branch, treasury=treasury,
                          items=[(oil, 2, "450")], status="ready")
        make_financial_transaction(treasury, "200", txn_type="out", description="بنزين")
        _intent, reply, payload = self.say("بعنا كام النهارده؟", _staff("accountant"))
        self.assertEqual(reply, "النهارده اتباع 1 فاتورة بـ 900 جنيه، والمصروفات 200 جنيه.")
        self.assertEqual(payload["count"], 1)

    def test_no_sales_yet(self):
        self.assertEqual(self.say("مبيعات النهارده", _staff("manager"))[1], "لسه مفيش مبيعات النهارده.")

    def test_a_cashier_doesnt_hear_the_totals(self):
        _intent, reply, payload = self.say("بعنا كام النهارده؟", _staff("cashier"))
        self.assertEqual(payload["action"], "denied")
        self.assertNotIn("جنيه", reply)

    def test_whats_running_low(self):
        for name, qty in (("فلتر هوا", 0), ("بوجيهات", 1), ("زيت 5W30", 40)):
            make_inventory(make_product(name=name, retail_price="100"), self.branch, quantity=qty)
        _intent, reply, payload = self.say("إيه اللي قرّب يخلص؟", _staff("stock"))
        self.assertTrue(reply.startswith("فيه 2 صنف قرّب يخلص: فلتر هوا (0)، بوجيهات (1)"))
        self.assertNotIn("زيت", reply)
        self.assertEqual(payload["count"], 2)

    def test_nothing_low_and_who_may_ask(self):
        make_inventory(make_product(name="زيت", retail_price="100"), self.branch, quantity=40)
        self.assertEqual(self.say("النواقص", _staff("manager"))[1], "مفيش حاجة قرّبت تخلص.")
        self.assertEqual(self.say("النواقص")[1], assistant.WHO_ARE_YOU)


class OutOfStockTests(AssistantDbTestCase):

    def setUp(self):
        super().setUp()
        self.wanted = make_product(name="طرمبة بنزين أصلي", part_number="16117373493",
                                   retail_price="3200")
        self.wanted.oem_cross_reference = ["0580464999"]
        self.wanted.save()
        make_inventory(self.wanted, self.branch, quantity=0)

    def test_an_equivalent_on_this_shelf(self):
        bosch = make_product(name="طرمبة بنزين بوش", part_number="0580464999", retail_price="2400")
        make_inventory(bosch, self.branch, quantity=3)
        _intent, reply, payload = self.say("عندك طرمبة بنزين أصلي؟")
        self.assertEqual(reply, "طرمبة بنزين أصلي مش متوفر حالياً في الفرع، وسعره 3200 جنيه. "
                                "بس عندي بديله طرمبة بنزين بوش متوفر بسعر 2400 جنيه.")
        self.assertEqual(payload["product"]["alternative"]["part_number"], "0580464999")
        self.assertNotIn(str(bosch.b2b_wholesale_price or "x"), reply)

    def test_in_another_branch(self):
        other = make_branch(name="المعادي")
        make_inventory(self.wanted, other, quantity=2)
        reply = self.say("عندك طرمبة بنزين أصلي؟")[1]
        self.assertTrue(reply.endswith("بس موجود في فرع المعادي."))

    def test_an_equivalent_out_of_stock_too_isnt_offered(self):
        bosch = make_product(name="طرمبة بنزين بوش", part_number="0580464999", retail_price="2400")
        make_inventory(bosch, self.branch, quantity=0)
        reply = self.say("عندك طرمبة بنزين أصلي؟")[1]
        self.assertNotIn("بديله", reply)

    def test_offline_offers_the_same_equivalent(self):
        bosch = make_product(name="طرمبة بنزين بوش", part_number="0580464999", retail_price="2400")
        make_inventory(bosch, self.branch, quantity=3)
        offline = Knowledge(services.offline_catalog(self.branch))
        question = "عندك طرمبة بنزين أصلي؟"
        self.assertEqual(offline.answer("يا موس " + question, now=0.0).text, self.say(question)[1])

    def test_an_alternative_whose_number_has_letters(self):
        # part_number__in with lowercased codes missed «LR…» on Postgres.
        coil = make_product(name="كويل إشعال", part_number="MAIN-COIL", retail_price="900")
        coil.oem_cross_reference = ["LR012345"]
        coil.save()
        make_inventory(coil, self.branch, quantity=0)
        bosch = make_product(name="كويل بوش", part_number="LR012345", retail_price="700")
        make_inventory(bosch, self.branch, quantity=2)
        reply = self.say("عندك كويل إشعال؟")[1]
        self.assertIn("بس عندي بديله كويل بوش متوفر بسعر 700 جنيه", reply)
        offline = Knowledge(services.offline_catalog(self.branch))
        self.assertEqual(offline.answer("يا موس عندك كويل إشعال؟", now=0.0).text, reply)

    def test_loose_substring_hits_dont_crowd_out_the_real_one(self):
        for i in range(55):                              # «X0580464999Y»: contains the code, isn't it
            decoy = make_product(name=f"قطعة {i}", part_number=f"DECOY-{i}", retail_price="10")
            decoy.oem_cross_reference = [f"X0580464999Y{i}"]
            decoy.save()
            make_inventory(decoy, self.branch, quantity=100)
        bosch = make_product(name="طرمبة بنزين بوش", part_number="0580464999", retail_price="2400")
        make_inventory(bosch, self.branch, quantity=1)
        self.assertIn("بديله طرمبة بنزين بوش", self.say("عندك طرمبة بنزين أصلي؟")[1])

    def test_the_price_is_retail(self):
        bosch = make_product(name="طرمبة بنزين بوش", part_number="0580464999", retail_price="2400",
                             b2b_wholesale_price=Decimal("1700"))
        make_inventory(bosch, self.branch, quantity=3)
        reply = self.say("عندك طرمبة بنزين أصلي؟")[1]
        self.assertIn("2400", reply)
        self.assertNotIn("1700", reply)
