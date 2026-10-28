"""«🧠 مخ الفهم» (robot/understand.py): each sentence understood first — from every hearing of it,
what was just said and the step waited on — as a sentence the rules read for sure; the rules
still do it all. Every meaning is kept and teaches the shop's robot: understood the same way
twice, it's understood from then on without Gemini, offline too.

The sentences are the shop's own, from its logs (brain-0.9.9 and brain-0.9.10). Gemini is
stubbed with the meaning it gives."""

import json
import time
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import override_settings

from inventory.models import Branch, Inventory, Product
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_employee, make_inventory, make_product
from robot import services, understand, views, wizard
from robot.brain import talk
from robot.models import RobotDevice, RobotUnderstanding


def means(intent, command, *, meant="", needs_context=False, confidence=0.9):
    return json.dumps({"meant": meant or command, "intent": intent, "command": command,
                       "needs_context": needs_context, "confidence": confidence}, ensure_ascii=False)


@override_settings(GEMINI_API_KEY="test-key")
class UnderstandingTests(ERPTenantTestCase):

    def setUp(self):
        from hr.models import Employee

        cache.clear()
        self.addCleanup(cache.clear)
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-understand", branch=self.branch)
        self.device.issue_token()
        self.device.save()
        self.oil = make_product(name="فلتر زيت", part_number="11427953125", retail_price="450", car_model="")
        make_inventory(self.oil, self.branch, quantity=5)
        user, _profile = make_employee(username="boss", role="manager", branch=self.branch, first_name="سعيد")
        self.manager, _ = Employee.objects.get_or_create(user=user)
        for name in ("shelf_location", "ai_reply"):
            patcher = mock.patch.object(services, name, return_value="")
            patcher.start()
            self.addCleanup(patcher.stop)
        self.asked = []

    def say(self, text, meaning=None, employee="manager"):
        """One sentence to the robot (the button held), Gemini giving `meaning` (JSON) for it."""
        def gemini(parts, **kwargs):
            self.asked.append({"prompt": parts[0]["text"], "context": json.loads(parts[1]["text"]), **kwargs})
            return meaning or ""
        request = SimpleNamespace(data={"transcript": text, "ptt": "1"},
                                  robot_employee=self.manager if employee == "manager" else employee)
        with mock.patch("robot.audio.gemini_generate", side_effect=gemini):
            body, _listen = views._voice_turn(request, self.device, None, {"t0": time.monotonic()})
        return body

    # --- the car, from brain-0.9.10's log -------------------------------------

    def test_half_heard_car_is_understood_and_answers_the_part_asked(self):
        body = self.say("عندك فلتر زيت ايه فسيتي", means("part", "عندك فلتر زيت F30؟"))
        self.assertIn("فلتر زيت", body["reply"])
        self.assertEqual(body["understood"]["command"], "عندك فلتر زيت F30؟")
        self.assertIn("«عندك فلتر زيت F30؟» (part · gemini", body["meant"])     # the robot's log
        # «الساتي» (Azure: «The facility متوريني twenty؟») — «إف سيتي موتور إن توينتي».
        body = self.say("الساتي", means("car", "عربيتي F30 موتور N20"))
        self.assertIn("فلتر زيت", body["reply"])                        # the filter asked for, for it
        self.assertNotIn("Could", body["reply"])
        context = self.asked[-1]["context"]
        self.assertEqual(context["hearings"], ["الساتي"])
        self.assertIn("فلتر زيت", context["robot_said"])                # what was going on, sent along
        self.assertTrue(self.asked[-1]["json_out"])

    # --- a new part into the stock, from brain-0.9.10's log --------------------

    def test_a_new_part_with_the_steps_understood(self):
        body = self.say("تعرف تضيفلي في مخزون منتج جديد", means("stock", "ضيف منتج جديد للمخزون"))
        self.assertEqual(body["reply"], "تمام، صنف جديد. اسمه إيه؟")
        body = self.say("طقم فانوس اف فورتي فايف", means("answer", "طقم فانوس F45", needs_context=True))
        self.assertIn("صنف جديد اسمه طقم فانوس F45", body["reply"])
        waiting = self.asked[-1]["context"]["waiting_for"]
        self.assertEqual((waiting["kind"], waiting["step"]), ("stock", "item"))
        body = self.say("مش عارف رقمها", means("answer", "مفيش", needs_context=True))
        self.assertEqual(body["reply"], "سعر البيع كام؟")
        body = self.say("خليه بتمنمية وخمسين", means("answer", "850", needs_context=True))
        self.assertIn("اشتريت الحتة بكام", body["reply"])
        body = self.say("بستمية", means("answer", "600", needs_context=True))
        self.assertIn("هتضيف كام حتة", body["reply"])
        self.say("اتنين", means("answer", "2", needs_context=True))
        body = self.say("ايوه سجله", means("yes", "أيوه", needs_context=True))
        self.assertEqual(body["action"], "wizard_more", body["reply"])
        self.say("لا كده تمام", means("no", "لا", needs_context=True))
        self.asked.clear()
        body = self.say("هنا")                                          # «أنهي فرع؟»: the rules' own words
        self.assertEqual(self.asked, [])
        self.assertEqual(body["action"], "entry_done", body["reply"])
        made = Product.objects.get(name="طقم فانوس F45")
        self.assertEqual(Inventory.objects.get(product=made, branch=self.branch).quantity, 2)

    def test_thanks_whatever_the_teacher_guessed(self):
        self.say("ضيف طقم فانوس للمخزون", means("stock", "ضيف طقم فانوس للمخزون"))
        body = self.say("شكرا ليك obama", means("thanks", "شكرا"))
        self.assertEqual(body["action"], "wizard_cancelled", body["reply"])
        self.assertIsNone(wizard._get(self.device))

    def test_not_understood_is_asked_again_and_the_step_waits(self):
        self.say("ضيف طقم فانوس للمخزون", means("stock", "ضيف طقم فانوس للمخزون"))
        body = self.say("كيف صمت", means("unclear", "", confidence=0.1))
        self.assertEqual(body["reply"], talk.HEARD_BADLY)
        self.assertEqual(wizard._get(self.device)["step"], "new_ok")   # never a part called «كيف صمت»

    # --- what it never sees, and what stays the rules' -------------------------

    def test_a_pin_never_goes_to_gemini(self):
        from robot.brain import pins
        from robot.models import RobotStaffPin
        RobotStaffPin.objects.create(employee=self.manager, pin_hash=pins.hash_pin("2468", iterations=1000))
        self.say("اعمل فاتورة مبيعات", means("sale", "اعمل فاتورة مبيعات"), employee=None)
        self.asked.clear()
        self.say("اتنين اربعة ستة تمانية", means("answer", "2468"), employee=None)
        self.assertEqual(self.asked, [])
        self.assertFalse(RobotUnderstanding.objects.filter(heard__contains="اتنين").exists())

    def test_without_a_key_or_an_answer_the_rules_alone(self):
        with override_settings(GEMINI_API_KEY=""):
            body = self.say("عندك فلتر زيت", means("part", "عندك طرمبة"))
        self.assertEqual(self.asked, [])
        self.assertIn("فلتر زيت", body["reply"])
        body = self.say("عندك فلتر زيت", "not json")
        self.assertIn("فلتر زيت", body["reply"])
        self.assertNotIn("understood", body)
        self.assertTrue(body["meant"].startswith("— Gemini didn't answer in"))   # the robot's log says so

    def test_flash_lite_first_and_room_for_it(self):
        # brain-0.9.12's log: 2 s a model was under what it took — most sentences timed out.
        self.say("عندك فلتر زيت ايه فسيتي", means("part", "عندك فلتر زيت F30؟"))
        self.assertEqual(self.asked[-1]["prefer"], understand.MODEL)
        self.assertGreaterEqual(self.asked[-1]["timeout"], 3.0)
        # …and the robot's log says where the time went (the teacher, the understanding).
        self.assertTrue(views._timing_line({"t0": 0.0, "hear": 0.1, "think": 1.0, "teach_ms": 5, "und_ms": 800}, [])
                        .endswith("teach=5;und=800"))

    def test_understood_while_a_mobile_number_is_awaited(self):
        # brain-0.9.12's log: a part out («قولي رقم موبايلك») and every sentence after it went
        # un-understood — «بقولك فلتر زيت مش حساس زيت» was offered MINI filters for an E90.
        from robot import memory
        memory.remember(self.device, restock_for=self.oil.pk)
        self.say("بقولك فلتر زيت مش حساس زيت", means("part", "عندك فلتر زيت؟"))
        self.assertEqual(len(self.asked), 1)
        self.asked.clear()
        memory.remember(self.device, restock_for=self.oil.pk)          # (asked again: the part's out)
        body = self.say("صفر واحد صفر واحد اتنين تلاتة اربعة خمسة ستة سبعة تمانية", means("answer", "x"))
        self.assertEqual(self.asked, [])                                # the number: the rules' own
        self.assertEqual(body.get("action"), "restock_saved", body["reply"])

    def test_the_plain_words_need_no_asking(self):
        self.say("ضيف فلتر زيت للمخزون", means("stock", "ضيف فلتر زيت للمخزون"))
        self.asked.clear()
        self.say("3", means("answer", "3"))
        self.assertEqual(self.asked, [])
        wizard._end(self.device)
        self.say("ضيف منتج جديد اسمه طقم فانوس للمخزون", means("stock", "ضيف منتج جديد اسمه طقم فانوس للمخزون"))
        self.say("مفيش")
        self.asked.clear()
        body = self.say("1500")                                         # a price to «سعر البيع كام؟»
        self.assertEqual(self.asked, [])
        self.assertIn("اشتريت الحتة بكام", body["reply"])

    def test_a_figure_put_right_before_its_kept(self):
        # brain-0.9.12's log: «سعر البيع 1000، والشرا 6000 … أسجله؟» — «سعر البيع خطأ، سعر البيع
        # 15,000» was looked up as a part. And the dearer cost was read back as if fine.
        self.say("ضيف منتج جديد اسمه طقم فوانيس للمخزون", means("stock", "ضيف منتج جديد اسمه طقم فوانيس للمخزون"))
        for said in ("مفيش", "1000", "6000", "2"):
            body = self.say(said)
        self.assertIn("خلي بالك: الشرا أغلى من البيع", body["reply"])
        body = self.say("سعر البيع خطأ، سعر البيع 15,000", means("answer", "سعر البيع 15000", needs_context=True))
        self.assertIn("سعر البيع 15", body["reply"])
        self.assertNotIn("خلي بالك", body["reply"])
        self.assertEqual(wizard._get(self.device)["pending"]["retail"], "15000.00")
        body = self.say("العدد تلاتة", means("answer", "العدد 3", needs_context=True))
        self.assertIn("3 حتت", body["reply"])
        self.assertEqual(body["action"], "wizard_confirm")

    # --- what it learns --------------------------------------------------------

    def test_learned_twice_stands_in_when_gemini_doesnt_answer_and_offline(self):
        for _ in range(understand.LEARN_SEEN):
            self.say("عندي فلتر الزيت بتاع الاف تلاتين", means("part", "عندك فلتر زيت F30؟"))
        self.asked.clear()
        # «يفهم ذكي مش يحفظ»: understood afresh, with what's going on now — Gemini's asked again…
        body = self.say("عندي فلتر الزيت بتاع الاف تلاتين", means("part", "عندك فلتر زيت E90؟"))
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(body["understood"], {"intent": "part", "command": "عندك فلتر زيت E90؟", "by": "gemini",
                                              "ms": body["understood"]["ms"]})
        # …and what it learned stands in only when Gemini doesn't answer.
        RobotUnderstanding.objects.filter(command="عندك فلتر زيت E90؟").delete()
        body = self.say("عندي فلتر الزيت بتاع الاف تلاتين", "")
        self.assertEqual(body["understood"]["by"], "learned")
        self.assertIn("(part · learned)", body["meant"])
        self.assertIn({"heard": "عندي فلتر الزيت بتاع الاف تلاتين", "means": "عندك فلتر زيت F30؟"},
                      understand.lessons())
        catalog = services.offline_catalog(self.branch)
        self.assertIn({"heard": "عندي فلتر الزيت بتاع الاف تلاتين", "means": "عندك فلتر زيت F30؟"},
                      catalog["heard_sentences"])
        self.assertEqual(understand.summary()["at_once"], 1)

    def test_never_learned_what_needs_what_came_before_or_money(self):
        for _ in range(3):
            understand._keep(self.device, understand.Meaning("answer", "F20", needs_context=True), "توينتي يا باشا",
                             "", {})
            understand._keep(self.device, understand.Meaning("stock", "ضيف 3 بوجيه للمخزون"), "زود تلاتة بوجيه",
                             "", {})
        learned = understand.learned_meanings()
        self.assertNotIn("|توينتي يا باشا", learned)
        self.assertIn("|زود تلاته بوجيه", learned)                      # online, at once
        self.assertEqual(understand.lessons(), [])                      # …but never offline: a stock move

    def test_two_meanings_for_the_same_words_teach_nothing(self):
        understand._keep(self.device, understand.Meaning("part", "عندك طرمبة بنزين؟"), "عندك طرمبه", "", {})
        understand._keep(self.device, understand.Meaning("part", "عندك طرمبة مية؟"), "عندك طرمبه", "", {})
        understand._keep(self.device, understand.Meaning("part", "عندك طرمبة مية؟"), "عندك طرمبه", "", {})
        self.assertNotIn("|عندك طرمبه", understand.learned_meanings())

    def test_the_dataset_has_every_meaning(self):
        import io
        import zipfile

        from robot import teachers
        self.say("عندك فلتر زيت ايه فسيتي", means("part", "عندك فلتر زيت F30؟"))
        data = b"".join(teachers.training_zip())
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            lines = zf.read("understanding.jsonl").decode().splitlines()
        row = json.loads(lines[0])
        self.assertEqual((row["heard"], row["intent"], row["command"]),
                         ("عندك فلتر زيت ايه فسيتي", "part", "عندك فلتر زيت F30؟"))
