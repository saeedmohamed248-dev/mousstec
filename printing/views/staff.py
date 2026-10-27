"""
👥 موظفين المطبعة — إضافة موظف بدور جاهز (استقبال/إنتاج/مصمم/محاسب/مدير) وصلاحياته.

🐛 [FIX]: لينك «الموظفون والصلاحيات (الطريقة السهلة)» في المطابع كان بيودّي على
/system/staff/ (صفحة السيارات) ⇒ 404. فصاحب المطبعة كان لازم يعمل الموظف من شاشة
Django الخام ويختار عشرات الصلاحيات بإيده. هنا: إيميل + دور ⇒ رابط دعوة يعيّن
بيه الموظف كلمة السر، والدور بيدّيله صلاحيات الشاشات والفلوس المناسبة.
"""
from __future__ import annotations

from django.contrib import admin, messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group, Permission, User
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _, gettext_lazy as _l

from erp_core.decorators import tenant_or_404

# الموديل ← الصلاحيات (v=عرض a=إضافة c=تعديل d=حذف)
_ALL = 'vacd'
PRINT_ROLES = {
    'reception': {
        'label': _l('استقبال ومبيعات'),
        'hint': _l('العملاء والطلبات وعروض الأسعار واستلام الدفعات'),
        'perms': {'printcustomer': 'vac', 'printorder': 'vac', 'printjob': 'vac', 'pricequotation': 'vac',
                  'quotationline': _ALL, 'printtransaction': 'va', 'producttype': 'va', 'printbranch': 'v',
                  'machineprofile': 'v', 'printtreasury': 'v'},
        'flags': {'can_create_orders', 'can_edit_orders', 'can_view_all_orders', 'can_manage_customers',
                  'can_view_project_files', 'can_upload_project_files'},
    },
    'production': {
        'label': _l('إنتاج وتشغيل'),
        'hint': _l('مهام الطباعة والخامات المصروفة والماكينات — من غير فلوس'),
        'perms': {'printorder': 'v', 'printjob': 'vc', 'printjobmaterial': _ALL, 'printmaterial': 'v',
                  'machineprofile': 'v', 'producttype': 'v', 'printcustomer': 'v'},
        'flags': {'can_view_all_orders', 'can_view_project_files'},
    },
    'designer': {
        'label': _l('مصمم'),
        'hint': _l('المهام وملفات التصميم وسجل أعماله وAI Studio'),
        'perms': {'printorder': 'v', 'printjob': 'vc', 'designerworklog': 'vac', 'printcustomer': 'v',
                  'producttype': 'va'},
        'flags': {'can_view_all_orders', 'can_view_project_files', 'can_upload_project_files',
                  'can_use_ai_studio'},
    },
    'accountant': {
        'label': _l('محاسب'),
        'hint': _l('الخزائن والدفعات وكشوف الحساب والتقارير والأرباح'),
        'perms': {'printorder': 'vc', 'printjob': 'v', 'printcustomer': 'v', 'printtransaction': 'vac',
                  'printtreasury': 'vc', 'pricequotation': 'v', 'printmaterial': 'v'},
        'flags': {'can_view_all_orders', 'can_view_treasury', 'can_manage_treasury', 'can_view_profits',
                  'can_view_reports'},
    },
    'manager': {
        'label': _l('مدير المطبعة'),
        'hint': _l('كل شاشات المطبعة والفلوس — ماعدا إدارة الموظفين'),
        'perms': {m: _ALL for m in ('printcustomer', 'printorder', 'printjob', 'pricequotation', 'quotationline',
                                    'printtransaction', 'printtreasury', 'printmaterial', 'printjobmaterial',
                                    'machineprofile', 'designer', 'designerworklog', 'producttype', 'printbranch')},
        'flags': '*',
    },
}
FLAG_FIELDS = [
    ('can_view_treasury', _l('مشاهدة الخزينة')),
    ('can_manage_treasury', _l('إدارة الخزينة (إيداع/سحب/رد فلوس)')),
    ('can_view_profits', _l('مشاهدة الأرباح والتكاليف')),
    ('can_view_reports', _l('التقارير وكشوف الحساب')),
    ('can_create_orders', _l('إنشاء طلبات')),
    ('can_edit_orders', _l('تعديل طلبات')),
    ('can_delete_orders', _l('حذف طلبات')),
    ('can_view_all_orders', _l('مشاهدة كل الطلبات')),
    ('can_manage_customers', _l('إدارة العملاء')),
    ('can_view_project_files', _l('مشاهدة ملفات المشاريع')),
    ('can_upload_project_files', _l('رفع ملفات المشاريع')),
    ('can_manage_stock', _l('إدارة المخزون')),
    ('can_view_designers', _l('مشاهدة أداء المصممين')),
    ('can_use_ai_studio', _l('استخدام AI Studio')),
]
_GROUP_PREFIX = 'printing:'
_ACTIONS = {'v': 'view', 'a': 'add', 'c': 'change', 'd': 'delete'}


def role_group(role):
    """The auth Group for ``role`` with the preset's permissions (kept in sync)."""
    preset = PRINT_ROLES[role]
    codenames = [f'{_ACTIONS[a]}_{model}' for model, acts in preset['perms'].items() for a in acts]
    group, _created = Group.objects.get_or_create(name=f'{_GROUP_PREFIX}{role}')
    group.permissions.set(Permission.objects.filter(content_type__app_label='printing', codename__in=codenames))
    return group


def user_role(user):
    names = [g.name for g in user.groups.all() if g.name.startswith(_GROUP_PREFIX)]
    return names[0][len(_GROUP_PREFIX):] if names else ''


