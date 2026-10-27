"""robot/memory.py + robot/brain/talk.py — the robot remembers what it was
just talking about.

«التانية» or «E90» answers «تقصد أنهي؟»; «وبكام؟», «مكانه فين؟», «عليه
ضمان؟», «ماركته إيه؟», «بيركب على E91؟», «فيه أرخص؟» are about the part
just named — answered from its own data. A sentence that names another part
is a new question. The LLM sees what was just said and that part's retail
data, and an answer with a price that isn't the catalog's is dropped. With
the net down, the brain says the same words. Runs on a tenant schema.
"""

from decimal import Decimal
from unittest import mock

from rest_framework.test import APIRequestFactory

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import memory, services, views
from robot.brain import talk
from robot.brain.knowledge import Knowledge
from robot.models import RobotDevice


class MemoryTestCase(ERPTenantTestCase):

    def setUp(self):
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="الرئيسي")
        self.device = RobotDevice(name="موس", device_uid="esp-memory", branch=self.branch)
        self.token = self.device.issue_token()
        self.device.save()
        memory.forget(self.device)
        self.addCleanup(memory.forget, self.device)

        self.e90 = make_product(name="طرمبة مية", part_number="11517586925", car_model="E90",
                                retail_price="1800")
        self.e90.brand, self.e90.warranty_months = "Pierburg", 12
        self.e90.chassis_compatibility = ["E90", "E91"]
        self.e90.save()
        make_inventory(self.e90, self.branch, quantity=2)
        self.f30 = make_product(name="طرمبة مية", part_number="11518635089", car_model="F30",
                                retail_price="2100")
        make_inventory(self.f30, self.branch, quantity=1)

        self.oil = make_product(name="فلتر زيت", part_number="11427953125", retail_price="450.50",
                                b2b_wholesale_price=Decimal("321.00"))
        self.oil.brand = ""
        self.oil.save()
        inv = make_inventory(self.oil, self.branch, quantity=3)
        inv.shelf_location = "B3"
        inv.save()
        for name, number, price, qty in (("فلتر مان", "HU816X", "300", 5), ("فلتر ماهلي", "OX387D", "520", 2)):
            alt = make_product(name=name, part_number=number, retail_price=price)
            alt.oem_cross_reference = ["11427953125"]
            alt.save()
            make_inventory(alt, self.branch, quantity=qty)

    def say(self, text, ai=""):
        with mock.patch.object(services, "ai_reply", return_value=ai) as llm, \
                mock.patch.object(services, "maybe_raise_procurement_signal"):
            intent, reply, payload = views._handle_voice(text, self.device, None)
            memory.note_turn(self.device, text, reply)          # what voice() does
        self.llm = llm
        return reply


class ChoiceTests(MemoryTestCase):

    def test_the_model_picks_one_of_the_offered_parts(self):
        self.assertIn("تقصد أنهي", self.say("عندك طرمبة مية؟"))
        self.assertEqual(self.say("E90"), "أيوه، طرمبة مية موجود، عندي منه حتتين، بسعر 1800 جنيه.")

    def test_by_position(self):
        self.say("عندك طرمبة مية؟")
        self.assertEqual(self.say("التانية"), "أيوه، طرمبة مية موجود، عندي منه حتة واحدة، بسعر 2100 جنيه.")

    def test_a_new_question_isnt_taken_for_an_answer(self):
        self.say("عندك طرمبة مية؟")
        self.assertIn("فلتر زيت", self.say("عندك فلتر زيت E90؟"))

    def test_a_model_none_of_them_has_picks_nothing(self):
        self.say("عندك طرمبة مية؟")
        self.assertIsNone(memory.follow_up("G20", self.device)[0])


