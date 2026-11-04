"""🌐 رسايل السيارات (الخزن، التحصيل، المرتجعات…) كانت عربي ثابت في الكود —
اللي شغّال بالإنجليزي كان بيشوف أخطاء عربي. دلوقتي بتتترجم حسب لغة المستخدم.
"""
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from django.utils import translation

from inventory.services import customer_payments, vendor_payments


class AutomotiveMessagesI18nTests(SimpleTestCase):

    def _msg(self, fn, lang):
        with translation.override(lang):
            with self.assertRaises(ValidationError) as cm:
                fn('NaN')
        return cm.exception.messages[0]

    def test_payment_errors_follow_the_language(self):
        for mod in (vendor_payments, customer_payments):
            self.assertEqual(self._msg(mod._q, 'ar'), 'المبلغ لازم يبقى رقم صحيح.')
            self.assertEqual(self._msg(mod._q, 'en'), 'The amount must be a valid number.')

    def test_formatted_messages_keep_their_placeholders(self):
        with translation.override('en'):
            msg = translation.gettext(
                'رصيد خزنة «%(name)s» مش كفاية (متاح: %(bal)s).') % {'name': 'Main', 'bal': '10.00'}
        self.assertEqual(msg, 'Treasury “Main” doesn\'t have enough (available: 10.00).')

    def test_every_english_entry_keeps_the_same_placeholders(self):
        """أي ترجمة ناقص/زيادة فيها %(x)s بتوقع الصفحة بـ KeyError وقت التشغيل."""
        import re
        ph = re.compile(r'%\(\w+\)[sdf]|%\(\w+\)\.\d+f')
        from django.utils.translation import trans_real
        catalog = trans_real.translation('en')._catalog
        bad = [k for k, v in catalog.items() if isinstance(k, str) and isinstance(v, str) and v
               and sorted(ph.findall(k)) != sorted(ph.findall(v))]
        self.assertEqual(bad, [])


class PrintingMessagesI18nTests(SimpleTestCase):
    """🖨️ رسايل المطابع (عروض الأسعار، الخزائن، الشيكات) بتتترجم برضه."""

    def test_printing_messages_are_translated(self):
        with translation.override('en'):
            self.assertEqual(translation.gettext('العرض لازم يبقى مقبول الأول'),
                             'The quotation must be accepted first')
            self.assertEqual(
                translation.gettext('مينفعش تمسح التسوية: الكمية اللي زادت اتصرف منها (المتاح %(qty)s).') % {'qty': 3},
                "This adjustment can't be deleted: some of the added quantity has been used (available 3).")
