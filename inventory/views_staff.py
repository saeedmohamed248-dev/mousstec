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
from django.shortcuts import get_object_or_404, redirect, render

from .models import Branch, EmployeeProfile
from .views import role_required, tenant_required


def _sync_branch_access(prof, post, branches):
    """يزامن صفوف BranchAccess من الفورم.

    لكل فرع (ماعدا الفرع الأساسي) فيه select اسمه access_<id> بقيمة:
      '' = مفيش صلاحية، 'view' = يشوف فقط، 'edit' = يشوف ويعدّل.
    """
    from .models import BranchAccess
    for b in branches:
        if prof.branch_id and b.id == prof.branch_id:
            # الفرع الأساسي دايماً تعديل — مبيتخزّنش كـ BranchAccess
            BranchAccess.objects.filter(employee=prof, branch=b).delete()
            continue
        mode = (post.get(f'access_{b.id}') or '').strip()
        if mode == 'view':
            BranchAccess.objects.update_or_create(
                employee=prof, branch=b, defaults={'can_edit': False})
        elif mode == 'edit':
            BranchAccess.objects.update_or_create(
                employee=prof, branch=b, defaults={'can_edit': True})
        else:
            BranchAccess.objects.filter(employee=prof, branch=b).delete()


def _build_invite_url(request, user) -> str:
    """Signed 7-day set-password link (tenant schema + user id) on this host."""
    token = signing.dumps({
        'schema_name': connection.schema_name,
        'user_id': user.id,
        'email': user.email,
        'created': int(time.time()),
    }, salt='employee-set-password')
    return f"{request.scheme}://{request.get_host()}/account/set-password/?token={token}"


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
def switch_branch(request):
    """🔁 تبديل الفرع النشط لموظف متعدد الفروع (يُحفظ في الـ session)."""
    if request.method != 'POST':
        return redirect('/system/dashboard/')
    prof = getattr(request.user, 'employee_profile', None)
    allowed = prof.allowed_branch_ids() if prof else None
    try:
        target = int(request.POST.get('branch') or 0)
    except (TypeError, ValueError):
        target = 0
    # الأدمن/superuser (allowed=None) يشوف الكل أصلاً؛ غير كده لازم الفرع مسموح
    if target and (allowed is None or target in allowed):
        request.session['active_branch_id'] = target
    # 🛡️ منع الـ open-redirect: نقبل مسارات داخلية فقط
    nxt = request.POST.get('next') or ''
    if not nxt.startswith('/') or nxt.startswith('//'):
        nxt = '/system/dashboard/'
    return redirect(nxt)


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

        set_url = _build_invite_url(request, user)
        _send_invite(email, full_name, set_url)

        ctx.update({
            'success': True,
            'created_email': email,
            'created_name': full_name,
            'set_url': set_url,
        })
        return render(request, 'inventory/add_employee.html', ctx)

    return render(request, 'inventory/add_employee.html', ctx)


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def staff_list(request):
    """Roster of all employees with role/branch/status and an edit link."""
    employees = (EmployeeProfile.objects
                 .select_related('user', 'branch')
                 .order_by('user__first_name', 'user__username'))
    return render(request, 'inventory/staff_list.html', {'employees': employees})


@login_required(login_url='/login/')
@tenant_required
@role_required('admin', 'manager')
def edit_employee(request, user_id):
    """Edit an existing employee's identity, role, branch and permissions."""
    user = get_object_or_404(User, pk=user_id)
    prof, _ = EmployeeProfile.objects.get_or_create(user=user)
    roles = EmployeeProfile.ROLE_CHOICES
    branches = Branch.objects.all().order_by('name')

    def _ctx(**extra):
        access_map = {
            ba.branch_id: ('edit' if ba.can_edit else 'view')
            for ba in prof.branch_access.all()
        }
        branch_rows = [{
            'branch': b,
            'is_primary': (prof.branch_id == b.id),
            'mode': access_map.get(b.id, ''),
        } for b in branches]
        c = {'roles': roles, 'branches': branches, 'branch_rows': branch_rows,
             'emp': user, 'prof': prof, 'is_self': (user.id == request.user.id)}
        c.update(extra)
        return c

    if request.method == 'POST':
        action = (request.POST.get('action') or 'save').strip()

        # 🔁 إعادة إرسال دعوة تعيين/تغيير كلمة المرور
        if action == 'resend':
            if not user.email:
                return render(request, 'inventory/edit_employee.html',
                              _ctx(error='الموظف ملوش إيميل مسجّل.'))
            set_url = _build_invite_url(request, user)
            _send_invite(user.email, user.get_full_name() or user.email, set_url)
            return render(request, 'inventory/edit_employee.html',
                          _ctx(success='تم إرسال رابط تعيين كلمة المرور للموظف على إيميله.',
                               set_url=set_url))

        # 💾 حفظ التعديلات
        full_name = (request.POST.get('full_name') or '').strip()
        email = (request.POST.get('email') or '').strip().lower()
        role = (request.POST.get('role') or prof.role).strip()
        branch_id = (request.POST.get('branch') or '').strip()
        can_see_costs = bool(request.POST.get('can_see_costs'))
        is_active = bool(request.POST.get('is_active'))
        max_discount_raw = (request.POST.get('max_discount_pct') or '0').strip()

        valid_roles = {r[0] for r in roles}
        errors = []
        if not full_name:
            errors.append('اكتب اسم الموظف.')
        if not email or '@' not in email or '.' not in email.split('@')[-1]:
            errors.append('اكتب إيميل صحيح.')
        if role not in valid_roles:
            errors.append('اختر دوراً وظيفياً صحيحاً.')
        if email and (User.objects.filter(email__iexact=email).exclude(pk=user.id).exists()
                      or User.objects.filter(username__iexact=email).exclude(pk=user.id).exists()):
            errors.append('فيه موظف تاني مسجّل بنفس الإيميل ده.')
        try:
            max_discount = Decimal(max_discount_raw or '0')
            if max_discount < 0 or max_discount > 100:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            errors.append('نسبة الخصم لازم تكون رقم بين 0 و 100.')
            max_discount = prof.max_discount_pct

        # 🛡️ لا تسمح للمدير إنه يوقف/ينزّل دور نفسه (يقفل على نفسه بالغلط)
        if user.id == request.user.id and not is_active:
            errors.append('مش ممكن توقف حسابك أنت.')
            is_active = True

        if errors:
            return render(request, 'inventory/edit_employee.html', _ctx(errors=errors))

        parts = full_name.split()
        user.first_name = parts[0]
        user.last_name = ' '.join(parts[1:])
        user.email = email
        user.username = email
        user.is_active = is_active
        user.save()

        prof.role = role
        prof.can_see_costs = can_see_costs
        prof.max_discount_pct = max_discount
        prof.branch_id = int(branch_id) if branch_id.isdigit() else None
        prof.save()

        # 🏢 صلاحيات الفروع الإضافية: لكل فرع select قيمته '', 'view', أو 'edit'
        from .models import BranchAccess
        _sync_branch_access(prof, request.POST, branches)

        return render(request, 'inventory/edit_employee.html',
                      _ctx(success='تم حفظ تعديلات الموظف.'))

    return render(request, 'inventory/edit_employee.html', _ctx())