class FollowUpTests(MemoryTestCase):

    def test_price_where_and_no_warranty(self):
        first = self.say("عندك فلتر زيت؟")
        self.assertEqual(first, "أيوه، فلتر زيت موجود، عندي منه 3 حتت، بسعر 450 جنيه و50 قرش. موجود في الرف B3.")
        self.assertEqual(self.say("وبكام؟"), first)
        self.assertEqual(self.say("مكانه فين؟"), "فلتر زيت في الرف B3.")
        self.assertEqual(self.say("عليه ضمان؟"), "مفيش ضمان متسجل على فلتر زيت. اسأل حد من الموظفين.")

    def test_which_branch_has_it(self):
        maadi = make_branch(name="المعادي")                   # the plan allows two branches
        make_inventory(self.oil, maadi, quantity=2)
        self.say("عندك فلتر زيت؟")
        self.assertEqual(self.say("في أنهي فرع؟"),
                         "فلتر زيت موجود هنا، عندي منه 3 حتت، وفي فرع المعادي حتتين.")
        belt = make_product(name="سير مكنة", part_number="11287628649", retail_price="300")
        make_inventory(belt, self.branch, quantity=0)
        make_inventory(belt, maadi, quantity=1)
        self.say("عندك سير مكنة؟")
        self.assertEqual(self.say("موجود في فرع تاني؟"),
                         "سير مكنة مش موجود هنا دلوقتي، وفي فرع المعادي حتة واحدة.")
        belt.inventory_set.update(quantity=0)
        self.assertEqual(self.say("والفروع؟"), "سير مكنة مش موجود في أي فرع دلوقتي.")

    def test_warranty_brand_and_what_it_fits(self):
        self.say("عندك طرمبة مية؟")
        self.say("E90")
        self.assertEqual(self.say("عليه ضمان؟"), "طرمبة مية عليه ضمان سنة.")
        self.assertEqual(self.say("ماركته إيه؟"), "طرمبة مية ماركة Pierburg، جديد، وعليه ضمان سنة.")
        self.assertEqual(self.say("بيركب على E91؟"), "أيوه، طرمبة مية بيركب على E91.")
        self.assertEqual(self.say("وعلى F30؟"),
                         "طرمبة مية مسجل إنه بيركب على E90، E91 بس. اتأكد من رقم الشاسيه مع الموظف.")

    def test_cheaper_and_alternatives_then_pick_one(self):
        self.say("عندك فلتر زيت؟")
        self.assertEqual(self.say("فيه أرخص؟"), "فيه كمان: فلتر مان بسعر 300 جنيه.")
        self.assertEqual(self.say("فيه بديل؟"), "فيه كمان: فلتر مان بسعر 300 جنيه، فلتر ماهلي بسعر 520 جنيه.")
        self.assertEqual(self.say("التانية"), "أيوه، فلتر ماهلي موجود، عندي منه حتتين، بسعر 520 جنيه.")

    def test_the_part_asked_about_stays_after_the_alternatives(self):
        self.say("عندك فلتر زيت؟")
        self.say("فيه بديل؟")
        self.assertEqual(self.say("مكانه فين؟"), "فلتر زيت في الرف B3.")

    def test_a_sentence_naming_another_part_is_a_new_question(self):
        self.say("عندك فلتر زيت؟")
        self.assertEqual(self.say("وبكام طرمبة المية F30؟"), "أيوه، طرمبة مية موجود، عندي منه حتة واحدة، بسعر 2100 جنيه.")

    def test_nothing_to_follow_without_a_conversation(self):
        # No part to say the price of: the robot asks which one (it was «ملقتش القطعة»).
        self.assertEqual(self.say("وبكام؟"), talk.ASK_PART)

    def test_a_cache_that_is_down_costs_nothing(self):
        with mock.patch("django.core.cache.cache.get", side_effect=ConnectionError), \
                mock.patch("django.core.cache.cache.set", side_effect=ConnectionError):
            self.assertIn("450 جنيه و50 قرش", self.say("عندك فلتر زيت؟"))
            self.assertEqual(self.say("وبكام؟"), talk.ASK_PART)


