"""
🧭 «أول خطوات» للشركة الجديدة — قايمة خطوات قصيرة بتتعلّم لوحدها من بيانات الشركة.

الفكرة: العميل اللي بيسجّل ويلاقي السيستم فاضي بيمشي في أول يوم. القايمة بتقوله يبدأ منين
وكل خطوة ليها زرار للشاشة بتاعتها، وبتتشطب لوحدها أول ما يعملها (من غير ما يضغط «تم»).
القايمة بتختفي لما الخطوات كلها تخلص. للمالك/المدير بس.
"""
from __future__ import annotations

from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _


def _url(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return '#'


def _auto_steps():
    from inventory.models import Customer, EmployeeProfile, Product, SaleInvoice, Treasury
    walk_in = Customer.normalize_phone('0000000000')
    return [
        ('ضيف أصنافك', 'من ملف Excel أو بالتصوير أو صنف صنف', Product.objects.exists(),
         _url('inventory:product_import')),
        ('جهّز خزنتك', 'الخزنة اللي الفلوس بتدخلها وتخرج منها', Treasury.objects.exists(),
         _url('inventory:treasury_list')),
        ('اعمل أول فاتورة بيع', 'من الكاشير — المخزون والخزنة بيتظبطوا لوحدهم',
         SaleInvoice.objects.filter(is_return=False).exists(), _url('inventory:lightning_pos')),
        ('سجّل أول عميل', 'عشان الآجل وكشف الحساب', Customer.objects.exclude(phone=walk_in).exists(),
         _url('inventory:customers_receivables')),
        ('ضيف موظف', 'كاشير أو فني بصلاحياته', EmployeeProfile.objects.exclude(user__is_superuser=True).exists(),
         _url('inventory:add_employee')),
    ]


_PRINT_EXPENSE_CATEGORIES = ('rent', 'utilities', 'maintenance', 'shipping', 'marketing', 'materials',
                             'salaries', 'other_expense')


def _print_steps():
    """🖨️ خطوات المطبعة بترتيب الدورة: بيع ← عميل ← خزنة ← ماكينة ← شراء ← مصروف.

    🐛 [FIX]: كان فيه قايمتين في لوحة المطبعة — «ابدأ هنا» (5 خطوات) و«أول خطوات عشان تبدأ شغلك»
    (5 خطوات تانية) بأرقام مختلفة («خلصت 0 من 5» و«خلصت 1 من 5») وخطوات متكررة. وخطوة «سجّل
    خاماتك» كانت بتتشطب لوحدها من أول يوم لأن الخامات الأساسية بتتعمل مع الشركة. دلوقتي قايمة واحدة
    بتستخدمها اللوحة والكارت العام، و«ضيف ماكيناتك» (تكلفة التشغيل = ربح صح) بقت جزء منها.
    (key, title, hint, done, url)
    """
    from printing.models import MachineProfile, MaterialPurchase, PrintCustomer, PrintOrder, PrintTransaction, PrintTreasury
    capital_url = _url('admin:printing_printtransaction_add')
    if capital_url != '#':
        capital_url += '?mode=income&category=capital'
    return [
        ('order', 'اعمل أول فاتورة / طلب لعميل', 'دقيقة — العميل والبنود والمدفوع في شاشة واحدة',
         PrintOrder.objects.exists(), _url('printing:quick_invoice')),
        ('customer', 'ضيف أول عميل', 'اسمه ورقمه — وتقدر تضيف مديونيته القديمة',
         PrintCustomer.objects.exists(), _url('printing:quick_customer')),
        ('capital', 'سجّل الفلوس اللي في الخزنة', 'رأس المال أو الكاش اللي بتبدأ بيه',
         PrintTransaction.objects.filter(category='capital').exists()
         or PrintTreasury.objects.exclude(balance=0).exists(), capital_url),
        ('machine', 'ضيف ماكيناتك', 'وتكلفة تشغيلها عشان الربح يتحسب صح',
         MachineProfile.objects.exists(), _url('admin:printing_machineprofile_add')),
        ('purchase', 'سجّل أول فاتورة شراء خامات', 'الورق والحبر بيدخلوا المخزن بتكلفتهم',
         MaterialPurchase.objects.exists(), _url('printing:quick_purchase')),
        ('expense', 'سجّل أول مصروف', 'إيجار، كهرباء، مواصلات…',
         PrintTransaction.objects.filter(category__in=_PRINT_EXPENSE_CATEGORIES).exists(),
         _url('printing:quick_expense')),
    ]


def print_start_steps():
    """🚀 «ابدأ هنا» في لوحة المطبعة: نفس قايمة onboarding_steps + أول خطوة لسه ماخلصتش."""
    steps = [{'key': k, 'title': _(t), 'hint': _(h), 'done': bool(d), 'url': u} for k, t, h, d, u in _print_steps()]
    done = sum(s['done'] for s in steps)
    if done == len(steps):
        return None
    return {'steps': steps, 'done': done, 'total': len(steps), 'percent': int(done * 100 / len(steps)),
            'next_key': next(s['key'] for s in steps if not s['done'])}


def onboarding_steps(tenant):
    """{'steps': [...], 'done': n, 'total': n, 'percent'} أو None لو خلصت كلها."""
    if tenant is None or getattr(tenant, 'schema_name', 'public') == 'public':
        return None
    if getattr(tenant, 'industry', '') == 'printing':
        return print_start_steps()
    raw = _auto_steps()
    steps = [{'title': _(t), 'hint': _(h), 'done': bool(d), 'url': u} for t, h, d, u in raw]
    done = sum(s['done'] for s in steps)
    if done == len(steps):
        return None
    return {'steps': steps, 'done': done, 'total': len(steps), 'percent': int(done * 100 / len(steps))}
