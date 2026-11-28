"""«🧠 المخ بأدوات» (robot/agent.py): what the rules can't answer, Gemini answers from the system
itself — it calls a tool, the tool reads the stock, and only the tool's price is ever said.
Gemini is stubbed: the tools it calls, then the words it says."""

import json
from unittest import mock

from django.test import override_settings

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import agent, services, views
from robot.models import RobotDevice


def _gemini(*turns):
    """requests.post answering Gemini's turns in order: a dict is a tool call, a str the answer."""
    replies = []
    for turn in turns:
        if isinstance(turn, dict):
            part = {"functionCall": turn}
        else:
            part = {"text": turn}
        resp = mock.Mock(status_code=200, text="")
        resp.json.return_value = {"candidates": [{"content": {"role": "model", "parts": [part]}}]}
        replies.append(resp)
    return mock.patch("requests.post", side_effect=replies)


@override_settings(ROBOT_AGENT=True, GEMINI_API_KEY="gm-key")
class AgentTests(ERPTenantTestCase):

    def setUp(self):
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="المعادي")
        self.device = RobotDevice(name="موس", device_uid="esp-agent", branch=self.branch)
        self.device.issue_token()
        self.device.save()
        self.lamp = make_product(name="طقم فانوس امامي f30 زينون", part_number="63117000000",
                                 retail_price="14999", car_model="F30")
        make_inventory(self.lamp, self.branch, quantity=1)

    def test_it_reads_the_stock_and_says_its_price(self):
        said = "أيوه، طقم فانوس امامي F30 زينون موجود عندي حتة واحدة بسعر 14999 جنيه."
        with _gemini({"name": "find_parts", "args": {"query": "فانوس زينون F30"}}, said) as post:
            reply = agent.answer(self.device, None, "عندكم فوانيس زينون للـ F30؟")
        self.assertEqual(reply, said)
        second = post.call_args_list[1].kwargs["json"]
        result = second["contents"][-1]["parts"][0]["functionResponse"]
        self.assertEqual(result["name"], "find_parts")
        self.assertEqual(result["response"]["parts"][0]["part_number"], "63117000000")
        self.assertEqual(result["response"]["parts"][0]["price"], "14999 جنيه")
        self.assertIn("سيستم المحل", second["systemInstruction"]["parts"][0]["text"])
        self.assertTrue(second["tools"][0]["functionDeclarations"])

    def test_a_price_no_tool_gave_is_never_said(self):
        with _gemini({"name": "find_parts", "args": {"query": "فانوس"}}, "الفانوس بـ 9000 جنيه بس."):
            self.assertEqual(agent.answer(self.device, None, "الفانوس بكام؟"), "")

    def test_the_staffs_numbers_keep_their_roles(self):
        got = agent.call("shop_numbers", {"kind": "sales_today"}, self.device, None)
        self.assertFalse(got["allowed"])                                  # nobody known in front of it
        self.assertEqual(agent.call("nonsense", {}, self.device, None), {"error": "unknown tool"})
        details = agent.call("part_details", {"part_number": "63117000000"}, self.device, None)
        self.assertEqual((details["name"], details["stock_here"]), ("طقم فانوس امامي f30 زينون", 1))
        self.assertNotIn("cost", json.dumps(details))

    def test_what_the_rules_cant_answer_goes_to_it_then_the_general_ai(self):
        asked = "إيه أكتر حاجة بتبوظ في العربيات الألماني في الصيف؟"
        answer = "في الصيف أكتر حاجة بتتعب التبريد: الطرمبة والرديتر والترموستات. تحب أشوفلك حاجة منهم؟"
        with _gemini({"name": "find_parts", "args": {"query": "طرمبة مية"}}, answer), \
                mock.patch.object(services, "ai_reply", return_value="رد عام") as general:
            _intent, reply, _payload = views._handle_voice(asked, self.device, None)
        self.assertEqual(reply, answer)
        general.assert_not_called()
        with override_settings(ROBOT_AGENT=False), \
                mock.patch.object(services, "ai_reply", return_value="رد عام") as general:
            _intent, reply, _payload = views._handle_voice(asked, self.device, None)
        self.assertEqual(reply, "رد عام")                                 # off: the general AI, as before
