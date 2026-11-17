"""
🐛 «عندك طنابير خلفي F30؟» ⇒ البوت كان بيرد «ماشة خلفي F30» و«اكصدام خلفي F30»
على إنهم متوفرين، لأن «خلفي» و«F30» لوحدهم كانوا بيكسبوا النقط.
"""
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from omnichannel.services.inventory_context import quick_catalog_reply


class QuickReplyPartMatchTests(ERPTenantTestCase):
    def setUp(self):
        branch = make_branch()
        for name, code in (("ماشة خلفي - F30", "QR-1"), ("اكصدام خلفي f30", "QR-2"),
                           ("طنابير خلفي F30 بيلشتاين", "QR-3")):
            make_inventory(make_product(part_number=code, name=name, car_model="F30"), branch, 3)

    def test_only_the_requested_part_is_offered(self):
        reply = quick_catalog_reply("عندك طنابير خلفي f30")
        self.assertIn("طنابير", reply)
        self.assertNotIn("ماشة", reply)
        self.assertNotIn("اكصدام", reply)

    def test_unknown_part_gets_no_quick_reply(self):
        # مفيش «مساعد» في المخزون ⇒ مانقولش «متوفر» على أي حاجة (يروح للموظف)
        self.assertEqual(quick_catalog_reply("عندك مساعد خلفي f30"), "")

    def test_model_only_question_still_answers(self):
        self.assertIn("F30", quick_catalog_reply("عندك خلفي f30"))
