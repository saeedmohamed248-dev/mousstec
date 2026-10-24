"""robot/brain/counter.py — the counter page on the shop's Wi-Fi, over real
HTTP: the PIN and its lockout, what each role may do, a part that's out
isn't sold, the forms only work from the page, and every sale, expense and
count lands in the outbox for the server. No database (test_offline_ledger_db
.py takes it to the books).
"""

import http.client
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlencode

from robot.brain import pins
from robot.brain.counter import Counter, money, serve
from robot.brain.knowledge import Knowledge
from robot.brain.ledger import Ledger
from robot.brain.outbox import Outbox

CATALOG = {
    "parts": [
        {"part_number": "11427953125", "name": "فلتر زيت", "car_model": "E90", "retail_price": 450.5, "stock": 3},
        {"part_number": "11287628649", "name": "سير مكنة", "retail_price": 300, "stock": 1},
        {"part_number": "NOPRICE", "name": "غطا رديتر", "retail_price": 0, "stock": 5},
    ],
    "staff": [
        {"id": 7, "name": "كريم", "pin": pins.hash_pin("1234", iterations=1000), "can": ["sale", "expense"]},
        {"id": 8, "name": "سامي", "pin": pins.hash_pin("5678", iterations=1000), "can": ["stock_take"]},
    ],
}


