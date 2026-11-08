"""
🧭 «أول خطوات» للشركة الجديدة — قايمة 5 خطوات بتتعلّم لوحدها من بيانات الشركة.

الفكرة: العميل اللي بيسجّل ويلاقي السيستم فاضي بيمشي في أول يوم. القايمة بتقوله يبدأ منين
وكل خطوة ليها زرار للشاشة بتاعتها، وبتتشطب لوحدها أول ما يعملها (من غير ما يضغط «تم»).
القايمة بتختفي لما الخمس خطوات يخلصوا. للمالك/المدير بس.
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


def _print_steps():
    from printing.models import (MachineProfile, PrintMaterial, PrintOrder, PrintProof,
                                 PrintTransaction, PriceQuotation)
    return [
        ('ضيف ماكيناتك', 'وتكلفة تشغيلها عشان الربح يتحسب صح', MachineProfile.objects.exists(),
         _url('admin:printing_machineprofile_add')),
        ('سجّل خاماتك', 'الورق والحبر والفينيل اللي بتصرف منها', PrintMaterial.objects.exists(),
         _url('admin:printing_printmaterial_changelist')),
        ('اعمل أول فاتورة', 'الفاتورة السريعة — العميل والبنود والمدفوع في شاشة واحدة',
         PrintOrder.objects.exists(), _url('printing:quick_invoice')),
        ('ابعت أول عرض سعر أو بروفة', 'لينك العميل يوافق منه من موبايله',
         PriceQuotation.objects.exists() or PrintProof.objects.exists(),
         _url('admin:printing_pricequotation_add')),
        ('حصّل أول دفعة', 'الفلوس بتدخل الخزنة ومديونية العميل بتقل',
         PrintTransaction.objects.filter(transaction_type='in').exists(), _url('printing:quick_collect')),
    ]


def onboarding_steps(tenant):
    """{'steps': [...], 'done': n, 'total': n, 'percent'} أو None لو خلصت كلها."""
    if tenant is None or getattr(tenant, 'schema_name', 'public') == 'public':
        return None
    raw = _print_steps() if getattr(tenant, 'industry', '') == 'printing' else _auto_steps()
    steps = [{'title': _(t), 'hint': _(h), 'done': bool(d), 'url': u} for t, h, d, u in raw]
    done = sum(s['done'] for s in steps)
    if done == len(steps):
        return None
    return {'steps': steps, 'done': done, 'total': len(steps), 'percent': int(done * 100 / len(steps))}
