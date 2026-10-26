"""The robot finds the part inside a spoken question and says its RETAIL price.

`find_product` only matched when the whole sentence was a part number or sat
inside a product name, so «عندك فلتر زيت؟» found nothing, the LLM fallback
answered (it may never quote prices) and the robot never said a price. Runs
on a tenant schema like the inventory suite.
"""

from decimal import Decimal
from unittest import mock

from inventory.models import Branch
from inventory.tests.base import ERPTenantTestCase
from inventory.tests.factories import make_branch, make_inventory, make_product
from robot import services, views
from robot.models import RobotDevice


class SpokenPriceTests(ERPTenantTestCase):

    def setUp(self):
        self.branch = Branch.objects.order_by("id").first() or make_branch(name="الرئيسي")
        self.device = RobotDevice(name="موس", device_uid="esp-price-test", branch=self.branch)
        self.device.issue_token()
        self.device.save()
        self.oil = make_product(name="فلتر زيت", part_number="11427953125",
                                retail_price="450.50", b2b_wholesale_price=Decimal("321.00"))
        make_inventory(self.oil, self.branch, quantity=3)
        self.pump_e90 = make_product(name="طرمبة مية", car_model="E90", retail_price="1800")
        self.pump_f30 = make_product(name="طرمبة مية", car_model="F30", retail_price="2100")
        make_inventory(self.pump_e90, self.branch, quantity=1)
        make_inventory(self.pump_f30, self.branch, quantity=1)

    def _say(self, text):
        with mock.patch.object(services, "ai_reply", return_value="") as ai:
            intent, reply, payload = views._handle_voice(text, self.device, None)
        return intent, reply, payload, ai

    def test_a_natural_question_gets_the_retail_price(self):
        _, reply, payload, ai = self._say("عندك فلتر زيت؟")
        self.assertEqual(reply, "أيوه، فلتر زيت موجود، عندي منه 3 حتت، بسعر 450 جنيه و50 قرش.")
        self.assertEqual(payload["product"]["id"], self.oil.pk)
        ai.assert_not_called()                       # the LLM never answers a priced part
        self.assertNotIn("321", reply)               # wholesale never

    def test_a_word_in_one_long_name_is_asked_about_not_priced(self):
        # 🐛 In the shop: «الزيت» answered «خرطوم علوي من موتور لمبرد الزيت … بسعر 567 جنيه»,
        # the first part with «الزيت» in its name, said with a price as if it were asked for.
        hose = make_product(name="خرطوم علوي من موتور لمبرد الزيت F25 N20", retail_price="567")
        make_inventory(hose, self.branch, quantity=1)
        self.assertIsNone(services.find_product("الزيت"))
        self.assertIsNone(services.find_product("طرمبة"))           # two pumps: never the first one
        self.assertEqual(services.find_product("فلتر زيت"), self.oil)  # its whole name
        self.assertEqual(services.find_product("خرطوم علوي من موتور لمبرد"), hose)   # most of it
        intent, reply, payload, _ = self._say("الزيت")
        self.assertEqual(intent, "inventory_query")
        self.assertNotIn("567", reply)                               # no price for a part not asked for
        self.assertIn("choices", payload)

    def test_vosks_news_arabic_spelling_still_finds_it(self):
        # 🐛 [FIX]: Vosk heard «عندك فلتر زيت» as «عندك فلترا زيت»: only «زيت» matched.
        oil_pan = make_product(name="كرتيرة زيت", retail_price="900")
        make_inventory(oil_pan, self.branch, quantity=1)
        _, reply, payload, _ = self._say("عندك فلترا زيت")
        self.assertEqual(payload["product"]["id"], self.oil.pk, reply)
        self.assertIn("بسعر 450 جنيه و50 قرش", reply)

    def test_the_shops_own_words_for_a_part(self):
        # «كاوتش» is the catalogue's «إطار», «بوجيهات» its «شمعة», «بريك باد» its «تيل».
        tire = make_product(name="إطار ميشلان", retail_price="2500")
        plug = make_product(name="شمعة احتراق", retail_price="120")
        pads = make_product(name="تيل فرامل أمامي", retail_price="700")
        for p in (tire, plug, pads):
            make_inventory(p, self.branch, quantity=4)
        for said, product in (("عندك كاوتش؟", tire), ("عامل كام البوجيهات يا باشا", plug),
                              ("عايز بريك باد امامي يا هندسة", pads), ("فيه كاوتشات ميشلان؟", tire)):
            with self.subTest(said):
                _, reply, payload, ai = self._say(said)
                self.assertEqual(payload["product"]["id"], product.pk, reply)
                ai.assert_not_called()

    def test_the_shops_words_dont_pick_one_of_two(self):
        make_product(name="إطار ميشلان", retail_price="2500")
        make_product(name="إطار بريدجستون", retail_price="2300")
        _, reply, payload, _ = self._say("بكام الكاوتش")
        self.assertIn("تقصد أنهي", reply)                # two tires: asked, never a guessed price
        self.assertNotIn("جنيه", reply)

    def test_the_way_a_shop_asks_isnt_a_part(self):
        # «يا هندسة», «عامل» aren't in any part's name: the two pumps are still asked about.
        _, reply, payload, _ = self._say("عامل كام الطرمبة يا هندسة")
        self.assertEqual(payload["choices"], ["طرمبة مية (E90)", "طرمبة مية (F30)"])

    def test_a_part_number_inside_the_sentence(self):
        _, reply, _, _ = self._say("بكام 11427953125 لو سمحت")
        self.assertIn("بسعر 450 جنيه و50 قرش", reply)

    def test_a_part_number_said_in_groups(self):
        _, reply, _, _ = self._say("بكام ١١ ٤٢ ٧ ٩٥٣ ١٢٥")
        self.assertIn("فلتر زيت", reply)

    def test_the_car_model_picks_between_same_name_parts(self):
        # «طرمبه ميه» as speech-to-text writes it, «طرمبة مية» in the catalogue.
        _, reply, payload, _ = self._say("عندك طرمبه ميه E90؟")
        self.assertEqual(payload["product"]["id"], self.pump_e90.pk)
        self.assertIn("بسعر 1800 جنيه", reply)

    def test_two_parts_that_fit_equally_are_asked_about(self):
        _, reply, payload, ai = self._say("بكام الطرمبة")
        self.assertEqual(payload["choices"], ["طرمبة مية (E90)", "طرمبة مية (F30)"])
        self.assertIn("تقصد أنهي", reply)
        self.assertNotIn("جنيه", reply)              # no price for a guess
        ai.assert_not_called()

    def test_out_of_stock_still_says_the_price(self):
        belt = make_product(name="سير مكنة", retail_price="300")
        make_inventory(belt, self.branch, quantity=0)
        _, reply, _, _ = self._say("عندك سير المكنة")
        self.assertEqual(reply, "سير مكنة مش موجود هنا دلوقتي، وسعره 300 جنيه. "
                                "لو تحب أبلغك أول ما ييجي، قولي رقم موبايلك.")

    def test_a_part_without_a_price_never_says_zero(self):
        cap = make_product(name="غطا رديتر", retail_price="0")
        make_inventory(cap, self.branch, quantity=2)
        _, reply, _, _ = self._say("عندك غطا رديتر")
        self.assertNotIn("0 جنيه", reply)
        self.assertIn("سعره مش متسجل", reply)

    def test_a_general_question_still_goes_to_the_llm(self):
        _, _, payload, ai = self._say("الساعة كام دلوقتي")
        ai.assert_called_once()
        self.assertTrue(payload["unresolved"])
