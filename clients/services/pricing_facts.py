"""
💰 الأسعار والباقات كما هي في الداتابيز — مصدر واحد لكل الكلام اللي بيتقال للعميل
=================================================================================
🐛 [FIX]: بوت الموقع (Gemini + الردود المحلية) كان بيقول أسعار قديمة مكتوبة بإيدينا
(سيلفر 780، جولد 1,250، Empire 1,800، خصومات 9/12.5/25%، تجربة 7 أيام، تصميمات 99-369)
— والعميل يتحاسب على 550/850/2,500 وخصومات 10/15/20% وتجربة 14 يوم. أي تعديل من لوحة
المالك دلوقتي بيوصل للبوت على طول.
"""
from __future__ import annotations

from decimal import Decimal


def _num(v) -> str:
    v = Decimal(str(v or 0))
    return f"{v:,.0f}" if v == v.to_integral_value() else f"{v:,.2f}"


def pricing_facts() -> dict:
    from clients.models import Client, DesignPackage, DiagnosticsTopUpPack, Plan
    from clients.models.tenancy import TRIAL_DAYS
    plans = list(Plan.objects.filter(is_active=True).order_by('industry', 'sort_order'))
    by_industry = {'automotive': [], 'printing': []}
    for p in plans:
        by_industry.setdefault(p.industry, []).append({
            'name': p.name, 'price': p.monthly_price, 'users': p.max_users,
            'branches': p.max_branches, 'treasuries': p.max_treasuries,
            'features': list(p.features or [])[:6],
        })
    sample = plans[0] if plans else None
    packs = list(DesignPackage.objects.filter(is_active=True).order_by('target_audience', 'sort_order'))
    try:
        from clients.views.subscription_views import paymob_configured
        card = paymob_configured()
    except Exception:
        card = False
    return {
        'trial_days': TRIAL_DAYS,
        'discounts': ({'quarterly': sample.quarterly_discount, 'semi_annual': sample.semi_annual_discount,
                       'annual': sample.annual_discount} if sample else {}),
        'plans': by_industry,
        'addon_price': Client.ADDON_PRICE_PER_MONTH,
        'design_customer': [(d.designs_count, d.price_egp) for d in packs if d.target_audience == 'customer'],
        'design_designer': [(d.designs_count, d.price_egp) for d in packs if d.target_audience == 'designer'],
        'diag_topup': [(t.uses_granted, t.price_egp) for t in
                       DiagnosticsTopUpPack.objects.filter(is_active=True).order_by('sort_order')],
        'card_payments': card,
    }


def _plan_line(p) -> str:
    return (f"- {p['name']} ({_num(p['price'])} ج/شهر): {p['branches']} فرع + {p['users']} مستخدم + "
            f"{p['treasuries']} خزينة" + (f" — {'، '.join(p['features'][:4])}" if p['features'] else ''))


def payment_methods_text(facts: dict) -> str:
    from django.conf import settings
    lines = [f"- فودافون كاش على 01094850763 أو إنستاباي على {settings.INSTAPAY_HANDLE}"
             f" (لينك الدفع: {settings.INSTAPAY_LINK}): حوّل المبلغ وارفع سكرين الإيصال من صفحة الدفع، "
             "والتفعيل بعد المراجعة"]
    if facts['card_payments']:
        lines.append("- فيزا/ماستركارد: دفع فوري آمن عبر Paymob")
    return '\n'.join(lines)


def pricing_text_for_bot() -> str:
    """بلوك «الباقات والأسعار» لبرومبت البوت — من الداتابيز."""
    f = pricing_facts()
    parts = ["💰 الباقات والأسعار (الأسعار الحالية بالجنيه المصري):", ""]
    if f['plans'].get('automotive'):
        parts += ["📌 باقات السيارات:", *[_plan_line(p) for p in f['plans']['automotive']], ""]
    if f['plans'].get('printing'):
        parts += ["📌 باقات الطباعة:", *[_plan_line(p) for p in f['plans']['printing']], ""]
    d = f['discounts']
    if d:
        parts.append(f"📌 خصومات: {d['quarterly']}% ربع سنوي | {d['semi_annual']}% نصف سنوي | {d['annual']}% سنوي")
    parts.append(f"📌 إضافة موظف/فرع/خزينة: {_num(f['addon_price'])} ج/شهر")
    parts.append(f"📌 تجربة مجانية: {f['trial_days']} يوم بكل مزايا أعلى باقة بدون دفع — ولو اشترك أثناء "
                 f"التجربة، المدة المدفوعة بتبدأ بعد ما التجربة تخلص")
    parts.append("📌 تغيير الباقة: الترقية فورية بفرق السعر عن الأيام الفاضلة، والتخفيض بيبدأ بعد المدة الحالية")
    if f['design_customer'] or f['design_designer']:
        parts += ["", "📌 باقات متجر التصميم بالذكاء الاصطناعي (دفعة واحدة):"]
        if f['design_customer']:
            parts.append("👤 للعملاء: " + " | ".join(f"{n} تصميم = {_num(p)} ج.م" for n, p in f['design_customer']))
        if f['design_designer']:
            parts.append("🎨 للمصممين: " + " | ".join(f"{n} تصميم = {_num(p)} ج.م" for n, p in f['design_designer']))
    if f['diag_topup']:
        parts.append("📌 شحن التشخيص للورش: " + " | ".join(f"{n} استخدام = {_num(p)} ج.م" for n, p in f['diag_topup']))
    parts += ["", "🔒 طرق الدفع:", payment_methods_text(f),
              "- الموقع الإماراتي: الاشتراك عن طريق الدعم على واتساب"]
    return '\n'.join(parts)
