"""
🇪🇬 كشف الأعطال لكل العربيات مش BMW بس.

🐛 الماركات المنتشرة في مصر (شيري، MG، BYD، جيلي، شيفروليه، أوبل، رينو، بيجو، سوزوكي، ميتسوبيشي…)
ماكانتش بتتعرف من رقم الشاسيه، ولو اتعرفت من غير قراءات خاصة كانت بترجع «مفيش DIDs» وماتقراش
حتى القراءات العامة. دلوقتي بتتعرف وبتنزل للقراءات العامة (OBD2) — أكواد الأعطال شغّالة على أي عربية.
"""
import json
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.test import SimpleTestCase

JS = Path(settings.BASE_DIR) / 'smart_diagnostics/static/smart_diagnostics/js'


@skipUnless(shutil.which('node'), 'node مش متسطّب')
class EgyptMakesDetectionTests(SimpleTestCase):
    def detect(self, vins):
        src = (JS / 'obd_wifi.js').read_text(encoding='utf-8')
        i = src.index('function detectManufacturerFromVIN')
        j = src.index('\n}\n', i) + 2
        script = src[i:j] + f'\nconsole.log(JSON.stringify({json.dumps(vins)}.map(detectManufacturerFromVIN)));'
        out = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)

    def test_common_egypt_makes_are_recognized(self):
        vins = {'LVVDB11B5KD000001': 'chery', 'LSJW74U31MZ000001': 'mg', 'LGXC16DF0K0000001': 'byd',
                'L6T7844Z0KN000001': 'geely', 'KL1JA6E09LB000001': 'gm', 'W0L0SDL68B4000001': 'opel',
                'VF1RFB00X56000001': 'renault', 'VF3LCYHZPHS000001': 'peugeot', 'MA3FB32S0K0000001': 'suzuki',
                'JMBXTGF2WEZ000001': 'mitsubishi', 'JN1TANT31U0000001': 'nissan', 'W1K2050461F000001': 'mercedes'}
        self.assertEqual(self.detect(list(vins)), list(vins.values()))


class GenericFallbackTests(SimpleTestCase):
    def test_brands_without_their_own_library_fall_back_to_generic(self):
        for name in ('obd_wifi.js', 'obd_bluetooth.js'):
            src = (JS / name).read_text(encoding='utf-8')
            self.assertRegex(src, r'\.generic \|\| \[\]', name)
