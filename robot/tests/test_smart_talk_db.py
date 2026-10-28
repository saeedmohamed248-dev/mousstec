"""The robot talks it through (robot/understand.py → views._answer_meant): what «🧠 مخ الفهم»
understood decides who answers — a workshop question (wiring, a fault code) goes to the repair
coach with what the system's own bots know (the diagnostics catalog Smart Diagnostics reads,
bmw_ecu's fault lines and pinout diagrams), and a general subject is discussed, the conversation
so far in mind. Gemini is stubbed: the meaning for «understand», the spoken answer for the rest."""

import time
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import override_settings

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import services, workshop
from robot.models import RobotDevice
from robot.tests.test_understand_db import means


@override_settings(GEMINI_API_KEY="test-key")
class SmartTalkTests(ERPTenantTestCase):

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-smart", branch=self.branch)
        self.device.issue_token()
        self.device.save()
        make_inventory(make_product(name="فلتر زيت", part_number="11427953125", retail_price="450", car_model=""),
                       self.branch, quantity=5)
        patcher = mock.patch.object(services, "shelf_location", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.prompts = []

    def say(self, text, meaning, reply="تمام. أكمّلك؟"):
        def gemini(parts, **kwargs):
            if kwargs.get("what") == "understand":
                return meaning
            self.prompts.append(" ".join(p.get("text", "") for p in parts))
            return reply
        request = SimpleNamespace(data={"transcript": text, "ptt": "1"}, robot_employee=None)
        with mock.patch("robot.audio.gemini_generate", side_effect=gemini):
            body, _listen = views_turn(request, self.device)
        return body

    def test_a_fault_code_is_explained_with_the_diagnostics_catalog(self):
        from diagnostics_catalog.models import DTCDefinition
        DTCDefinition.objects.update_or_create(code="P0301", defaults={
            "short_description": "مس فاير في السلندر الأول", "full_description": "احتراق ناقص في سلندر 1",
            "guided_steps": ["بدّل الكويل مع سلندر تاني", "افحص البوجيه"], "likely_oem_parts": ["12138657273"]})
        body = self.say("العربية طالعة كود بي زيرو تلاتة زيرو واحد",
                        means("workshop", "العربية طالعة كود P0301 يعني إيه؟"),
                        reply="ده مس فاير في السلندر الأول. أول حاجة بدّل الكويل. أكمّلك؟")
        self.assertEqual(body["reply"], "ده مس فاير في السلندر الأول. أول حاجة بدّل الكويل. أكمّلك؟")
        self.assertTrue(body.get("workshop"))
        prompt = self.prompts[-1]
        self.assertIn("أسطى Mouss Tec", prompt)                         # the system's repair coach…
        self.assertIn("📟 من كتالوج الأعطال عندنا", prompt)               # …with the diagnostics catalog
        self.assertIn("P0301: مس فاير في السلندر الأول", prompt)
        self.assertIn("بدّل الكويل مع سلندر تاني", prompt)
        self.assertIn("12138657273", prompt)                              # the parts likely at fault

    def test_a_code_not_in_the_catalog_gets_bmw_ecus_own_line(self):
        facts = workshop.fault_facts("كود P0300")
        self.assertIn("P0300: خبط عشوائي في أكتر من سلندر", facts)       # bmw_ecu.scan.dtc_decoder
        self.assertIn("مش الكود نفسه", workshop.fault_facts("كود P0999"))  # only its kind: said so

    def test_wiring_with_the_ecu_pinouts(self):
        from bmw_ecu.models import EcuPinoutDiagram
        EcuPinoutDiagram.objects.create(ecu_name="MSD80", image_url="/x.png",
                                        callouts=[{"pin": 18, "label": "BOOT"}, {"pin": 3, "label": "CAN-H"}])
        self.say("فيشة المكنة ام اس دي تمانين البن بتاع البوت فين",
                 means("workshop", "فيشة كمبيوتر MSD80 البن بتاع البوت فين؟"))
        prompt = self.prompts[-1]
        self.assertIn("🔌 من خرايط الفيش عندنا", prompt)
        self.assertIn("MSD80: بن 18: BOOT، بن 3: CAN-H", prompt)

    def test_a_general_subject_is_discussed_not_which_part(self):
        body = self.say("ايه رايك في ماتش امبارح", means("chat", "إيه رأيك في ماتش امبارح؟"),
                        reply="الماتش كان جامد والدفاع كان أحسن بكتير. انت كنت مع مين؟")
        self.assertEqual(body["reply"], "الماتش كان جامد والدفاع كان أحسن بكتير. انت كنت مع مين؟")
        self.assertTrue(body.get("discussion"))
        self.assertIn("يدردش أو يتناقش", self.prompts[-1])               # the discussion's own prompt
        # …and it goes on: what was said is in mind.
        self.say("انا كنت مع الاهلي", means("chat", "أنا كنت مع الأهلي"), reply="عاش، الأهلي لعب كويس.")
        self.assertIn("رأيك في ماتش امبارح", self.prompts[-1])

    def test_small_talk_and_parts_stay_the_rules(self):
        body = self.say("ازيك", means("chat", "إزيك؟"), reply="SHOULD-NOT")
        self.assertNotIn("SHOULD-NOT", body["reply"])                  # «الحمد لله…»: its own answer
        body = self.say("عندك فلتر زيت", means("chat", "عندك فلتر زيت؟"), reply="SHOULD-NOT")
        self.assertIn("فلتر زيت", body["reply"])
        self.assertNotIn("SHOULD-NOT", body["reply"])

    def test_without_understanding_a_fault_code_still_gets_the_coach(self):
        from diagnostics_catalog.models import DTCDefinition
        DTCDefinition.objects.update_or_create(code="P0420", defaults={"short_description": "كفاءة الشكمان ضعيفة"})
        body = self.say("العربية طالعة كود P0420", "", reply="ده الشكمان. أكمّلك؟")
        self.assertEqual(body["reply"], "ده الشكمان. أكمّلك؟")
        self.assertIn("P0420: كفاءة الشكمان ضعيفة", self.prompts[-1])


def views_turn(request, device):
    from robot import views
    return views._voice_turn(request, device, None, {"t0": time.monotonic()})
