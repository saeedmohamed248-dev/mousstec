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
        self.assertEqual(reply, "طرمبة بنزين أصلي مش موجود هنا دلوقتي، وسعره 3200 جنيه. "
                                "بس عندي بديله طرمبة بنزين بوش موجود بسعر 2400 جنيه.")
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
        self.assertIn("بس عندي بديله كويل بوش موجود بسعر 700 جنيه", reply)
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


class MorningBriefingTests(AssistantDbTestCase):
    """The first face of the day: a manager hears yesterday and what's
    waiting — each line only for a role allowed to hear it."""

    def _yesterday_sale(self, amount="450", qty=2):
        from datetime import timedelta

        from django.utils import timezone

        from inventory.models import SaleInvoice
        oil = make_product(name="فلتر زيت", retail_price=amount)
        make_inventory(oil, self.branch, quantity=10)
        invoice = make_sale_invoice(make_customer(), self.branch, treasury=make_treasury(self.branch),
                                    items=[(oil, qty, amount)], status="ready")
        SaleInvoice.objects.filter(pk=invoice.pk).update(date_created=timezone.now() - timedelta(days=1))

    def test_a_manager_hears_yesterday_and_whats_waiting(self):
        from robot.models import ProcurementSignal, RobotVoiceInteraction
        self._yesterday_sale()
        empty = make_product(name="بوجيهات", retail_price="100")
        make_inventory(empty, self.branch, quantity=0)
        ProcurementSignal.objects.create(device=self.device, product=empty, branch=self.branch, status="open")
        RobotVoiceInteraction.objects.create(device=self.device, transcript="عندك كوتش؟", intent="unknown",
                                             reply_text="", payload={"unresolved": True})
        said = assistant.morning_briefing(_staff("manager"), self.device)
        self.assertEqual(said, "إمبارح اتباع 1 فاتورة بـ 900 جنيه. فيه 1 صنف قرّب يخلص. "
                               "و1 صنف العملاء طلبوه وهو خلصان. "
                               "وفيه 1 سؤال معرفتش أرد عليه، علّمهولي من صفحة الروبوت.")

    def test_todays_sales_arent_yesterdays(self):
        oil = make_product(name="فلتر زيت", retail_price="450")
        make_inventory(oil, self.branch, quantity=10)
        make_sale_invoice(make_customer(), self.branch, treasury=make_treasury(self.branch),
                          items=[(oil, 1, "450")], status="ready")
        self.assertTrue(assistant.morning_briefing(_staff("accountant"), self.device)
                        .startswith("إمبارح مكانش فيه مبيعات."))

    def test_each_role_hears_its_own(self):
        self._yesterday_sale()
        make_inventory(make_product(name="بوجيهات", retail_price="100"), self.branch, quantity=0)
        cashier = assistant.morning_briefing(_staff("cashier"), self.device)
        self.assertNotIn("جنيه", cashier)
        stock = assistant.morning_briefing(_staff("stock"), self.device)
        self.assertNotIn("جنيه", stock)                          # no sales figures for the store room
        self.assertIn("قرّب يخلص", stock)

    def test_said_after_the_clock_in(self):
        from robot import security
        from robot.models import RobotCommand
        self._yesterday_sale()
        manager = _staff("manager")
        request = APIRequestFactory().post("/api/robot/v1/face/", {"face_embedding": [0.1] * 8}, format="json",
                                           HTTP_X_ROBOT_TOKEN=self.token)
        with mock.patch.object(views.security, "matching_available", return_value=True), \
                mock.patch.object(views.security, "identify_employee", return_value=(manager, 0.97)), \
                mock.patch.object(views.security, "register_attendance", return_value=("clock_in", None)), \
                mock.patch.object(views, "RobotAccessLog"), \
                mock.patch.object(security.RobotDeviceThrottle, "allow_request", return_value=True):
            manager.id = 1
            views.face(request)
        text = RobotCommand.objects.filter(device=self.device, kind="say").latest("id").payload["text"]
        self.assertTrue(text.startswith("أهلاً يا كريم، سجّلت حضورك. إمبارح اتباع 1 فاتورة بـ 900 جنيه."))


class WebSearchTests(AssistantDbTestCase):
    """A manager asks what the ERP doesn't know: the robot looks it up on the
    internet (Gemini + Google Search) and says where it's from. Nobody else
    gets the internet: customers get the shop's answers, as before."""

    FOUND = ("الـ N52 بياخد حوالي 6.5 لتر زيت.", [{"title": "bmw.com", "uri": "https://x/1"}])

    def ask(self, text, employee=None, web=FOUND):
        with mock.patch.object(services, "web_answer", return_value=web) as looked, \
                mock.patch.object(services, "ai_reply", return_value="") as llm, \
                mock.patch.object(services, "maybe_raise_procurement_signal"):
            intent, reply, payload = views._handle_voice(text, self.device, employee)
        return reply, payload, looked, llm

    def test_a_manager_asks_the_internet(self):
        reply, payload, looked, _ = self.ask("دوّر على النت الـ N52 بياخد كام لتر زيت", _staff("manager"))
        self.assertEqual(reply, "الـ N52 بياخد حوالي 6.5 لتر زيت. (المصدر: bmw.com)")
        self.assertTrue(payload["web"])
        self.assertEqual(payload["sources"][0]["title"], "bmw.com")
        looked.assert_called_once()

    def test_what_nothing_answered_is_looked_up_for_a_manager(self):
        reply, payload, looked, llm = self.ask("إيه الفرق بين الزيت الصناعي والمعدني؟", _staff("supervisor"))
        self.assertTrue(payload["web"])
        looked.assert_called_once()
        llm.assert_not_called()

    def test_customers_and_the_cashier_dont_get_the_internet(self):
        reply, payload, looked, llm = self.ask("إيه الفرق بين الزيت الصناعي والمعدني؟")
        looked.assert_not_called()
        llm.assert_called_once()
        reply, payload, looked, _ = self.ask("دوّر على النت على سعر الدولار", _staff("cashier"))
        self.assertEqual(payload["action"], "denied")
        looked.assert_not_called()
        self.assertEqual(self.ask("دوّر على النت على سعر الدولار")[0], assistant.WHO_ARE_YOU)

    def test_a_stock_question_stays_the_shops(self):
        _, payload, looked, _ = self.ask("عندك كوتش؟", _staff("manager"))
        looked.assert_not_called()
        self.assertTrue(payload["unresolved"])

    def test_search_down_says_so_or_falls_back(self):
        reply, payload, _, _ = self.ask("دوّر على النت على سعر الدولار", _staff("manager"), web=("", []))
        self.assertEqual(reply, assistant.WEB_DOWN)
        _, payload, _, llm = self.ask("إيه الفرق بين الزيت الصناعي والمعدني؟", _staff("manager"), web=("", []))
        llm.assert_called_once()                                # the LLM answers instead

    def test_dorli_ala_a_part_is_still_the_stock(self):
        oil = make_product(name="فلتر زيت", retail_price="450")
        make_inventory(oil, self.branch, quantity=3)
        reply, _, looked, _ = self.ask("دورلي على فلتر زيت", _staff("manager"))
        self.assertIn("450", reply)
        looked.assert_not_called()

    def test_offline_it_says_the_net_is_down(self):
        offline = Knowledge(services.offline_catalog(self.branch))
        self.assertEqual(offline.answer("يا موس دوّر على النت على سعر الدولار", now=0.0).text,
                         "النت فاصل دلوقتي، فمقدرش أدوّر. أول ما يرجع اسألني تاني.")
