"""🔐 خانات ربط Meta (فيسبوك/إنستجرام/واتساب) في الإعدادات — حماية من «الملء التلقائي» للمتصفح.

🐛 [FIX]: خانة الـ Access Token نوعها password، فـ Chrome كان فاكرها صفحة تسجيل دخول وبيحط
إيميل صاحب الحساب في الخانة اللي قبلها (Instagram Account ID) وباسورده في خانة التوكن —
والحفظ بيعدّي، فالربط بيبوظ من غير ما صاحب الشركة يعرف ليه (والباسورد بتاعه يتخزّن كتوكن).
دلوقتي: الخانات بتقول للمتصفح مايملاهاش، والـ IDs لازم أرقام والتوكن لازم شكل توكن Meta.
"""
from __future__ import annotations

import re

from django import forms

SECRET_ATTRS = {"autocomplete": "new-password", "data-lpignore": "true", "data-1p-ignore": "true",
                "data-form-type": "other", "spellcheck": "false", "dir": "ltr"}
ID_ATTRS = {"autocomplete": "off", "data-lpignore": "true", "data-1p-ignore": "true",
            "data-form-type": "other", "inputmode": "numeric", "dir": "ltr", "spellcheck": "false"}

_AUTOFILL_HINT = "لو ظهر فيها إيميلك أو كلمة السر بتاعتك يبقى المتصفح ملاها لوحده — امسحها."


def secret_widget():
    return forms.PasswordInput(render_value=False, attrs=dict(SECRET_ATTRS))


def guard_id_widgets(form, *names):
    for name in names:
        if name in form.fields:
            form.fields[name].widget.attrs.update(ID_ATTRS)


def clean_numeric_id(value, label, *, allow_prefix=""):
    """رقم صفحة/حساب من Meta: أرقام بس (و act_ قبل رقم حساب الإعلانات)."""
    value = (value or "").strip().replace(" ", "")
    if not value:
        return value
    digits = value[len(allow_prefix):] if allow_prefix and value.lower().startswith(allow_prefix) else value
    if not digits.isdigit() or len(digits) < 5:
        raise forms.ValidationError(f"{label} أرقام بس (زي 105196610999164). {_AUTOFILL_HINT}")
    return value


def clean_meta_token(value):
    """توكن Meta بيبدأ بـ EAA وطويل (100+ حرف) ومن غير مسافات — أي حاجة تانية غالباً باسورد اتملى لوحده."""
    value = (value or "").strip()
    if not value:
        return value
    if not value.startswith("EAA") or len(value) < 60 or re.search(r"\s|@", value):
        raise forms.ValidationError(
            "ده مش Access Token — التوكن بتاع Meta بيبدأ بـ EAA وطويل جداً (انسخه من Meta for Developers "
            f"أو Business Settings ← System users). {_AUTOFILL_HINT}")
    return value


def clean_app_secret(value):
    value = (value or "").strip()
    if value and not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise forms.ValidationError(f"App Secret بيبقى 32 حرف/رقم (من App settings ← Basic). {_AUTOFILL_HINT}")
    return value
