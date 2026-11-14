"""🛠️ كتالوج الخدمات والمصنعيات (ServiceCatalog) — إضافة/تعديل/حذف من البورتال.

🐛 [FIX — دورة ناقصة]: الخدمات اللي بتظهر في «أمر الشغل» كانت بتتضاف من لوحة Django admin بس،
فصاحب المركز مالوش طريقة يضيف «تغيير زيت» أو «وزن أمامي» بسعره ونسبة عمولة الفني.
"""
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import reverse

from inventory.models import ServiceCatalog
from inventory.views import tenant_required
from inventory.views.utils import module_required, role_required

SERVICE_ROLES = ('admin', 'manager', 'accountant')


def can_manage_services(user):
    """نفس قاعدة role_required (owner≡admin، supervisor≡manager) — لإظهار لينك «إدارة الخدمات»."""
    if user.is_superuser:
        return True
    role = getattr(getattr(user, 'employee_profile', None), 'role', None)
    return {'owner': 'admin', 'supervisor': 'manager'}.get(role, role) in SERVICE_ROLES


def _parse(post):
    """بيرجّع (data, None) أو (None, كود الغلطة). الحدود على قد أعمدة الموديل عشان مايوقعش الحفظ بـ 500."""
    name = (post.get('name') or '').strip()[:200]
    if not name:
        return None, 'name'
    try:
        price = Decimal((post.get('labor_price') or '0').strip())
        hours = Decimal((post.get('estimated_hours') or '1').strip())
        commission = Decimal((post.get('tech_commission_percent') or '0').strip())
    except (InvalidOperation, AttributeError):
        return None, 'number'
    if not all(v.is_finite() for v in (price, hours, commission)):
        return None, 'number'
    if not (Decimal('0') <= price < Decimal('100000000') and Decimal('0') <= hours < Decimal('1000')
            and Decimal('0') <= commission <= Decimal('100')):
        return None, 'number'
    return {
        'name': name,
        'labor_price': price.quantize(Decimal('0.01')),
        'estimated_hours': hours.quantize(Decimal('0.1')),
        'tech_commission_percent': commission.quantize(Decimal('0.01')),
    }, None


@login_required(login_url='/login/')
@tenant_required
@role_required(*SERVICE_ROLES)
@module_required('jobcard')
def services_catalog(request):
    back = reverse('inventory:services_catalog')
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'delete':
            svc = ServiceCatalog.objects.filter(pk=request.POST.get('pk') or 0).first()
            if svc is None:
                return redirect(back + '?err=notfound')
            # الخدمة اتباعت قبل كده (PROTECT من بنود الفواتير) ⇒ مانمسحهاش عشان التقارير والعمولات
            if svc.saleinvoiceserviceitem_set.exists():
                return redirect(back + '?err=used')
            svc.delete()
            return redirect(back + '?ok=deleted')
        data, err = _parse(request.POST)
        if err:
            return redirect(back + f'?err={err}')
        if action == 'edit':
            updated = ServiceCatalog.objects.filter(pk=request.POST.get('pk') or 0).update(**data)
            return redirect(back + ('?ok=edited' if updated else '?err=notfound'))
        ServiceCatalog.objects.create(**data)
        return redirect(back + '?ok=added')

    services = (ServiceCatalog.objects.annotate(times_used=Count('saleinvoiceserviceitem'))
                .order_by('name'))
    return render(request, 'inventory/services_catalog.html', {
        'services': services,
        'ok': request.GET.get('ok', ''),
        'err': request.GET.get('err', ''),
    })
