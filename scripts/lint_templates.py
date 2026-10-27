#!/usr/bin/env python3
"""🧪 لينت قوالب خفيف — يمسك أخطاء بتعدّي على Django بس بتظهر للعميل.

بيشتغل من غير Django/DB (بايثون عادي) عشان ينفع في CI بسرعة.

بيمسك تعليق {# ... #} متعدد الأسطر — Django بيدعمه على سطر واحد بس، فأي تعليق
بيمتد لأكتر من سطر بيتطبع كنص حرفي في الصفحة (ده الباج اللي كسر صفحة الدخول).
الحل: {% comment %} ... {% endcomment %}.

(توازن وسوم البلوك بيغطيه أمر Django `check_templates` اللي بيصنّف كل قالب فعلاً،
فمابنكرّروش هنا عشان نتجنّب الـ false positives.)

الاستخدام: python scripts/lint_templates.py [جذر]  (الافتراضي: مجلد الشغل)
الخروج: 0 لو سليم، 1 لو فيه مشاكل.
"""
import ast
import re
import sys
from pathlib import Path

# فلاتر بتطلّع نص مش متأثر بلغة الواجهة (أي stringfilter بيعمل str() عادي).
_JS_SAFE_FILTERS = re.compile(
    r'\|\s*(unlocalize|stringformat|escapejs|json_script|safe|yesno|lower|upper|date|time|urlencode)\b')
