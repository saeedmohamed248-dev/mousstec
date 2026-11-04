"""🌐 رسايل السيارات (الخزن، التحصيل، المرتجعات…) كانت عربي ثابت في الكود —
اللي شغّال بالإنجليزي كان بيشوف أخطاء عربي. دلوقتي بتتترجم حسب لغة المستخدم.
"""
import polib
from django.conf import settings
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
        po = polib.pofile(str(settings.BASE_DIR / 'locale/en/LC_MESSAGES/django.po'))
        bad = [e.msgid for e in po if e.msgstr and not e.obsolete
               and sorted(ph.findall(e.msgid)) != sorted(ph.findall(e.msgstr))]
        self.assertEqual(bad, [])
