"""لينت القيم جوه JavaScript في القوالب (scripts/lint_templates.py).

Django بيطبع الأرقام بتنسيق لغة الواجهة: بالعربي 30.0444 ⇒ «30,0444» وبالإنجليزي
1234 ⇒ «1,234». جوه <script> ده كسر صفحة الحضور (زرار «تسجيل حضور» ميت) وصفحة صرف
العمولات وتقارير المطبعة، وجوه onclick بعت رقم فاتورة غلط — اللينت بيمنع رجوعه.
"""
import sys
from pathlib import Path

from django.conf import settings
from django.template import Context, Template
from django.test import SimpleTestCase
from django.utils import translation

sys.path.insert(0, str(Path(settings.BASE_DIR) / 'scripts'))
import lint_templates as lint  # noqa: E402


class JsNumberLintTests(SimpleTestCase):

    def hits(self, html):
        return [expr for _, expr in lint.find_unsafe_js_values(html)]

    def test_flags_numbers_in_script_code_and_onclick(self):
        self.assertEqual(self.hits('<script>const LAT = {{ lat }};</script>'), ['lat'])
        self.assertEqual(self.hits('<button onclick="openPay({{ inv.id }}, \'x\')">'), ['inv.id'])
        self.assertEqual(self.hits('<script>var n = {{ count|default:0 }};</script>'), ['count|default:0'])

    def test_ignores_strings_comments_template_literals_and_safe_filters(self):
        html = """<script>
          const a = '{{ name }}', b = "{{ title }}";
          // {{ in_comment }}
          /* {{ block_comment }} */
          const re = s.replace(/"/g, '&quot;');
          const t = `${x} {{ currency }} ${y ?? '?'}`;
          const ok = {{ lat|unlocalize }} + {{ n|stringformat:'d' }};
          const flag = {{ on|yesno:"true,false" }};
          const data = {{ payload|safe }};
        </script>
        <script src="/static/x.js">{{ ignored }}</script>"""
        self.assertEqual(self.hits(html), [])

    def test_flags_unescaped_values_in_event_attributes(self):
        found = [e for _, e in lint.find_unescaped_attr_values(
            '<button onclick="openSub(\'{{ t.name }}\', {{ t.id|unlocalize }}, \'{{ t.plan|escapejs }}\')">')]
        self.assertEqual(found, ['t.name'])

    def test_escapejs_keeps_a_hostile_name_inside_the_string(self):
        html = Template('<b onclick="f(\'{{ n|escapejs }}\')">').render(Context({'n': "x');alert(1);//"}))
        self.assertNotIn("');", html)

    def test_unlocalize_is_available_in_every_template(self):
        tpl = Template('{{ v|unlocalize }}|{{ n|unlocalize }}')
        for lang in ('ar', 'en'):
            with translation.override(lang):
                self.assertEqual(tpl.render(Context({'v': 30.0444196, 'n': 1234})), '30.0444196|1234')

    def test_repository_templates_are_clean(self):
        root = Path(settings.BASE_DIR)
        bad = []
        for tpl in root.rglob('*.html'):
            if any(p in tpl.parts for p in ('.git', 'site-packages', 'node_modules', 'staticfiles')):
                continue
            text = tpl.read_text(encoding='utf-8')
            for line, expr in lint.find_unsafe_js_values(text) + lint.find_unescaped_attr_values(text):
                bad.append(f'{tpl.relative_to(root)}:{line} {{{{ {expr} }}}}')
        self.assertEqual(bad, [])


class TransInsideJsStringLintTests(SimpleTestCase):
    """ترجمة فيها ' جوه '…' في JS بتوقّف السكريبت كله بالإنجليزي (نقطة البيع
    وفاتورة الشراء كانوا ميتين: «You've reached…» / «doesn't change…»)."""

    TR = {'وصلت الحد': ["You've reached the limit"], 'تمام': ['Done']}

    def hits(self, html):
        return [msg for _, msg in lint.find_trans_breaking_js(html, self.TR)]

    def test_flags_translation_that_closes_the_string(self):
        self.assertEqual(self.hits("<script>flash('{% trans \"وصلت الحد\" %}');</script>"),
                         ["You've reached the limit"])

    def test_safe_cases(self):
        html = """<script>
          flash('{% filter escapejs %}{% trans "وصلت الحد" %}{% endfilter %}');
          flash("{% trans "وصلت الحد" %}");
          flash('{% trans "تمام" %}');
        </script>"""
        self.assertEqual(self.hits(html), [])

    def test_escapejs_filter_keeps_the_apostrophe_inside_the_string(self):
        out = Template("{% filter escapejs %}You've{% endfilter %}").render(Context())
        self.assertNotIn("'", out)

    def test_repository_templates_are_clean(self):
        root = Path(settings.BASE_DIR)
        translations = lint.load_translations(root)
        self.assertGreater(len(translations), 1000)
        bad = []
        for tpl in root.rglob('*.html'):
            if any(p in tpl.parts for p in ('.git', 'site-packages', 'node_modules', 'staticfiles')):
                continue
            for line, msg in lint.find_trans_breaking_js(tpl.read_text(encoding='utf-8'), translations):
                bad.append(f'{tpl.relative_to(root)}:{line} {msg}')
        self.assertEqual(bad, [])
