"""
🏢 /account/workspace/ — إعدادات مساحة العمل لمالك الشركة:
تغيير المجال في الفترة التجريبية (مرتين بالكتير) والحذف النهائي للحساب.
المنطق كله في clients/services/workspace.py.
"""
from __future__ import annotations

import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils.translation import gettext as _
from django_tenants.utils import get_public_schema_name

from clients.forms import TenantSignupForm
from clients.models import Client
from clients.services import workspace as ws
from clients.views.auth_views import _client_ip, _throttle


def _industry_targets(tenant):
    """اختيارات «المجال الجديد» متجمّعة بالمجال — من غير الاختيار الحالي."""
    industry_labels = dict(Client.INDUSTRY_CHOICES)
    type_labels = dict(Client.BUSINESS_TYPE_CHOICES)
    groups = []
    for industry, types in TenantSignupForm.INDUSTRY_BUSINESS_TYPES.items():
        options = [{'value': f'{industry}:{bt}', 'label': type_labels.get(bt, bt)}
                   for bt in types if (industry, bt) != (tenant.industry, tenant.business_type)]
        if options:
            groups.append({'industry': industry, 'label': industry_labels.get(industry, industry),
                           'options': options})
    return groups


def _owner_tenant_or_deny(request):
    tenant = getattr(request, 'tenant', None)
    if not tenant or tenant.schema_name == get_public_schema_name():
        return None
    if not request.user.is_superuser or tenant.is_fraud_flagged:
        raise PermissionDenied
    return tenant


@login_required(login_url='/login/')
def workspace_export(request):
    """📦 نسخة من بيانات الشركة (ZIP فيه CSV لكل جدول) — للمالك بس، 3 مرات في الساعة."""
    tenant = _owner_tenant_or_deny(request)
    if tenant is None:
        return redirect('/')
    blocked, _wait = _throttle(f"workspace_export:{tenant.schema_name}", limit=3, window=3600)
    if blocked:
        messages.error(request, _("🚫 نزّلت نسخة كذا مرة في الساعة دي — جرّب تاني بعد شوية."))
        return redirect('workspace_settings')
    filename, fileobj = ws.export_workspace(tenant)
    ws._audit('other', tenant, request.user, "📦 workspace data export downloaded")
    return FileResponse(fileobj, as_attachment=True, filename=filename, content_type='application/zip')


@login_required(login_url='/login/')
def workspace_settings(request):
    tenant = getattr(request, 'tenant', None)
    if not tenant or tenant.schema_name == get_public_schema_name():
        # مالك المنصة: الشركات بتتدار من لوحة الشركات (اللينك ظاهر له في قايمة الأدمن)
        return redirect('saas_tenants_list' if request.user.is_superuser else '/')
    # 👑 مالك الشركة بس (الحساب اللي اتعمل في التسجيل superuser جوه الـ schema بتاعته).
    #    حساب متعلّم «احتيال» مايمسحش نفسه — الأدلة لازم تفضل.
    if not request.user.is_superuser or tenant.is_fraud_flagged:
        raise PermissionDenied

    error = ''
    action = request.POST.get('action', '') if request.method == 'POST' else ''
    if action == 'change_industry':
        industry, _sep, business_type = (request.POST.get('target') or '').partition(':')
        try:
            changed = ws.change_industry(tenant, industry, business_type, user=request.user)
        except ws.WorkspaceError as exc:
            error = str(exc)
        else:
            messages.success(request, _("✅ مجالك بقى «%(industry)s — %(type)s». باقيلك %(left)s مرة تغيير.") % {
                'industry': changed.get_industry_display(), 'type': changed.get_business_type_display(),
                'left': changed.industry_changes_left})
            return redirect('workspace_settings')
    elif action == 'delete_workspace':
        blocked, _wait = _throttle(f"workspace_delete:{tenant.schema_name}:{_client_ip(request)}",
                                   limit=5, window=900)
        if blocked:
            error = _("🚫 محاولات كتير. جرّب تاني بعد ربع ساعة.")
        else:
            try:
                ws.delete_workspace(tenant, user=request.user, password=request.POST.get('password', ''),
                                    confirm=request.POST.get('confirm', ''))
            except ws.WorkspaceError as exc:
                error = str(exc)
            else:
                # الـ schema اتمسحت: مفيش context processors ولا جداول للشركة — صفحة ثابتة
                # والجلسة بتتقفل من غير signals الخروج (بعضها بيكتب في جداول الشركة).
                request.session.flush()
                request.user = AnonymousUser()
                base_domain = os.getenv('BASE_DOMAIN', 'mousstec.com')
                return HttpResponse(render_to_string('clients/workspace_deleted.html', {
                    'company': tenant.name, 'home_url': f'https://{base_domain}/',
                }))

    return render(request, 'clients/workspace_settings.html', {
        'tenant': tenant,
        'change_status': ws.industry_change_status(tenant),
        'max_changes': ws.MAX_INDUSTRY_CHANGES,
        'industry_targets': _industry_targets(tenant),
        'blockers': ws.deletion_blockers(tenant),
        'confirm_slug': ws.workspace_slug(tenant),
        'error': error,
        'action': action,
    }, status=400 if error else 200)
