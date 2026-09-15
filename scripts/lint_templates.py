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
import sys
from pathlib import Path


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
    problems = 0
    checked = 0
    for tpl in templates:
        # نتجاهل ملفات مكتبات الطرف الثالث جوه venv/site-packages
        if 'site-packages' in tpl.parts or 'node_modules' in tpl.parts:
            continue
        try:
            lines = tpl.read_text(encoding='utf-8').splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        checked += 1
        for start, end, snippet in find_multiline_comments(lines):
            problems += 1
            print(f"❌ {tpl}:{start} تعليق {{# … #}} متعدد الأسطر (بينتهي سطر {end}) "
                  f"— هيتطبع كنص! استخدم {{% comment %}}. → {snippet}")

    print(f"\n🧪 اتفحص {checked} قالب — مشاكل: {problems}")
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
