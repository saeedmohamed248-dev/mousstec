"""Staff onboarding — the owner / branch manager adds an employee by email
and role from inside the dashboard (no raw Django admin). The employee then
receives an email invite to set their own password and activate their login.

Mirrors the tenant signup email flow: fire-and-forget send (blocked SMTP on
cloud hosts must never hang the request), signed token carrying the tenant
schema, and a public /account/set-password/ landing page.
"""
from __future__ import annotations

import threading
import time
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core import signing
from django.db import connection
from django.shortcuts import render

from .models import Branch, EmployeeProfile
from .views import role_required, tenant_required


def _send_invite(email: str, full_name: str, set_url: str) -> None:
    """Email the new employee their set-password link (fire-and-forget)."""
    try:
        from django.core.mail import EmailMessage
        msg = EmailMessage(
            subject='👋 دعوة للانضمام | Mouss Tec',
            body=(
                f"أهلاً {full_name}،\n\n"
                f"تمت إضافتك كموظف على منصة Mouss Tec.\n"
                f"اضغط الرابط لتعيين كلمة مرورك وتفعيل حسابك:\n\n"
                f"{set_url}\n\n"
                f"الرابط صالح لمدة 7 أيام. بعد التفعيل تدخل من صفحة الدخول "
                f"بإيميلك وكلمة المرور.\n\nMouss Tec"
            ),
            from_email=None,
            to=[email],
        )
        msg.encoding = 'utf-8'
        threading.Thread(target=lambda m=msg: m.send(fail_silently=True), daemon=True).start()
    except Exception:
        # الصفحة بتعرض رابط الدعوة للمدير على أي حال، فمفيش داعي نكسر الطلب.
        pass


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def add_employee(request):
    """Single-screen employee onboarding: name + email + role + permissions."""
    roles = EmployeeProfile.ROLE_CHOICES
    branches = Branch.objects.all().order_by('name')
    ctx = {'roles': roles, 'branches': branches}

    if request.method == 'POST':
        full_name = (request.POST.get('full_name') or '').strip()
        email = (request.POST.get('email') or '').strip().lower()
        role = (request.POST.get('role') or 'cashier').strip()
        branch_id = (request.POST.get('branch') or '').strip()
        can_see_costs = bool(request.POST.get('can_see_costs'))
        max_discount_raw = (request.POST.get('max_discount_pct') or '0').strip()

        valid_roles = {r[0] for r in roles}
        errors = []
        if not full_name:
            errors.append('اكتب اسم الموظف.')
        if not email or '@' not in email or '.' not in email.split('@')[-1]:
            errors.append('اكتب إيميل صحيح.')
        if role not in valid_roles:
            errors.append('اختر دوراً وظيفياً صحيحاً.')
        if email and (User.objects.filter(email__iexact=email).exists()
                      or User.objects.filter(username__iexact=email).exists()):
            errors.append('فيه موظف مسجّل بنفس الإيميل ده بالفعل.')
        try:
            max_discount = Decimal(max_discount_raw or '0')
            if max_discount < 0 or max_discount > 100:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            errors.append('نسبة الخصم لازم تكون رقم بين 0 و 100.')
            max_discount = Decimal('0')

        if errors:
            ctx.update({'errors': errors, 'form': request.POST})
            return render(request, 'inventory/add_employee.html', ctx)

        # الإيميل هو اسم الدخول (الدخول بيتم بالإيميل). كلمة مرور غير صالحة
        # لحد ما الموظف يعيّنها من رابط الدعوة.
        parts = full_name.split()
        first = parts[0]
        last = ' '.join(parts[1:])
        user = User(username=email, email=email, first_name=first,
                    last_name=last, is_active=True)
        user.set_unusable_password()
        user.save()  # signal بينشئ EmployeeProfile تلقائياً

        prof = user.employee_profile
        prof.role = role
        prof.can_see_costs = can_see_costs
        prof.max_discount_pct = max_discount
        if branch_id.isdigit():
            prof.branch_id = int(branch_id)
        prof.save()

        token = signing.dumps({
            'schema_name': connection.schema_name,
            'user_id': user.id,
            'email': email,
            'created': int(time.time()),
        }, salt='employee-set-password')
        set_url = f"{request.scheme}://{request.get_host()}/account/set-password/?token={token}"

        _send_invite(email, full_name, set_url)

        ctx.update({
            'success': True,
            'created_email': email,
            'created_name': full_name,
            'set_url': set_url,
        })
        return render(request, 'inventory/add_employee.html', ctx)

    return render(request, 'inventory/add_employee.html', ctx)
