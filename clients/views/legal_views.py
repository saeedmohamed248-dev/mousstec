"""
⚖️ سياسة الخصوصية والشروط والأحكام
=====================================
🐛 [FIX]: الموقع ماكانش فيه سياسة خصوصية ولا شروط استخدام — مطلوبين قانونياً (قانون حماية البيانات
الشخصية 151/2020) ومن Paymob وMeta. نسخة عربي ونسخة إنجليزي (نص قانوني طويل — أوضح من تقسيمه
لسطور ترجمة)، وبتتعرض حسب لغة الموقع أو ?lang=.
"""
from django.shortcuts import render
from django.utils import translation


def _legal(request, name):
    lang = request.GET.get('lang')
    if lang not in ('ar', 'en'):
        lang = 'en' if (translation.get_language() or '').startswith('en') else 'ar'
    with translation.override(lang):
        return render(request, f'clients/legal/{name}_{lang}.html')


def privacy(request):
    return _legal(request, 'privacy')


def terms(request):
    return _legal(request, 'terms')
