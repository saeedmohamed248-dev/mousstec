"""🔁 مراجعة دورة الاشتراك بالتحويل (إعلان ← أسعار ← تسجيل ← دفع ← إيصال ← تفعيل).

اللي اتصلّح:
- صفحة رفع الإيصال لمن اختار إنستاباي كانت بتعرض كارت «VODAFONE CASH» هو الأساسي.
- «تمام» بعد رفع إيصال الاشتراك كان بيرجّع الشركة للصفحة الرئيسية للموقع بدل حسابها.
- «الكود X هيتطبّق على أول دفعة» كانت بتظهر في التسجيل حتى لكود غلط أو خلص.
"""

from django.test import Client as HttpClient, RequestFactory, TestCase, override_settings

from billing.tests.test_coupons_and_referrals import _NoSchemaMixin, _coupon, _shop
from billing.tests.test_promotions_and_plan_admin import _plan
from clients.models import Coupon, Domain, ManualPaymentReceipt
from clients.services import platform_billing as pb
from clients.views.manual_payment_views import _after_payment_url, _back_url
from marketplace_c2c.tests.test_parts_order_lifecycle import _PublicDomainMixin


def _receipt(shop, plan, method):
    inv = pb.open_plan_change_invoice(shop, plan, 3, provider=method)
    return ManualPaymentReceipt.objects.create(purchase_type='subscription', purchase_id=inv.pk, amount=inv.total,
                                               payment_method=method, tenant=shop)


@override_settings(ALLOWED_HOSTS=['*'])
class ReceiptUploadPageTests(_NoSchemaMixin, _PublicDomainMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.silver = _plan('auto-silver', '550')
        self.shop = _shop('rcpt_shop')
        Domain.objects.create(domain='rcpt-shop.mousstec.test', tenant=self.shop, is_primary=True)

    def test_instapay_receipt_shows_the_instapay_address_first(self):
        r = _receipt(self.shop, self.silver, 'instapay')
        html = HttpClient().get(f'/payment/manual/upload/{r.receipt_code}/').content.decode()
        self.assertIn('INSTAPAY', html)
        self.assertNotIn('VODAFONE CASH', html)                                   # مش هو الكارت الأساسي
        self.assertIn('أو ادفع بفودافون كاش', html)
        self.assertLess(html.index('saeed.mohamed.123@instapay'), html.index('01094850763'))

    def test_vodafone_receipt_keeps_vodafone_first(self):
        r = _receipt(self.shop, self.silver, 'vodafone_cash')
        html = HttpClient().get(f'/payment/manual/upload/{r.receipt_code}/').content.decode()
        self.assertIn('VODAFONE CASH', html)
        self.assertIn('أو ادفع عبر InstaPay', html)

    def test_after_upload_the_company_goes_back_to_its_own_account(self):
        r = _receipt(self.shop, self.silver, 'instapay')
        req = RequestFactory().get('/', secure=True, HTTP_HOST='mousstec.test')
        self.assertEqual(_back_url(r, req), 'https://rcpt-shop.mousstec.test/')
        self.assertEqual(_after_payment_url(r, req), 'https://rcpt-shop.mousstec.test/')
        self.assertEqual(_after_payment_url(r), 'https://rcpt-shop.mousstec.test/')


@override_settings(ALLOWED_HOSTS=['*'])
class SignupCouponNoteTests(_PublicDomainMixin, TestCase):

    def tearDown(self):
        Coupon.objects.all().delete()

    def test_note_only_for_a_live_code(self):
        _coupon('LIVE50', percent_off=50)
        live = HttpClient().get('/connect/signup/', {'coupon': 'LIVE50'}).content.decode()
        self.assertIn('هيتطبّق على أول دفعة', live)
        dead = HttpClient().get('/connect/signup/', {'coupon': 'NOSUCH'}).content.decode()
        self.assertNotIn('هيتطبّق على أول دفعة', dead)
        self.assertIn('name="ref" value="NOSUCH"', dead)                         # بيتحفظ بهدوء زي الأول
