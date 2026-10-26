"""The robot knows every branch: their stock offline («بس موجود في فرع X»,
«في أنهي فرع؟»), and the counter page records a sale or an expense on the
branch picked — after a screen that says what's written, on which branch.
robot/tests/test_all_branches_db.py takes them to the books."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from robot.brain import knowledge, talk
from robot.brain.counter import Counter
from robot.brain.knowledge import Knowledge
from robot.brain.ledger import Ledger
from robot.tests.test_brain_counter import CATALOG as COUNTER_CATALOG
from robot.tests.test_brain_counter import CounterPageTests

MAADI, NASR = {"id": 2, "name": "المعادي"}, {"id": 3, "name": "مدينة نصر"}
CATALOG = {
    "branch": "الرئيسي", "branch_id": 1, "branches": [{"id": 1, "name": "الرئيسي"}, MAADI, NASR],
    "parts": [
        {"part_number": "WP-1", "name": "طرمبة مية", "retail_price": 1800, "stock": 0,
         "elsewhere": [dict(MAADI, stock=4), dict(NASR, stock=1)]},
        {"part_number": "OF-1", "name": "فلتر زيت", "retail_price": 450, "stock": 3,
         "elsewhere": [dict(NASR, stock=2)]},
        {"part_number": "SP-1", "name": "مساعدين خلفي", "retail_price": 950, "stock": 0, "elsewhere": []},
    ],
}


class OfflineKnowsTheOtherBranchesTests(unittest.TestCase):

    def setUp(self):
        self.k = Knowledge(json.loads(json.dumps(CATALOG)))

    def test_out_here_but_in_another_branch(self):
        reply = self.k.answer("يا موس عندك طرمبة مية؟", now=10.0)
        self.assertEqual(reply.text, "طرمبة مية مش موجود هنا دلوقتي، وسعره 1800 جنيه. بس موجود في فرع المعادي وفرع مدينة نصر.")
        self.assertEqual(reply.segments[-4:], [knowledge.OTHER_BRANCH, "المعادي", knowledge.AND_BRANCH, "مدينة نصر"])
        self.assertTrue(all(c in self.k.clip_texts() for c in reply.segments[-4:]))   # said offline in its voice
        # Nowhere else: the number for when it comes, as before.
        self.assertIn(talk.RESTOCK_ASK, self.k.answer("يا موس فيه مساعدين خلفي؟", now=20.0).text)

    def test_which_branch_and_where(self):
        self.k.answer("يا موس عندك فلتر زيت؟", now=10.0)
        self.assertEqual(self.k.answer("يا موس في أنهي فرع؟", now=11.0).text,
                         "فلتر زيت موجود هنا، عندي منه 3 حتت، وفي فرع مدينة نصر حتتين.")
        self.k.answer("يا موس عندك طرمبة مية؟", now=12.0)
        self.assertEqual(self.k.answer("يا موس مكانها فين؟", now=13.0).text,
                         "طرمبة مية مش موجود هنا دلوقتي، بس موجود في فرع المعادي وفرع مدينة نصر.")

    def test_an_old_catalog_still_says_the_net_is_needed(self):
        old = json.loads(json.dumps(CATALOG))
        old.pop("branches")
        k = Knowledge(old)
        k.answer("يا موس عندك فلتر زيت؟", now=10.0)
        self.assertIn(talk.BRANCHES_NEED_NET[:-1], k.answer("يا موس في أنهي فرع؟", now=11.0).text)


class CounterOnAnyBranchTests(CounterPageTests):

    def setUp(self):
        super().setUp()
        catalog = json.loads(json.dumps(COUNTER_CATALOG))
        catalog.update({"branch": "الرئيسي", "branch_id": 1, "branches": CATALOG["branches"]})
        catalog["parts"][0]["elsewhere"] = [dict(MAADI, stock=5)]      # فلتر زيت: 3 here, 5 in المعادي
        self.counter.k.update(catalog)

    def test_a_sale_on_another_branch_after_the_confirm_screen(self):
        self.login()
        csrf = self.csrf()
        _s, page = self.request("GET", "/sale")
        self.assertIn("<select name='branch'>", page)                  # three branches: the picker shows
        self.request("POST", "/sale/branch", {"csrf": csrf, "branch": 2})
        status, page = self.request("POST", "/sale/add", {"csrf": csrf, "part": "11427953125", "qty": 5})
        self.assertEqual(status, 200, page)                            # 5: المعادي's, not the 3 here
        status, page = self.request("POST", "/sale/add", {"csrf": csrf, "part": "11427953125", "qty": 1})
        self.assertIn("المتاح من فلتر زيت 0 بس.", page)
        self.assertIn("التالي: راجع وأكّد", page)
        _s, review = self.request("POST", "/sale/review", {"csrf": csrf, "customer_name": "أحمد"})
        self.assertIn("على <b>فرع المعادي</b>", review)
        self.assertIn("✅ أكّد", review)
        self.assertEqual(self.events("sale"), [])                       # nothing written by the review
        status, _ = self.request("POST", "/sale/confirm", {"csrf": csrf, "customer_name": "أحمد"})
        self.assertEqual(status, 303)
        (sale,) = self.events("sale")
        self.assertEqual(sale["payload"]["branch_id"], 2)
        self.assertEqual(self.ledger.sold()["2/11427953125"], 5)        # المعادي's stock, not this branch's
        self.assertEqual(self.counter.left(self.counter.part("11427953125")), 3)
        _s, receipt = self.request("GET", "/receipt")
        self.assertIn("فرع المعادي", receipt)

    def test_a_new_branch_empties_the_cart(self):
        self.login()
        csrf = self.csrf()
        self.request("POST", "/sale/add", {"csrf": csrf, "part": "11427953125", "qty": 1})
        self.request("POST", "/sale/branch", {"csrf": csrf, "branch": 2})
        self.assertEqual(next(iter(self.counter.sessions.values()))["cart"], [])
        _s, page = self.request("POST", "/sale/branch", {"csrf": csrf, "branch": 99})
        self.assertIn("الفرع ده مش موجود.", page)

    def test_the_robots_own_branch_sends_no_branch(self):
        self.login()
        csrf = self.csrf()
        self.request("POST", "/sale/add", {"csrf": csrf, "part": "11427953125", "qty": 1})
        self.request("POST", "/sale/confirm", {"csrf": csrf})
        self.assertNotIn("branch_id", self.events("sale")[0]["payload"])
        self.assertEqual(self.ledger.sold()["11427953125"], 1)

    def test_an_expense_on_another_branch_after_the_confirm_screen(self):
        self.login()
        csrf = self.csrf()
        _s, page = self.request("GET", "/expense")
        self.assertIn("<select name='branch'>", page)
        _s, review = self.request("POST", "/expense/review",
                                  {"csrf": csrf, "amount": "200", "description": "بنزين", "branch": 3})
        self.assertIn("مصروف 200 ج", review)
        self.assertIn("على <b>فرع مدينة نصر</b>", review)
        self.assertEqual(self.events("expense"), [])
        _s, bad = self.request("POST", "/expense/review", {"csrf": csrf, "amount": "0", "description": "بنزين"})
        self.assertIn("المبلغ مش مظبوط.", bad)
        self.request("POST", "/expense", {"csrf": csrf, "amount": "200", "description": "بنزين", "branch": 3})
        (expense,) = self.events("expense")
        self.assertEqual((expense["payload"]["branch_id"], expense["payload"]["amount"]), (3, "200.00"))


class OneBranchTests(unittest.TestCase):

    def test_no_picker_with_one_branch(self):
        with TemporaryDirectory() as tmp:
            from robot.brain.outbox import Outbox
            box = Outbox(Path(tmp) / "o.jsonl")
            counter = Counter(Knowledge({"parts": [], "branches": [{"id": 1, "name": "الرئيسي"}], "branch_id": 1}),
                              box, Ledger(Path(tmp) / "s.json", box))
            self.assertEqual(counter.branch_of({"branch_id": None}), (None, ""))
            self.assertEqual(len(counter.branches()), 1)


if __name__ == "__main__":
    unittest.main()
