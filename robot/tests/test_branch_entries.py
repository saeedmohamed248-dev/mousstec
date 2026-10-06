"""Nothing goes on the books until someone names the branch.

The robot records expenses and purchases by voice, and `/sale/` creates sale
invoices. Before any of them is written it asks «أسجلها على أنهي فرع؟»: a
branch name, «هنا» (the robot's branch) or «الغي» answers it. Money rules match
the ERP screens: an expense or a cash purchase comes out of the chosen
branch's cash treasury and is refused when there isn't enough; a cash sale is
refused when the branch has no cash treasury at all.

No database: models are stubbed, like the other robot tests.
"""

from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from robot import entries, services, views


def _branch(pk, name):
    b = mock.Mock(pk=pk, id=pk)
    b.name = name
    return b


MAADI, NASR, NASR2 = _branch(1, "المعادي"), _branch(2, "مدينة نصر"), _branch(3, "مدينة نصر 2")
BRANCHES = [MAADI, NASR, NASR2]


class ParsingTests(SimpleTestCase):

    def test_expense_amount_and_description(self):
        self.assertEqual(entries.parse_expense("سجل مصروف 200 جنيه بنزين"),
                         {"amount": Decimal("200"), "description": "بنزين"})

    def test_expense_with_arabic_digits_and_spent_wording(self):
        self.assertEqual(entries.parse_expense("مصروف ٥٠ شاي")["amount"], Decimal("50"))
        exp = entries.parse_expense("صرفنا 150 على كهربا")
        self.assertEqual((exp["amount"], exp["description"]), (Decimal("150"), "كهربا"))

    def test_expense_without_an_amount_is_flagged(self):
        self.assertIsNone(entries.parse_expense("سجل مصروف بنزين")["amount"])

    def test_other_speech_is_not_an_expense(self):
        self.assertIsNone(entries.parse_expense("عندك فلتر زيت؟"))

    def test_purchase_with_unit_price_and_cash(self):
        self.assertEqual(
            entries.parse_purchase("اشترينا 3 فلتر زيت من الأمل بسعر 150 كاش"),
            {"quantity": 3, "query": "فلتر زيت", "vendor": "الأمل",
             "unit_cost": Decimal("150"), "cash": True})

    def test_purchase_total_is_split_per_piece_and_defaults_to_credit(self):
        pur = entries.parse_purchase("فاتورة شراء 2 بوجيه من النور بإجمالي 300")
        self.assertEqual((pur["unit_cost"], pur["cash"]), (Decimal("150.00"), False))
        pur = entries.parse_purchase("اشترينا 4 بوجيه بسعر 100 الكل من النور")
        self.assertEqual((pur["unit_cost"], pur["vendor"]), (Decimal("25.00"), "النور"))


class BranchMatchingTests(SimpleTestCase):

    def test_branch_name_with_or_without_the_word_branch(self):
        self.assertIs(entries.match_branch("فرع المعادي", BRANCHES, NASR), MAADI)
        self.assertIs(entries.match_branch("المعادى", BRANCHES, NASR), MAADI)   # ى/ي

    def test_longest_name_wins(self):
        self.assertIs(entries.match_branch("مدينة نصر 2", BRANCHES, MAADI), NASR2)
        self.assertIs(entries.match_branch("مدينة نصر", BRANCHES, MAADI), NASR)

    def test_here_means_the_robots_branch(self):
        self.assertIs(entries.match_branch("هنا", BRANCHES, NASR), NASR)

    def test_anything_else_is_not_a_branch(self):
        self.assertIsNone(entries.match_branch("الشاي برد", BRANCHES, NASR))

    def test_the_question_lists_the_branches(self):
        q = entries.branch_question(mock.Mock(branch=MAADI), BRANCHES)
        for b in BRANCHES:
            self.assertIn(b.name, q)
        self.assertIn("الغي", q)


