"""robot/brain/voicedesk.py — a cash sale and an expense said to the robot with the net down:
the PIN, the amount read back before anything is written, the counter's own stock rule, and
what lands in the outbox for the server. And robot/brain/saynumber.py, which says the amount.
No database (test_offline_ledger_db.py takes the outbox to the books)."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from robot.brain import knowledge, pins, saynumber, voicedesk
from robot.brain.arabic_numbers import to_digits
from robot.brain.counter import Counter
from robot.brain.ledger import Ledger
from robot.brain.outbox import Outbox

CATALOG = {
    "parts": [
        {"part_number": "11427953125", "name": "فلتر زيت", "retail_price": 450, "stock": 3},
        {"part_number": "11287628649", "name": "سير مكنة", "retail_price": 300.5, "stock": 1},
        {"part_number": "NOPRICE", "name": "غطا رديتر", "retail_price": 0, "stock": 5},
    ],
    "staff": [
        {"id": 7, "name": "كريم", "pin": pins.hash_pin("1234", iterations=1000), "can": ["sale", "expense"]},
        {"id": 8, "name": "سامي", "pin": pins.hash_pin("5678", iterations=1000), "can": ["stock_take"]},
    ],
}


class SayNumberTests(unittest.TestCase):

    def test_said_the_egyptian_way(self):
        said = {0: "صفر", 7: "سبعة", 15: "خمستاشر", 25: "خمسة وعشرين", 100: "مية", 250: "ميتين وخمسين",
                900: "تسعمية", 1000: "ألف", 1250: "ألف وميتين وخمسين", 2005: "ألفين وخمسة",
                3000: "تلات تلاف", 11000: "حداشر ألف", 150000: "مية وخمسين ألف",
                999999: "تسعمية وتسعة وتسعين ألف وتسعمية وتسعة وتسعين"}
        for n, words in said.items():
            self.assertEqual(saynumber.text(n), words, n)

    def test_every_word_is_a_clip_the_bank_has(self):
        have = set(saynumber.clips())
        for n in list(range(0, 2100)) + [9999, 10000, 12345, 99999, 100000, 345678, 999999]:
            self.assertTrue(set(saynumber.words(n)) <= have, n)
        self.assertLess(len(have), 130)

    def test_what_it_says_is_heard_back_as_the_same_number(self):
        # The ears' own reading of numbers (arabic_numbers.to_digits) agrees with the mouth.
        for n in (7, 15, 25, 100, 250, 1250, 2005, 3000, 11000, 4500):
            self.assertEqual(to_digits(saynumber.text(n) + " جنيه").split()[0], str(n), n)

    def test_out_of_range(self):
        for n in (-1, 1_000_000):
            with self.assertRaises(ValueError):
                saynumber.words(n)


class VoiceDeskTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = Outbox(Path(self.tmp.name) / "outbox.jsonl")
        self.ledger = Ledger(Path(self.tmp.name) / "sold.json", self.box)
        self.k = knowledge.Knowledge(json.loads(json.dumps(CATALOG)))
        self.now, self.online, self.lines = 1000.0, False, []
        self.counter = Counter(self.k, self.box, self.ledger, online=lambda: False, log=self.lines.append)
        self.desk = voicedesk.VoiceDesk(self.counter, self.k.lookup, offline=lambda: not self.online,
                                        log=self.lines.append, clock=lambda: self.now)
        self.k.desk = self.desk

    def say(self, text):
        self.now += 2
        reply = self.k.answer("يا موس " + text, now=self.now)
        return reply.text

    def events(self, kind):
        return [e["payload"] for e in self.box.pending() if e["kind"] == kind]

    # ---- an expense
    def test_an_expense_pin_read_back_then_written(self):
        self.assertEqual(self.say("سجل مصروف ميتين وخمسين جنيه بنزين"), voicedesk.ASK_PIN)
        self.assertTrue(self.desk.secret)
        self.assertEqual(self.say("واحد اتنين تلاتة اربعة"), "مصروف ميتين وخمسين جنيه، أسجله؟")
        self.assertFalse(self.desk.secret)
        self.assertEqual(self.events("expense"), [])                     # nothing before «أيوه»
        self.assertEqual(self.say("أيوه"), voicedesk.DONE)
        (expense,) = self.events("expense")
        self.assertEqual((expense["amount"], expense["description"], expense["employee_id"]),
                         ("250.00", "بنزين", 7))

    def test_no_at_the_read_back_writes_nothing(self):
        self.say("سجل مصروف 200 جنيه شاي")
        self.say("1234")
        self.assertEqual(self.say("لا"), voicedesk.CANCELLED)
        self.assertEqual(self.say("أيوه")[:4] == voicedesk.DONE[:4], False)
        self.assertEqual(self.events("expense"), [])

    def test_no_amount_is_asked_for(self):
        self.assertEqual(self.say("سجل مصروف بنزين"), voicedesk.SAY_AMOUNT)

    # ---- a cash sale
    def test_a_cash_sale_read_back_with_its_total(self):
        self.say("بعت 2 فلتر زيت كاش")
        self.assertEqual(self.say("1234"), "بيع حتتين فلتر زيت بإجمالي تسعمية جنيه، أسجله؟")
        self.assertEqual(self.say("ايوه سجل"), voicedesk.DONE)
        (sale,) = self.events("sale")
        self.assertEqual(sale["items"], [{"part_number": "11427953125", "name": "فلتر زيت", "qty": 2,
                                          "unit_price": "450"}])
        self.assertEqual(sale["employee_id"], 7)
        # the PIN is remembered — and what's left counts what was just sold
        self.assertEqual(self.say("بعت 2 فلتر زيت كاش"), voicedesk.NOT_ENOUGH)
        self.assertEqual(self.say("بعت فلتر زيت"), "بيع حتة واحدة فلتر زيت بإجمالي ربعمية وخمسين جنيه، أسجله؟")
        self.assertEqual(self.say("تمام"), voicedesk.DONE)
        self.assertEqual(len(self.events("sale")), 2)

    def test_what_offline_cannot_sell(self):
        self.assertEqual(self.say("بعت 2 فلتر زيت آجل"), voicedesk.CREDIT_NEEDS_NET)
        self.assertEqual(self.say("بعت 2 شكمان كاش"), voicedesk.WHICH_PART)
        self.say("بعت غطا رديتر كاش")
        self.assertEqual(self.say("1234"), voicedesk.NO_PRICE_HERE)
        self.assertEqual(self.say("بعت سير مكنة كاش"), knowledge.ENTRY_NEEDS_NET)   # 300.50: piastres
        self.assertEqual(self.events("sale"), [])

    # ---- who
    def test_wrong_pins_lock_it_and_a_role_is_a_role(self):
        self.say("سجل مصروف 100 جنيه شاي")
        self.assertEqual(self.say("9999"), voicedesk.WRONG_PIN)
        self.assertEqual(self.say("8888"), voicedesk.WRONG_PIN)
        self.assertEqual(self.say("7777"), voicedesk.PIN_LOCKED)
        self.assertEqual(self.say("سجل مصروف 100 جنيه شاي"), voicedesk.PIN_LOCKED)
        self.now += voicedesk.LOCK_S + 1
        self.say("سجل مصروف 100 جنيه شاي")
        self.assertEqual(self.say("5678"), voicedesk.NOT_ALLOWED)          # سامي counts stock, no money
        self.assertEqual(self.events("expense"), [])

    def test_the_pin_is_never_in_the_log(self):
        self.say("سجل مصروف 100 جنيه شاي")
        self.say("واحد اتنين تلاتة اربعة")
        self.say("أيوه")
        self.assertFalse(any("1234" in str(line) for line in self.lines), self.lines)

    def test_a_question_left_unanswered_is_forgotten(self):
        self.say("سجل مصروف 100 جنيه شاي")
        self.now += voicedesk.STEP_S + 5
        self.assertIn("فلتر زيت", self.say("عندك فلتر زيت"))                # not taken for a PIN
        self.assertEqual(self.events("expense"), [])

    # ---- the rest stays the server's
    def test_online_it_is_the_servers(self):
        self.online = True
        self.assertIsNone(self.desk.hear("سجل مصروف 100 جنيه شاي"))

    def test_the_rest_of_the_books_still_needs_the_net(self):
        for said in ("حصل 500 جنيه من أحمد محمود", "اشترينا 3 فلتر زيت من الأمل بسعر 150", "اعمل عرض سعر"):
            self.assertEqual(self.say(said), knowledge.ENTRY_NEEDS_NET, said)
        self.assertIn("فلتر زيت", self.say("عندك فلتر زيت"))

    def test_every_sentence_has_its_clip(self):
        have = set(self.k.clip_texts())
        self.say("بعت 2 فلتر زيت كاش")
        self.now += 2
        reply = self.k.answer("يا موس 1234", now=self.now)
        self.assertTrue(set(reply.segments) <= have, set(reply.segments) - have)
        self.assertTrue(set(voicedesk.FIXED) <= have)
