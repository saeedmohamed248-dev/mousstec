"""👥 لوحة مالك المنصة — قفل/فتح الموديولات وفريق المنصة."""
from __future__ import annotations

import logging

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from clients.permissions import platform_owner_required
from ._common import _log_event

logger = logging.getLogger('mouss_tec_core')


# ─────────────────────────────────────────────────────────────────────
# 🧩 الموديولات — قفل/فتح موديول للمنصة كلها + مين بيستخدمه
# ─────────────────────────────────────────────────────────────────────
@platform_owner_required
def modules_list(request):
    from clients.models import PlatformModule
    from clients.services import modules as mods
    if request.method == 'POST':
        code = request.POST.get('code')
        if code in mods.MODULES:
            on = request.POST.get('action') == 'enable'
            PlatformModule.objects.update_or_create(code=code, defaults={
                'is_enabled': on, 'updated_by': request.user,
                'note': (request.POST.get('note') or '').strip()[:200]})
            mods.clear_cache()
            _log_event('other', user=request.user,
                       description=f"🧩 {'تشغيل' if on else 'قفل'} موديول {mods.MODULES[code]['name']}")
            messages.success(request, f"{'✅ اتفتح' if on else '⏸️ اتقفل'}: {mods.MODULES[code]['name']}"
                                      + ('' if on else ' — بياناته محفوظة وترجع أول ما تفتحه.'))
        return redirect('saas_modules')
    if request.GET.get('refresh'):
        from django.core.cache import cache
        cache.delete('platform_module_usage_v1_30')
    usage = mods.usage_stats(30)
    rows = PlatformModule.objects.select_related('updated_by').in_bulk(field_name='code')
    data = []
    for code, spec in mods.MODULES.items():
        row = rows.get(code)
        data.append({'code': code, 'spec': spec, 'on': mods.is_enabled(code), 'row': row,
                     'usage': usage.get(code, {'companies': 0, 'records': 0})})
    return render(request, 'clients/saas_admin/modules.html', {
        'modules': data, 'tenants_total': usage.get('_tenants', 0)})


# ─────────────────────────────────────────────────────────────────────
# 👥 فريق المنصة — إضافة موظف دعم/مبيعات/محاسبة وصلاحياته من غير Django admin
# ─────────────────────────────────────────────────────────────────────
def _temp_password():
    import secrets
    return secrets.token_urlsafe(9)


@platform_owner_required
def team_list(request):
    from django.contrib.auth import get_user_model
    from django.core.validators import validate_email
    from django.core.exceptions import ValidationError as _VE
    from clients.models import PlatformUserMFA, StaffRole
    User = get_user_model()
    roles = dict(StaffRole.ROLE_CHOICES)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            email = (request.POST.get('email') or '').strip().lower()
            role = request.POST.get('role')
            try:
                validate_email(email)
            except _VE:
                messages.error(request, "🛑 الإيميل غير صحيح.")
                return redirect('saas_team')
            if role not in roles or role == 'god':
                messages.error(request, "🛑 اختار دور من القائمة.")
                return redirect('saas_team')
            if User.objects.filter(username__iexact=email).exists():
                messages.error(request, "🛑 فيه حساب بالإيميل ده بالفعل.")
                return redirect('saas_team')
            password = _temp_password()
            first, _sp, last = (request.POST.get('name') or '').strip().partition(' ')
            user = User.objects.create_user(username=email, email=email, password=password, is_staff=True,
                                            first_name=first[:150], last_name=last[:150])
            StaffRole.objects.create(user=user, role=role,
                                     can_force_delete=request.POST.get('can_force_delete') == 'on')
            _log_event('other', user=request.user, description=f"👥 إضافة {email} للفريق كـ {roles[role]}")
            messages.success(request, f"✅ اتضاف {email} ({roles[role]}). كلمة السر المؤقتة: {password} "
                                      f"— ابعتهاله ويغيّرها أول ما يدخل من /secure-portal/login/")
            return redirect('saas_team')

        member = get_object_or_404(User, pk=request.POST.get('user_id'), is_superuser=False)
        if action == 'role':
            role = request.POST.get('role')
            if role in roles and role != 'god':
                StaffRole.objects.update_or_create(user=member, defaults={'role': role})
                _log_event('other', user=request.user, description=f"👥 دور {member.username} ⇐ {roles[role]}")
                messages.success(request, f"✅ دور {member.username} بقى {roles[role]}.")
        elif action == 'toggle':
            member.is_active = not member.is_active
            member.save(update_fields=['is_active'])
            _log_event('other', user=request.user,
                       description=f"👥 {'تفعيل' if member.is_active else 'إيقاف'} حساب {member.username}")
            messages.success(request, f"{'✅ اتفعّل' if member.is_active else '⏸️ اتوقف'} حساب {member.username}.")
        elif action == 'reset_password':
            password = _temp_password()
            member.set_password(password)
            member.save(update_fields=['password'])
            _log_event('other', user=request.user, description=f"👥 كلمة سر جديدة لـ {member.username}")
            messages.success(request, f"🔑 كلمة السر الجديدة لـ {member.username}: {password}")
        elif action == 'reset_mfa':
            PlatformUserMFA.objects.filter(user=member).update(is_enabled=False, backup_codes=[], enabled_at=None)
            _log_event('other', user=request.user, description=f"🔐 إعادة ضبط التحقق بخطوتين لـ {member.username}")
            messages.success(request, f"🔐 التحقق بخطوتين اتقفل لـ {member.username} — يفعّله تاني من حسابه.")
        return redirect('saas_team')

    members = (User.objects.filter(Q(is_superuser=True) | Q(staff_role__isnull=False))
               .select_related('staff_role').order_by('-is_superuser', 'username'))
    mfa_on = set(PlatformUserMFA.objects.filter(is_enabled=True).values_list('user_id', flat=True))
    return render(request, 'clients/saas_admin/team.html', {
        'members': [{'user': u, 'role': getattr(u, 'staff_role', None), 'mfa': u.pk in mfa_on} for u in members],
        'roles': [(k, v) for k, v in StaffRole.ROLE_CHOICES if k != 'god'],
        'role_legend': [(v, ' · '.join(sorted(StaffRole.ROLE_WIDGETS.get(k, ()))))
                        for k, v in StaffRole.ROLE_CHOICES if k != 'god'],
    })