class AnswerTests(SimpleTestCase):

    def _entry(self):
        e = mock.Mock(pk=5, kind="expense", summary="مصروف 200 جنيه (بنزين)")
        e.device.branch = NASR
        return e

    def _answer(self, text, *, record_error=None):
        entry = self._entry()
        with mock.patch.object(entries, "all_branches", return_value=BRANCHES), \
                mock.patch.object(entries, "record",
                                  side_effect=record_error) as record:
            result = entries.answer(entry, text)
        return result, entry, record

    def test_naming_a_branch_records_it_there(self):
        (reply, payload), _, record = self._answer("المعادي")
        record.assert_called_once()
        self.assertIs(record.call_args.args[1], MAADI)
        self.assertIn("المعادي", reply)
        self.assertEqual(payload["action"], "entry_done")

    def test_cancel_records_nothing(self):
        (_, payload), entry, record = self._answer("الغي")
        record.assert_not_called()
        self.assertEqual(entry.status, "cancelled")
        self.assertEqual(payload["action"], "entry_cancelled")

    def test_a_refusal_is_spoken_and_kept(self):
        (reply, payload), entry, _ = self._answer(
            "هنا", record_error=entries.EntryError("رصيد خزنة فرع مدينة نصر مش مكفي"))
        self.assertIn("مش مكفي", reply)
        self.assertEqual(entry.status, "failed")
        self.assertIs(entry.branch, NASR)

    def test_unrelated_speech_is_not_an_answer(self):
        result, _, record = self._answer("عندك بوجيهات؟")
        self.assertIsNone(result)
        record.assert_not_called()


class VoiceRequestTests(SimpleTestCase):

    def _request(self, text, *, allowed=True, product=None, vendor=None, salaries=False):
        device = mock.Mock(branch=NASR)
        with mock.patch("robot.permissions.employee_can", return_value=allowed), \
                mock.patch.object(entries, "expense_category",
                                  return_value=(mock.Mock(pk=3), salaries)), \
                mock.patch.object(services, "find_product", return_value=product), \
                mock.patch.object(entries, "find_vendor", return_value=vendor), \
                mock.patch.object(entries, "ask", return_value="أسجلها على أنهي فرع؟") as ask:
            result = entries.voice_request(text, device, mock.Mock())
        return result, ask

    def test_an_expense_waits_for_the_branch(self):
        (reply, payload), ask = self._request("سجل مصروف 200 جنيه بنزين")
        self.assertEqual(payload["action"], "entry_ask_branch")
        self.assertEqual(ask.call_args.kwargs["kind"], "expense")
        self.assertEqual(ask.call_args.kwargs["data"]["amount"], "200")

    def test_an_expense_needs_the_role(self):
        (_, payload), ask = self._request("سجل مصروف 200 جنيه بنزين", allowed=False)
        self.assertEqual(payload["action"], "denied")
        ask.assert_not_called()

    def test_an_expense_needs_an_amount(self):
        (_, payload), ask = self._request("سجل مصروف بنزين")
        self.assertEqual(payload["action"], "entry_needs_amount")
        ask.assert_not_called()

    def test_salaries_are_not_recorded_by_voice(self):
        (_, payload), ask = self._request("مصروف 3000 مرتبات", salaries=True)
        self.assertEqual(payload["action"], "entry_salaries")
        ask.assert_not_called()

    def test_a_purchase_waits_for_the_branch(self):
        product, vendor = mock.Mock(pk=7), mock.Mock(pk=8)
        product.name, vendor.name = "فلتر زيت", "الأمل"
        (reply, payload), ask = self._request(
            "اشترينا 3 فلتر زيت من الأمل بسعر 150 كاش", product=product, vendor=vendor)
        self.assertEqual(ask.call_args.kwargs["kind"], "purchase")
        self.assertEqual(ask.call_args.kwargs["data"],
                         {"product_id": 7, "vendor_id": 8, "quantity": 3,
                          "unit_cost": "150", "cash": True})
        self.assertEqual(ask.call_args.kwargs["amount"], Decimal("450"))

    def test_an_unknown_vendor_is_not_guessed(self):
        product = mock.Mock(pk=7)
        product.name = "فلتر زيت"
        (reply, _), ask = self._request(
            "اشترينا 3 فلتر زيت من حد ما بسعر 150", product=product, vendor=None)
        self.assertIn("مش لاقي المورد", reply)
        ask.assert_not_called()