_SCRIPT_RE = re.compile(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', re.S | re.I)
_ON_ATTR_RE = re.compile(r'\son[a-z]+="([^"]*)"', re.I)


def _js_code_expressions(js, tags=None):
    """يرجّع (offset, expr) لكل {{ expr }} واقع في «كود» جافاسكريبت — مش جوه
    نص '…' / "…" / `…` ولا تعليق. {% … %} بيتنط عليه — ولو `tags` قائمة بيتضاف
    فيها (offset, محتوى الوسم, الحالة) والحالة sq/dq/tpl معناها جوه نص JS."""
    out, i, n = [], 0, len(js)
    state, stack = 'code', []          # stack: عمق الأقواس جوه ${ … } في `…`
    while i < n:
        if js.startswith('{{', i):
            j = js.find('}}', i + 2)
            if j == -1:
                break
            if state == 'code':
                out.append((i, js[i + 2:j].strip()))
            i = j + 2
            continue
        if js.startswith('{%', i):
            j = js.find('%}', i + 2)
            if tags is not None and j != -1:
                tags.append((i, js[i + 2:j].strip(), state))
            i = n if j == -1 else j + 2
            continue
        c = js[i]
        if state == 'code':
            if c in '\'"`':
                state = {'\'': 'sq', '"': 'dq', '`': 'tpl'}[c]
            elif js.startswith('//', i):
                state = 'line'
            elif js.startswith('/*', i):
                state = 'block'
            elif c == '/' and (js[:i].rstrip()[-1:] or '(') in '(,=:[!&|?{};+-*%<>~^':
                # regex literal (/"/g …) — بنتنط عليه عشان علامات التنصيص اللي جواه
                j, in_class = i + 1, False
                while j < n and js[j] != '\n':
                    if js[j] == '\\':
                        j += 1
                    elif js[j] == '[':
                        in_class = True
                    elif js[j] == ']':
                        in_class = False
                    elif js[j] == '/' and not in_class:
                        break
                    j += 1
                i = j
            elif c == '{' and stack:
                stack[-1] += 1
            elif c == '}' and stack:
                if stack[-1] == 0:
                    stack.pop()
                    state = 'tpl'
                else:
                    stack[-1] -= 1
        elif state in ('sq', 'dq'):
            if c == '\\':
                i += 1
            elif c == '\n' or c == {'sq': '\'', 'dq': '"'}[state]:
                state = 'code'
        elif state == 'tpl':
            if c == '\\':
                i += 1
            elif c == '`':
                state = 'code'
            elif js.startswith('${', i):
                stack.append(0)
                state = 'code'
                i += 1
        elif state == 'line':
            if c == '\n':
                state = 'code'
        elif state == 'block':
            if js.startswith('*/', i):
                state = 'code'
                i += 1
        i += 1
    return out


def find_unsafe_js_values(text):
    """أرقام/قيم مطبوعة جوه كود JS من غير فلتر يمنع تنسيق اللغة.

    Django بيطبع الأرقام بتنسيق لغة الواجهة: بالعربي 1234.5 ⇒ «1234,5»، وبالإنجليزي
    1234 ⇒ «1,234». جوه <script> أو onclick ده بيكسر الصفحة (SyntaxError — زي صفحة
    الحضور اللي زرار «تسجيل حضور» فيها كان ميت بالعربي) أو بيبعت قيمة غلط بصمت
    (openPay(1,234) = فاتورة #1). الحل: |unlocalize (أو |stringformat / |escapejs).
    """
    issues = []
    for rx in (_SCRIPT_RE, _ON_ATTR_RE):
        for m in rx.finditer(text):
            for off, expr in _js_code_expressions(m.group(1)):
                if _JS_SAFE_FILTERS.search(expr):
                    continue
                line = text.count('\n', 0, m.start(1) + off) + 1
                issues.append((line, expr))
    return issues


_ATTR_SAFE_FILTERS = re.compile(r'\|\s*(escapejs|unlocalize|stringformat|yesno)\b')


def find_unescaped_attr_values(text):
    """قيم جوه onclick/onchange… من غير |escapejs.

    الـ autoescape بيحوّل ' لـ &#x27; — بس المتصفح بيرجّعها ' قبل ما يشغّل الـ JS
    بتاع الـ attribute، فاسم زي «x');alert(1);//» بيتنفّذ كود. ده كان شغّال على
    لوحة صاحب المنصة: اسم شركة من التسجيل العام بيتنفّذ لما السوبر أدمن يدوس زرار.
    """
    issues = []
    for m in _ON_ATTR_RE.finditer(text):
        for v in re.finditer(r'{{\s*(.*?)\s*}}', m.group(1)):
            if not _ATTR_SAFE_FILTERS.search(v.group(1)):
                issues.append((text.count('\n', 0, m.start(1) + v.start()) + 1, v.group(1)))
    return issues


_TRANS_TAG = re.compile(r'''^(?:trans|translate)\s+(["'])(.*?)\1\s*(?:noop\s*)?$''', re.S)
_QUOTE_OF = {'sq': "'", 'dq': '"', 'tpl': '`'}


def load_translations(root):
    """كل الترجمات {msgid: msgstr} من locale/*/LC_MESSAGES/django.po."""
    out = {}
    for po in Path(root).glob('locale/*/LC_MESSAGES/django.po'):
        text = po.read_text(encoding='utf-8')
        for m in re.finditer(r'^msgid ((?:".*"\n)+)msgstr ((?:".*"\n)+)', text, re.M):
            msgid, msgstr = (''.join(ast.literal_eval(x) for x in g.strip().split('\n'))
                             for g in m.groups())
            if msgid and msgstr:
                out.setdefault(msgid, []).append(msgstr)
    return out


def find_trans_breaking_js(text, translations):
    """{% trans "…" %} جوه نص JS والترجمة (أو الأصل) فيها نفس علامة التنصيص.

    Django مابيعملش escape لترجمة نص ثابت، فـ «You've reached…» جوه '…' بتقفل
    النص ⇒ SyntaxError والسكريبت كله بيقف — نقطة البيع وفاتورة الشراء كانوا ميتين
    بالإنجليزي كده. الحل: {% filter escapejs %}{% trans "…" %}{% endfilter %}.
    """
    issues = []
    for m in _SCRIPT_RE.finditer(text):
        tags = []
        _js_code_expressions(m.group(1), tags)
        escaped = 0
        for off, tag, state in tags:
            if tag.startswith('filter') and 'escapejs' in tag:
                escaped += 1
            elif tag == 'endfilter':
                escaped = max(escaped - 1, 0)
            if escaped or state not in _QUOTE_OF:
                continue
            tm = _TRANS_TAG.match(tag)
            if not tm:
                continue
            quote = _QUOTE_OF[state]
            for candidate in [tm.group(2), *translations.get(tm.group(2), [])]:
                if quote in candidate or '\\' in candidate or '\n' in candidate:
                    line = text.count('\n', 0, m.start(1) + off) + 1
                    issues.append((line, candidate[:60]))
                    break
    return issues


def find_multiline_comments(lines):
    """يرجّع أرقام أسطر فيها {# من غير #} على نفس السطر بس ليها #} في سطر بعدين
    (يعني تعليق متعدد الأسطر مكسور). بيتجاهل {# اللي مالهاش #} خالص (زي CSS)."""
    issues = []
    for i, line in enumerate(lines):
        idx = line.find('{#')
        if idx == -1:
            continue
        if '#}' in line[idx + 2:]:
            continue  # تعليق سطر واحد سليم
        # دوّر على #} في الأسطر اللي بعده (لحد 20 سطر) — لو موجود فهو تعليق مكسور
        for j in range(i + 1, min(i + 21, len(lines))):
            if '#}' in lines[j]:
                issues.append((i + 1, j + 1, line.strip()[:70]))
                break
    return issues


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else '.')
    templates = sorted(root.rglob('*.html'))
    translations = load_translations(root)
    problems = 0
    checked = 0
    for tpl in templates:
        # نتجاهل ملفات مكتبات الطرف الثالث جوه venv/site-packages
        if 'site-packages' in tpl.parts or 'node_modules' in tpl.parts:
            continue
        try:
            text = tpl.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        lines = text.splitlines()
        checked += 1
        for start, end, snippet in find_multiline_comments(lines):
            problems += 1
            print(f"❌ {tpl}:{start} تعليق {{# … #}} متعدد الأسطر (بينتهي سطر {end}) "
                  f"— هيتطبع كنص! استخدم {{% comment %}}. → {snippet}")
        for line, expr in find_unsafe_js_values(text):
            problems += 1
            print(f"❌ {tpl}:{line} {{{{ {expr} }}}} جوه كود JS من غير |unlocalize — بالعربي "
                  f"1234.5 بيتطبع «1234,5» وبالإنجليزي 1234 «1,234» (SyntaxError أو قيمة غلط).")
        for line, msg in find_trans_breaking_js(text, translations):
            problems += 1
            print(f"❌ {tpl}:{line} ترجمة فيها علامة تنصيص جوه نص JS («{msg}») — بتقفل النص "
                  f"وتوقّف السكريبت كله. لفّها في {{% filter escapejs %}}…{{% endfilter %}}.")
        for line, expr in find_unescaped_attr_values(text):
            problems += 1
            print(f"❌ {tpl}:{line} {{{{ {expr} }}}} جوه onclick/on… من غير |escapejs — "
                  f"المتصفح بيفك الـ &#x27; فـ ' في الاسم بتقفل النص وتشغّل كود (XSS).")

    print(f"\n🧪 اتفحص {checked} قالب — مشاكل: {problems}")
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
