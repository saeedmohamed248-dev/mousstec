"""
🛒 البوت يقفل البيعة — طلب العميل من الشات بيتسجّل «عرض سعر» في موس تك.

بنتأكد إن:
  • سطر [[ORDER: ...]] بيتقري صح (أرقام عربي، ×/x، أكتر من صنف) وبيتشال من الرد.
  • السعر دايماً من المخزون، والأكواد اللي مش موجودة بتتسجّل كنواقص.
  • الفاتورة بحالة «عرض سعر» فالمخزون مابيتخصمش لحد ما تتعتمد.
  • تعديل العميل لطلبه بيحدّث نفس عرض السعر بدل ما يعمل فاتورة جديدة.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from clients.models import Client
from inventory.models import Inventory, SaleInvoice
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from omnichannel.models import TenantChannelConfig
from omnichannel.services import llm
from omnichannel.services.orders import OrderRequest, create_draft_order, extract_order


class ExtractOrderTests(SimpleTestCase):
    def test_parses_items_name_phone_and_strips_tag(self):
        reply = "تمام يا أحمد، طلبك اتسجل ✅\n[[ORDER: BP-111×٢; bp-222 x 1 | أحمد علي | ٠١٠٠١٢٣٤٥٦٧]]"
        clean, order = extract_order(reply)
        self.assertEqual(clean, "تمام يا أحمد، طلبك اتسجل ✅")
        self.assertEqual(order.items, [("BP-111", 2), ("bp-222", 1)])
        self.assertEqual((order.name, order.phone), ("أحمد علي", "01001234567"))

    def test_no_tag_returns_reply_untouched(self):
        self.assertEqual(extract_order("بـ 450 ج"), ("بـ 450 ج", None))

    def test_broken_or_empty_tag_is_still_hidden_from_customer(self):
        clean, order = extract_order("تمام [[ORDER:  | x ]]")
        self.assertEqual((clean, order), ("تمام", None))

    def test_quantity_is_capped(self):
        _clean, order = extract_order("[[ORDER: A1×999]]")
        self.assertEqual(order.items, [("A1", 50)])

    def test_prompt_rules_only_when_enabled(self):
        cfg = TenantChannelConfig(tenant=Client(name="فيكست"))
        self.assertNotIn("[[ORDER:", llm.build_system_prompt(cfg, ""))
        cfg.auto_orders_enabled = True
        self.assertIn("[[ORDER:", llm.build_system_prompt(cfg, ""))


class CreateDraftOrderTests(ERPTenantTestCase):
    def setUp(self):
        self.branch = make_branch()
        self.p1 = make_product(part_number="BP-111", retail_price="450.00")
        make_inventory(self.p1, self.branch, quantity=5)

    def _order(self, *items, name="أحمد"):
        return OrderRequest(items=list(items), name=name)

    def test_whatsapp_order_becomes_quotation_with_stock_price(self):
        inv = create_draft_order(channel="whatsapp", sender_id="201001234567", sender_name="",
                                 order=self._order(("bp-111", 2), ("NOPE-9", 1)))
        self.assertEqual(inv.status, "quotation")
        self.assertEqual(inv.sales_channel, "chatbot")
        self.assertEqual(inv.customer.phone.replace("+", "")[-10:], "1001234567")
        [item] = list(inv.items.all())
        self.assertEqual((item.product_id, item.quantity, item.unit_price), (self.p1.pk, 2, Decimal("450.00")))
        self.assertIn("NOPE-9", inv.notes)
        # عرض سعر ⇒ المخزون زي ما هو
        self.assertEqual(Inventory.objects.get(product=self.p1, branch=self.branch).quantity, 5)

    def test_customer_edit_updates_same_quotation(self):
        first = create_draft_order(channel="whatsapp", sender_id="201001234567", sender_name="",
                                   order=self._order(("BP-111", 1)))
        second = create_draft_order(channel="whatsapp", sender_id="201001234567", sender_name="",
                                    order=self._order(("BP-111", 3)))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SaleInvoice.objects.filter(sales_channel="chatbot").count(), 1)
        self.assertEqual(second.items.get().quantity, 3)

    def test_unknown_codes_only_or_no_phone_creates_nothing(self):
        self.assertIsNone(create_draft_order(channel="whatsapp", sender_id="201", sender_name="",
                                             order=self._order(("NOPE", 1))))
        # ماسنجر من غير رقم موبايل ⇒ مانقدرش نسجّل عميل
        self.assertIsNone(create_draft_order(channel="messenger", sender_id="PSID1", sender_name="",
                                             order=self._order(("BP-111", 1))))
        self.assertFalse(SaleInvoice.objects.filter(sales_channel="chatbot").exists())