def apply_role(user, role, *, reset_flags=True):
    """Give ``user`` the role's screens (via its group) and, optionally, its default money flags."""
    from printing.models import Designer, StaffPermission
    user.groups.remove(*user.groups.filter(name__startswith=_GROUP_PREFIX))
    user.groups.add(role_group(role))
    perms, _c = StaffPermission.objects.get_or_create(user=user)
    if reset_flags:
        wanted = PRINT_ROLES[role]['flags']
        for field, _label in FLAG_FIELDS:
            setattr(perms, field, wanted == '*' or field in wanted)
        perms.save()
    if role == 'designer':
        Designer.objects.get_or_create(user=user)   # يظهر في اختيار المصمم على المهام
    return perms


def _owner_only(view):
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            return HttpResponseForbidden(_('إدارة الموظفين لصاحب المطبعة بس.'))
        return view(request, *args, **kwargs)
    wrapped.__name__ = view.__name__
    wrapped.__doc__ = view.__doc__
    return wrapped


def _ctx(request, **extra):
    ctx = admin.site.each_context(request)
    ctx.update({'roles': [(k, v['label'], v['hint']) for k, v in PRINT_ROLES.items()],
                'flag_fields': FLAG_FIELDS})
    ctx.update(extra)
    return ctx


@login_required
@tenant_or_404
@_owner_only
def staff_list(request):
    """قايمة موظفين المطبعة بأدوارهم وحالتهم."""
    rows = []
    for u in User.objects.filter(is_staff=True).prefetch_related('groups').order_by('-is_active', 'first_name', 'username'):
        role = user_role(u)
        rows.append({'user': u, 'role': role, 'role_label': PRINT_ROLES[role]['label'] if role else '',
                     'is_owner': u.is_superuser})
    return render(request, 'printing/staff_list.html', _ctx(request, rows=rows, title=_('موظفين المطبعة')))


@login_required
@tenant_or_404
@_owner_only
def staff_add(request):
    """إضافة موظف: الاسم + الإيميل + الدور ⇒ رابط دعوة لتعيين كلمة السر."""
    from inventory.views_staff import _build_invite_url, _send_invite
    form = {'role': 'reception'}
    errors = []
    if request.method == 'POST':
        form = request.POST
        full_name = (form.get('full_name') or '').strip()
        email = (form.get('email') or '').strip().lower()
        role = (form.get('role') or '').strip()
        if not full_name:
            errors.append(_('اكتب اسم الموظف.'))
        if not email or '@' not in email or '.' not in email.split('@')[-1]:
            errors.append(_('اكتب إيميل صحيح — الموظف بيدخل بيه.'))
        elif User.objects.filter(email__iexact=email).exists() or User.objects.filter(username__iexact=email).exists():
            errors.append(_('فيه موظف مسجّل بنفس الإيميل ده بالفعل.'))
        if role not in PRINT_ROLES:
            errors.append(_('اختار دور الموظف.'))
        if not errors:
            first, _sp, last = full_name.partition(' ')
            with transaction.atomic():
                user = User(username=email, email=email, first_name=first, last_name=last.strip(),
                            is_active=True, is_staff=True)
                user.set_unusable_password()
                user.save()
                apply_role(user, role)
            set_url = _build_invite_url(request, user)
            _send_invite(email, full_name, set_url)
            return render(request, 'printing/staff_form.html', _ctx(
                request, title=_('إضافة موظف'), created=user, set_url=set_url,
                role_label=PRINT_ROLES[role]['label']))
    return render(request, 'printing/staff_form.html', _ctx(request, title=_('إضافة موظف'), form=form, errors=errors))


@login_required
@tenant_or_404
@_owner_only
def staff_edit(request, user_id):
    """تعديل دور الموظف وصلاحيات الفلوس، إيقافه/تفعيله، ورابط دعوة جديد."""
    from inventory.views_staff import _build_invite_url
    from printing.models import StaffPermission
    member = get_object_or_404(User, pk=user_id, is_staff=True)
    if member.is_superuser:
        messages.info(request, _('صاحب المطبعة عنده كل الصلاحيات.'))
        return redirect('printing:staff_list')
    perms, _c = StaffPermission.objects.get_or_create(user=member)
    set_url = ''
    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        if action == 'toggle_active':
            member.is_active = not member.is_active
            member.save(update_fields=['is_active'])
            messages.success(request, _('الموظف اتفعّل.') if member.is_active else _('الموظف اتوقف — مش هيقدر يدخل.'))
            return redirect('printing:staff_edit', user_id=member.pk)
        if action == 'invite':
            set_url = _build_invite_url(request, member)
        else:
            role = (request.POST.get('role') or '').strip()
            if role in PRINT_ROLES:
                role_changed = role != user_role(member)
                apply_role(member, role, reset_flags=role_changed)
                if not role_changed:
                    for field, _label in FLAG_FIELDS:
                        setattr(perms, field, bool(request.POST.get(field)))
                    perms.save()
                messages.success(request, _('اتحفظت صلاحيات الموظف.') if not role_changed
                                 else _('الدور اتغيّر — والصلاحيات رجعت لافتراضي الدور الجديد.'))
                return redirect('printing:staff_edit', user_id=member.pk)
            messages.error(request, _('اختار دور الموظف.'))
    perms.refresh_from_db()
    return render(request, 'printing/staff_edit.html', _ctx(
        request, title=member.get_full_name() or member.username, member=member, current_role=user_role(member),
        # ⚠️ مش «perms» — ده اسم متغيّر Django الجاهز للصلاحيات (القايمة الجانبية بتستخدمه)
        flags=[(f, label, getattr(perms, f)) for f, label in FLAG_FIELDS], set_url=set_url))