class CounterPageTests(unittest.TestCase):

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = Outbox(Path(self.tmp.name) / "outbox.jsonl")
        self.ledger = Ledger(Path(self.tmp.name) / "sold.json", self.box)
        self.counter = Counter(Knowledge(json.loads(json.dumps(CATALOG))), self.box, self.ledger,
                               online=lambda: False, log=lambda *_: None)
        self.server = serve(self.counter, "127.0.0.1", 0)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.cookie = ""

    def request(self, method, path, form=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = urlencode(form) if form is not None else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode("utf-8")
        set_cookie = resp.getheader("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";")[0]
        conn.close()
        return resp.status, text

    def login(self, employee=7, pin="1234"):
        return self.request("POST", "/login", {"employee": employee, "pin": pin})

    def csrf(self):
        token = next(iter(self.counter.sessions))
        return self.counter.sessions[token]["csrf"]

    def events(self, kind=None):
        return [e for e in self.box.pending() if kind is None or e["kind"] == kind]


class LoginTests(CounterPageTests):

    def test_names_then_the_pin(self):
        status, page = self.request("GET", "/")
        self.assertIn("كريم", page)
        self.assertIn('dir="rtl"', page)
        status, _ = self.login(pin="9999")
        self.assertEqual(status, 403)
        self.assertEqual(self.login()[0], 303)
        status, page = self.request("GET", "/")
        self.assertIn("أهلاً كريم", page)
        self.assertIn("بيع كاش", page)
        self.assertNotIn("📦 جرد", page)                                   # not his role

    def test_five_wrong_pins_wait(self):
        for _ in range(5):
            self.login(pin="0000")
        status, page = self.login()                                         # the right one, too soon
        self.assertEqual(status, 403)
        self.assertIn("محاولات غلط كتير", page)

    def test_arabic_digits_work(self):
        self.assertEqual(self.login(pin="١٢٣٤")[0], 303)

    def test_no_staff_yet_says_where_to_set_them(self):
        self.counter.k.update({"parts": CATALOG["parts"]})
        self.assertIn("أرقام الكاشير", self.request("GET", "/")[1])


class SaleTests(CounterPageTests):

    def test_search_add_confirm_receipt(self):
        self.login()
        status, page = self.request("GET", "/sale?q=" + quote("فلتر"))
        self.assertIn("فلتر زيت", page)
        self.assertIn("450.50 ج", page)
        csrf = self.csrf()
        self.assertEqual(self.request("POST", "/sale/add", {"csrf": csrf, "part": "11427953125", "qty": "2"})[0], 200)
        self.request("POST", "/sale/add", {"csrf": csrf, "part": "11287628649", "qty": "1"})
        status, _ = self.request("POST", "/sale/confirm", {"csrf": csrf, "customer_name": "أستاذ مجدي"})
        self.assertEqual(status, 303)
        status, receipt = self.request("GET", "/receipt")
        self.assertIn("1,201 ج", receipt)
        self.assertIn("أستاذ مجدي", receipt)
        (event,) = self.events("sale")
        payload = event["payload"]
        self.assertEqual(payload["employee_id"], 7)
        self.assertEqual([(i["part_number"], i["qty"], i["unit_price"]) for i in payload["items"]],
                         [("11427953125", 2, "450.5"), ("11287628649", 1, "300")])
        self.assertTrue(payload["receipt"].startswith("OFF-"))

    def test_whats_out_isnt_sold(self):
        self.login()
        csrf = self.csrf()
        status, page = self.request("POST", "/sale/add", {"csrf": csrf, "part": "11287628649", "qty": "2"})
        self.assertEqual(status, 400)
        self.assertIn("المتاح من سير مكنة 1 بس", page)
        self.request("POST", "/sale/add", {"csrf": csrf, "part": "11287628649", "qty": "1"})
        self.request("POST", "/sale/confirm", {"csrf": csrf})
        # Sold on this page: the next sale sees none left, though the catalog says 1.
        status, page = self.request("POST", "/sale/add", {"csrf": csrf, "part": "11287628649", "qty": "1"})
        self.assertIn("المتاح من سير مكنة 0 بس", page)

    def test_no_price_no_sale(self):
        self.login()
        _status, page = self.request("POST", "/sale/add", {"csrf": self.csrf(), "part": "NOPRICE", "qty": "1"})
        self.assertIn("سعره مش متسجل", page)

    def test_two_phones_cant_both_sell_the_last_one(self):
        import threading
        sessions = []
        for _ in range(2):
            token, _error = self.counter.login("10.0.0.%d" % len(sessions), 7, "1234")
            sess = self.counter.session(token)
            self.assertEqual(self.counter.add(sess, "11287628649", 1), "")     # the last belt, in both carts
            sessions.append(sess)
        slow = self.ledger.sold

        def sold():                                                         # widen the window
            out = slow()
            threading.Event().wait(0.05)
            return out
        self.ledger.sold = sold
        results = []
        threads = [threading.Thread(target=lambda s=s: results.append(self.counter.confirm(s))) for s in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(bool(receipt) for receipt, _e in results), [False, True])
        self.assertEqual(len(self.events("sale")), 1)

    def test_the_forms_only_work_from_the_page(self):
        self.login()
        status, _ = self.request("POST", "/sale/add", {"csrf": "forged", "part": "11427953125", "qty": "1"})
        self.assertEqual(status, 403)
        self.cookie = ""
        status, _ = self.request("POST", "/sale/confirm", {"csrf": "x"})
        self.assertEqual(status, 303)                                       # back to the login
        self.assertEqual(self.events(), [])

    def test_a_role_without_sales(self):
        self.login(employee=8, pin="5678")
        _status, page = self.request("POST", "/sale/add", {"csrf": self.csrf(), "part": "11427953125", "qty": "1"})
        self.assertIn("مش مسموحلك تبيع", page)


class ExpenseAndCountTests(CounterPageTests):

    def test_an_expense_is_recorded_at_once(self):
        self.login()
        status, page = self.request("POST", "/expense", {"csrf": self.csrf(), "amount": "٢٠٠", "description": "بنزين"})
        self.assertIn("المصروف اتسجّل", page)
        (event,) = self.events("expense")
        self.assertEqual((event["payload"]["amount"], event["payload"]["description"]), ("200.00", "بنزين"))
        status, page = self.request("POST", "/expense", {"csrf": self.csrf(), "amount": "-5", "description": "x"})
        self.assertEqual(status, 400)

    def test_nan_or_infinity_is_a_wrong_amount_not_a_crash(self):
        self.login()
        for typed in ("nan", "Infinity", "-inf"):
            status, page = self.request("POST", "/expense", {"csrf": self.csrf(), "amount": typed, "description": "x"})
            self.assertEqual(status, 400, typed)
            self.assertIn("المبلغ مش مظبوط", page)
        self.assertEqual(self.events(), [])

    def test_a_count(self):
        self.login(employee=8, pin="5678")
        page = self.request("GET", "/count?q=11427953125")[1]
        self.assertIn("فلتر زيت", page)
        self.request("POST", "/count", {"csrf": self.csrf(), "part": "11427953125", "counted": "4"})
        (event,) = self.events("count")
        self.assertEqual((event["payload"]["query"], event["payload"]["counted_qty"]), ("11427953125", 4))

    def test_logout(self):
        self.login()
        self.request("POST", "/logout", {"csrf": self.csrf()})
        self.assertIn("الرقم السري", self.request("GET", "/")[1])


class LedgerTests(unittest.TestCase):

    def test_sent_counts_until_the_next_catalog(self):
        with TemporaryDirectory() as tmp:
            box = Outbox(Path(tmp) / "o.jsonl")
            ledger = Ledger(Path(tmp) / "sold.json", box)
            box.add("sale", {"items": [{"part_number": "A", "qty": 2}]})
            box.add("expense", {"amount": "5"})
            self.assertEqual(ledger.sold()["A"], 2)
            box.flush(lambda events: {"applied": len(events), "failed": 0}, on_sent=ledger.sent)
            self.assertEqual(box.pending(), [])
            self.assertEqual(ledger.sold()["A"], 2)                          # sent, not in a catalog yet
            self.assertTrue(ledger.waiting_for_catalog())
            ledger.catalog_fetched()
            self.assertEqual(ledger.sold()["A"], 0)

    def test_the_brain_sends_then_fetches_the_stock_again(self):
        from unittest import mock

        from robot.brain import config, loop
        with TemporaryDirectory() as tmp:
            cfg = config.Config(api_base="x", token="y", home=Path(tmp))
            monitor = mock.Mock()
            monitor.sync_push.return_value = {"applied": 1, "failed": 0}
            monitor.catalog.return_value = {"parts": CATALOG["parts"], "generated_at": "now"}
            brain = loop.Brain(cfg, ears=mock.Mock(ready=False), server=monitor, mic=None,
                               speaker=mock.Mock(), log=lambda *_: None)
            brain.online = True
            brain.outbox.add("sale", {"items": [{"part_number": "11287628649", "qty": 1}]})
            self.assertEqual(brain.sync(), 1)
            monitor.catalog.assert_called_once()                            # right after the sale went
            self.assertFalse(brain.ledger.waiting_for_catalog())
            brain.online = False
            brain.outbox.add("expense", {"amount": "5"})
            self.assertEqual(brain.sync(), 0)                               # offline: kept
            self.assertEqual(len(brain.outbox.pending()), 1)

    def test_money_is_said_plainly(self):
        self.assertEqual(money("1201"), "1,201 ج")
        self.assertEqual(money(450.5), "450.50 ج")


if __name__ == "__main__":
    unittest.main()
