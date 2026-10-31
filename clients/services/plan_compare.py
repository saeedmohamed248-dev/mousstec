"""
📊 جدول «قارن الباقات» — كل ميزة وكل حد لكل باقة، من الداتابيز مباشرة
=======================================================================
قبل كده كارت الباقة كان بيعرض سطور مكتوبة بإيد المالك + أسماء المزايا المفعّلة من غير
أرقامها — فحدود زي «150 كرت صيانة/شهر» أو «50 سؤال للمستشار» كانت بتظهر بس لو حد كتبها
بإيده، ولو المالك غيّر الحد من محرر الباقات الكلام المكتوب بيفضل قديم.

الجدول ده بيتبني من نفس القيم اللي بتتطبّق فعلاً على الشركة:
    • Plan.max_users / max_branches / max_treasuries
    • entitlements[code].monthly_limit (0 أو فاضي = غير محدود — نفس EntitlementService)
    • Plan.monthly_diagnostics_scans_quota / monthly_diagnostics_bot_quota / monthly_ai_designs_quota
      (0 = مش ضمن الباقة — بتتشحن بباقة إضافية)
فأي تعديل من لوحة المالك بيظهر للعميل في نفس اللحظة.
"""
from __future__ import annotations

from django.utils.translation import gettext as _

# مزايا ليها رقم: (الكود، عنوان السطر). عنوان السطر فيه الوحدة عشان الخانة تبقى رقم بس.
LIMIT_FEATURES = (
    ('workshop_repair_cards', 'كروت الصيانة في الشهر'),
    ('inventory_items', 'أصناف المخزون'),
    ('print_orders', 'طلبات الطباعة في الشهر'),
    ('ai_copilot', 'أسئلة المستشار الذكي في الشهر'),
)
# حصص محفوظة في أعمدة الباقة نفسها (0 = مش ضمن الباقة)
QUOTA_FIELDS = (
    ('monthly_diagnostics_scans_quota', 'فحوصات التشخيص في الشهر'),
    ('monthly_diagnostics_bot_quota', 'أسئلة بوت التشخيص في الشهر'),
    ('monthly_ai_designs_quota', 'تصاميم AI في الشهر'),
)
# حدود الأصناف مالهاش معنى من غير المخزون نفسه: لو المخزون مفعّل ومفيش حد ⇒ غير محدود
_IMPLIED_BY = {'inventory_items': 'core_inventory'}


def _ents(plan):
    ents = plan.entitlements if isinstance(plan.entitlements, dict) else {}
    return {c: cfg for c, cfg in ents.items() if isinstance(cfg, dict)}


def _limit_cell(ents, code):
    cfg = ents.get(code)
    if cfg is None and code in _IMPLIED_BY:
        cfg = ents.get(_IMPLIED_BY[code])
        if cfg and cfg.get('enabled'):
            return {'unlimited': True}
    if not cfg or not cfg.get('enabled'):
        return {'no': True}
    try:
        limit = int(cfg.get('monthly_limit') or 0)
    except (TypeError, ValueError):
        limit = 0
    return {'n': limit} if limit > 0 else {'unlimited': True}


def compare_table(plans, feature_labels):
    """plans: باقات قطاع واحد بالترتيب · feature_labels: {code: اسم الميزة باللغة الحالية}.

    ⇒ {'rows': [{'label', 'cells': [{'n'}|{'unlimited'}|{'yes'}|{'no'}]}]}
    السطور اللي مفيش ولا باقة في القطاع فيها الميزة مش بتظهر."""
    plans = list(plans)
    if not plans:
        return None
    all_ents = [_ents(p) for p in plans]
    rows = [
        {'label': _('المستخدمين'), 'cells': [{'n': p.max_users} for p in plans]},
        {'label': _('الفروع'), 'cells': [{'n': p.max_branches} for p in plans]},
        {'label': _('الخزائن'), 'cells': [{'n': p.max_treasuries} for p in plans]},
    ]
    shown = set()
    for code, label in LIMIT_FEATURES:
        cells = [_limit_cell(e, code) for e in all_ents]
        if any('no' not in c for c in cells):
            rows.append({'label': _(label), 'cells': cells, 'limit': True})
            shown.add(code)
    for field, label in QUOTA_FIELDS:
        values = [int(getattr(p, field, 0) or 0) for p in plans]
        if any(values):
            rows.append({'label': _(label), 'limit': True,
                         'cells': [{'n': v} if v > 0 else {'no': True} for v in values]})
    # باقي المزايا: ✓/✗ — اللي في كل الباقات الأول وبعدين اللي في الباقات الأعلى (زي جداول المقارنة
    # العالمية)، وبس اللي في باقة واحدة على الأقل من القطاع
    extra = []
    for code, label in feature_labels.items():
        if code in shown or code in _IMPLIED_BY:
            continue
        # ميزة جديدة ليها حد من محرر الباقات بتظهر برقمها من غير ما نعدّل هنا
        cells = [{'yes': True} if c == {'unlimited': True} else c
                 for c in (_limit_cell(e, code) for e in all_ents)]
        if any('no' not in c for c in cells):
            first = next(i for i, c in enumerate(cells) if 'no' not in c)
            extra.append((first, -sum('no' not in c for c in cells), {'label': label, 'cells': cells}))
    rows.extend(row for *_, row in sorted(extra, key=lambda t: t[:2]))
    return {'rows': rows}


def feature_labels(is_en):
    """{code: اسم الميزة} من كتالوج المزايا باللغة الحالية (name_en ولو فاضي العربي)."""
    from clients.models import Feature
    return {code: ((en or ar) if is_en else ar) for code, ar, en in
            Feature.objects.filter(is_active=True).order_by('category', 'code')
            .values_list('code', 'name_ar', 'name_en')}


def plan_display_name(plan, is_en):
    from django.utils.translation import gettext
    return (plan.name_en or gettext(plan.name)) if is_en else plan.name


def compare_tables(plans, *, is_en, labels=None):
    """{industry: {'plans': [أسماء], 'rows': [...]}} لكل قطاع فيه باقات — لصفحة الأسعار والمميزات."""
    labels = feature_labels(is_en) if labels is None else labels
    by_industry = {}
    for p in plans:
        by_industry.setdefault(p.industry, []).append(p)
    out = {}
    for industry, objs in by_industry.items():
        table = compare_table(objs, labels)
        if table:
            table['plans'] = [plan_display_name(p, is_en) for p in objs]
            out[industry] = table
    return out