class VoiceRoutingTests(SimpleTestCase):

    def test_a_pending_question_takes_the_answer(self):
        pending = mock.Mock(pk=5)
        with mock.patch.object(entries, "pending_for", return_value=pending), \
                mock.patch.object(entries, "answer",
                                  return_value=("تمام، سجلت", {"action": "entry_done"})):
            result = views._handle_voice("المعادي", mock.Mock(), mock.Mock())
        self.assertEqual(result, ("command", "تمام، سجلت", {"action": "entry_done"}))

    def test_a_garbled_short_answer_is_asked_again(self):
        with mock.patch.object(entries, "pending_for", return_value=mock.Mock(pk=5)), \
                mock.patch.object(entries, "answer", return_value=None), \
                mock.patch.object(entries, "branch_question", return_value="أنهي فرع؟"):
            _, reply, payload = views._handle_voice("فرع إيه", mock.Mock(), mock.Mock())
        self.assertEqual(payload["action"], "entry_ask_branch_again")

    def test_a_new_question_is_still_answered(self):
        ans = {"found": True, "name": "فلتر زيت", "stock": 0, "retail_price": 100, "id": 1}
        with mock.patch.object(entries, "pending_for", return_value=mock.Mock(pk=5)), \
                mock.patch.object(entries, "answer", return_value=None), \
                mock.patch.object(views.enrollment, "active_session", return_value=None), \
                mock.patch.object(services, "get_open_stock_take", return_value=None), \
                mock.patch.object(services, "inventory_answer", return_value=ans), \
                mock.patch.object(services, "maybe_raise_procurement_signal"), \
                mock.patch.dict("sys.modules", {"inventory.models": mock.Mock()}):
            intent, _, _ = views._handle_voice("عندك فلتر زيت لعربية بي ام", mock.Mock(),
                                               mock.Mock())
        self.assertEqual(intent, "inventory_query")


class SaleAsksForTheBranchTests(SimpleTestCase):

    def _sell(self, body, *, treasury=True):
        request = APIRequestFactory().post("/api/robot/v1/sale/",
                                           {"part_number": "F", **body}, format="json")
        product = mock.Mock(scrap_price=Decimal("0"), retail_price=Decimal("100"))
        product.name = "فلتر"
        device = mock.Mock(branch=NASR, branch_id=NASR.pk)
        fake_inventory = mock.Mock()
        fake_inventory.Customer.get_or_create_by_phone.return_value = (mock.Mock(), False)
        fake_inventory.Branch.objects.order_by.return_value = BRANCHES
        with mock.patch.object(views, "_device_or_401", return_value=(device, None)), \
                mock.patch.object(views, "_require_permission", return_value=(mock.Mock(), None)), \
                mock.patch.object(views.services, "find_product", return_value=product), \
                mock.patch.object(views.services, "branch_stock", return_value=10) as stock, \
                mock.patch.dict("sys.modules", {"inventory.models": fake_inventory}), \
                mock.patch.object(entries, "branch_by_id",
                                  side_effect=lambda v: {1: MAADI, 2: NASR}.get(int(v))), \
                mock.patch.object(entries, "cash_treasury",
                                  return_value=mock.Mock() if treasury else None), \
                mock.patch.object(entries, "log_sale") as log, \
                mock.patch.object(views.services, "maybe_raise_procurement_signal"), \
                mock.patch.object(views.services, "create_robot_sale") as create:
            create.return_value = mock.Mock(id=1, total_amount=Decimal("100"), status="posted")
            response = views.sale(request)
        return response, create, stock, log

    def test_without_a_branch_nothing_is_created_and_the_question_comes_back(self):
        response, create, _, _ = self._sell({})
        self.assertEqual(response.status_code, 428)
        self.assertTrue(response.data["needs_branch"])
        self.assertEqual([b["name"] for b in response.data["branches"]],
                         ["المعادي", "مدينة نصر", "مدينة نصر 2"])
        create.assert_not_called()

    def test_the_named_branch_is_used_for_stock_and_invoice(self):
        response, create, stock, log = self._sell({"branch_id": 1})
        self.assertEqual(response.status_code, 201)
        self.assertIs(create.call_args.kwargs["branch"], MAADI)
        self.assertIs(stock.call_args.args[1], MAADI)
        self.assertIs(log.call_args.kwargs["branch"], MAADI)
        self.assertEqual(response.data["branch"], "المعادي")

    def test_an_unknown_branch_is_refused(self):
        response, create, _, _ = self._sell({"branch_id": 99})
        self.assertEqual(response.status_code, 400)
        create.assert_not_called()

    def test_cash_sale_needs_a_cash_treasury(self):
        response, create, _, _ = self._sell({"branch_id": 1, "payment": "cash"}, treasury=False)
        self.assertEqual(response.status_code, 409)
        self.assertIn("خزنة كاش", response.data["detail"])
        create.assert_not_called()

    def test_credit_sale_needs_no_treasury(self):
        response, create, _, _ = self._sell({"branch_id": 1, "payment": "credit"},
                                             treasury=False)
        self.assertEqual(response.status_code, 201)