class ThinkingTests(MemoryTestCase):

    def test_the_llm_sees_the_part_and_what_was_said(self):
        self.say("عندك فلتر زيت؟")
        self.say("ده بيتغير كل قد إيه؟", ai="كل 10 آلاف كيلو تقريباً.")
        kwargs = self.llm.call_args.kwargs
        self.assertIn("فلتر زيت", kwargs["facts"])
        self.assertIn("450 جنيه و50 قرش", kwargs["facts"])
        self.assertIn("B3", self.say("مكانه فين؟"))           # still on the filter
        self.assertEqual(kwargs["history"][0][0], "عندك فلتر زيت؟")
        self.assertEqual(kwargs["branch"], self.branch.name)    # «انت في أنهي فرع؟»

    def test_the_facts_are_retail_only(self):
        facts = memory.facts(self.oil, self.branch)
        self.assertIn("450 جنيه و50 قرش", facts)
        self.assertNotIn("321", facts)

    def test_an_invented_price_is_dropped(self):
        facts = memory.facts(self.oil, self.branch)
        for answer, kept in (("سعره 450 جنيه و50 قرش وعليه ضمان.", True),
                             ("هو بـ 999 جنيه بس.", False),
                             ("سعره ٣٥٠ جنيه.", False),
                             ("بيتغير كل 10000 كيلو.", True)):
            with self.subTest(answer), \
                    mock.patch("inventory.ai_services.call_llm_layer", return_value=answer):
                said = services.ai_reply("بكام وبيتغير امتى؟", facts=facts, history=[["عندك فلتر؟", "أيوه"]])
                self.assertEqual(bool(said), kept)

    def test_history_reaches_the_llm_in_order(self):
        with mock.patch("inventory.ai_services.call_llm_layer", return_value="تمام.") as llm:
            services.ai_reply("وده؟", history=[["عندك فلتر زيت؟", "أيوه، متوفر."]])
        roles = [m["role"] for m in llm.call_args.args[0]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        with mock.patch("inventory.ai_services.call_llm_layer", return_value="أنا في فرع المعادي.") as llm:
            services.ai_reply("انت في أنهي فرع؟", branch="المعادي")
        self.assertIn("وانت في فرع المعادي.", llm.call_args.args[0][0]["content"])


class OfflineRemembersTheSameTests(MemoryTestCase):

    def test_a_whole_conversation_word_for_word(self):
        offline = Knowledge(services.offline_catalog(self.branch))
        for i, question in enumerate((
                "عندك طرمبة مية؟", "E90", "عليه ضمان؟", "ماركته إيه؟", "بيركب على E91؟", "وعلى F30؟",
                "عندك فلتر زيت؟", "وبكام؟", "مكانه فين؟", "فيه أرخص؟", "فيه بديل؟", "التانية",
                "عليه ضمان؟", "عندك طرمبة مية؟", "الأولانية")):
            with self.subTest(question):
                said = offline.answer("يا موس " + question, now=float(i)).text
                self.assertEqual(said, self.say(question))

    def test_offline_says_this_branch_and_the_others_the_way_the_server_does(self):
        # The catalog carries the other branches' stock now (robot/tests/test_all_branches*.py):
        # no other branch has it, so — like the server — just this one, no «محتاج النت».
        offline = Knowledge(services.offline_catalog(self.branch))
        offline.answer("يا موس عندك فلتر زيت؟", now=0.0)
        reply = offline.answer("يا موس في أنهي فرع؟", now=1.0)
        self.say("عندك فلتر زيت؟")
        self.assertEqual(reply.text, self.say("في أنهي فرع؟"))
        self.assertEqual(reply.text, "فلتر زيت موجود هنا، عندي منه 3 حتت.")
        clips = offline.clip_texts()
        self.assertTrue(all(c in clips or c == "فلتر زيت" for c in reply.segments), reply.segments)

    def test_offline_forgets_after_two_minutes(self):
        offline = Knowledge(services.offline_catalog(self.branch))
        offline.answer("يا موس عندك فلتر زيت؟", now=0.0)
        self.assertIn("B3", offline.answer("يا موس مكانه فين؟", now=60.0).text)
        late = offline.answer("يا موس مكانه فين؟", now=60.0 + 121)
        self.assertNotIn("B3", late.text)

    def test_the_snapshot_carries_what_follow_ups_need(self):
        request = APIRequestFactory().post("/api/robot/v1/sync/pull/", HTTP_X_ROBOT_TOKEN=self.token)
        data = views.sync_pull(request).data
        pump = next(p for p in data["parts"] if p["part_number"] == "11517586925")
        self.assertEqual((pump["brand"], pump["warranty_months"], pump["chassis"]), ("Pierburg", 12, ["E90", "E91"]))
        oil = next(p for p in data["parts"] if p["part_number"] == "11427953125")
        self.assertEqual(oil["shelf"], "B3")
        self.assertNotIn("321", str(data["parts"]))          # wholesale never leaves (not the timestamp)

    def test_the_voice_bank_holds_the_follow_up_words(self):
        clips = Knowledge(services.offline_catalog(self.branch)).clip_texts()
        for text in ("عليه ضمان", "سنة", "Pierburg", "E91", "في الرف", "B3", "فيه كمان:", "بسعر 300 جنيه"):
            self.assertIn(text, clips)