class CreateRobotSaleGuardTests(SimpleTestCase):

    def test_cash_sale_without_treasury_writes_nothing(self):
        fake_inventory = mock.Mock()
        with mock.patch.dict("sys.modules", {"inventory.models": fake_inventory}), \
                mock.patch.object(services, "_cash_treasury", return_value=None):
            with self.assertRaises(ValueError):
                # __wrapped__: skip @transaction.atomic (no DB here).
                services.create_robot_sale.__wrapped__(product=mock.Mock(retail_price=100), branch=NASR,
                                           customer=mock.Mock(), payment="cash")
        fake_inventory.SaleInvoice.objects.create.assert_not_called()


class RecordingTests(SimpleTestCase):

    def setUp(self):
        patcher = mock.patch("robot.entries.transaction")   # no DB transaction
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_expense_needs_a_cash_treasury(self):
        with mock.patch.object(entries, "cash_treasury", return_value=None), \
                mock.patch.dict("sys.modules", {"inventory.models": mock.Mock()}):
            with self.assertRaisesRegex(entries.EntryError, "مالوش خزنة كاش"):
                entries.record_expense(branch=MAADI, amount=Decimal("200"), description="بنزين")

    def test_expense_needs_enough_balance(self):
        fake = mock.Mock()
        with mock.patch.object(entries, "cash_treasury",
                               return_value=mock.Mock(balance=Decimal("50"))), \
                mock.patch.dict("sys.modules", {"inventory.models": fake}):
            with self.assertRaisesRegex(entries.EntryError, "مش مكفي"):
                entries.record_expense(branch=MAADI, amount=Decimal("200"), description="بنزين")
        fake.FinancialTransaction.objects.create.assert_not_called()

    def test_expense_comes_out_of_the_chosen_branch_treasury(self):
        fake, treasury = mock.Mock(), mock.Mock(balance=Decimal("1000"))
        with mock.patch.object(entries, "cash_treasury", return_value=treasury) as find, \
                mock.patch.dict("sys.modules", {"inventory.models": fake}):
            entries.record_expense(branch=MAADI, amount=Decimal("200"), description="بنزين")
        self.assertIs(find.call_args.args[0], MAADI)
        kwargs = fake.FinancialTransaction.objects.create.call_args.kwargs
        self.assertEqual((kwargs["treasury"], kwargs["transaction_type"], kwargs["amount"]),
                         (treasury, "out", Decimal("200")))

    def test_cash_purchase_needs_enough_balance(self):
        fake = mock.Mock()
        with mock.patch.object(entries, "cash_treasury",
                               return_value=mock.Mock(balance=Decimal("100"))), \
                mock.patch.dict("sys.modules", {"inventory.models": fake}):
            with self.assertRaises(entries.EntryError):
                entries.record_purchase(branch=MAADI, product_id=1, vendor_id=2, quantity=3,
                                        unit_cost=Decimal("150"), cash=True)
        fake.PurchaseInvoice.objects.create.assert_not_called()

    def test_credit_purchase_is_posted_on_the_chosen_branch(self):
        fake = mock.Mock()
        invoice = fake.PurchaseInvoice.objects.create.return_value
        with mock.patch.object(entries, "cash_treasury") as find, \
                mock.patch.dict("sys.modules", {"inventory.models": fake}):
            entries.record_purchase(branch=MAADI, product_id=1, vendor_id=2, quantity=3,
                                    unit_cost=Decimal("150"), cash=False)
        find.assert_not_called()
        self.assertIs(fake.PurchaseInvoice.objects.create.call_args.kwargs["branch"], MAADI)
        self.assertEqual(invoice.status, "posted")


class NoNameNeededOnlyForBranchNamesTests(SimpleTestCase):

    def _is_answer(self, text):
        device = mock.Mock(branch=NASR)
        with mock.patch.object(entries, "pending_for", return_value=mock.Mock()), \
                mock.patch.object(entries, "all_branches", return_value=BRANCHES):
            return entries.is_answer(device, text)

    def test_a_branch_name_needs_no_wake_name(self):
        self.assertTrue(self._is_answer("المعادي"))

    def test_chatter_like_no_or_here_does_not_answer(self):
        # Said to someone else nearby, these must not cancel or place the entry.
        self.assertFalse(self._is_answer("لا"))
        self.assertFalse(self._is_answer("هنا"))
