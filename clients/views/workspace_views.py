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


def _safe_back(request, fallback='/'):
    from django.utils.http import url_has_allowed_host_and_scheme
    nxt = request.POST.get('next') or request.META.get('HTTP_REFERER') or fallback
    return nxt if url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}) else fallback


@login_required(login_url='/login/')
def set_business_type(request):
    """💎 «اختار نوع نشاطك» — من الكارت في الرئيسية (اتشال من فورم التسجيل). نفس المجال بس،
    ومش بيتحسب من مرات «تغيير المجال» (ده بيرتّب أقسام الرئيسية بس)."""
    tenant = _owner_tenant_or_deny(request)
    if tenant is None or request.method != 'POST':
        return redirect('/')
    bt = request.POST.get('business_type', '')
    if bt not in TenantSignupForm.INDUSTRY_BUSINESS_TYPES.get(tenant.industry, ()):
        messages.error(request, _("نوع النشاط مش من مجالك."))
        return redirect(_safe_back(request))
    Client.objects.filter(pk=tenant.pk).update(business_type=bt, business_type_confirmed=True)
    messages.success(request, _("✅ اتحفظ نوع نشاطك — الرئيسية اترتّبت عليه."))
    return redirect(_safe_back(request))


IMPORT_EXTS = {'.xlsx', '.xls', '.csv', '.ods', '.pdf', '.jpg', '.jpeg', '.png', '.heic', '.webp'}
IMPORT_MAX_MB = 15


@login_required(login_url='/login/')
def import_assist_request(request):
    """📦 «ابعتلنا ملف بضاعتك واحنا نرفعه» — من كارت «أول خطوات»؛ إيميل فوري لصاحب المنصة."""
    tenant = _owner_tenant_or_deny(request)
    if tenant is None or request.method != 'POST':
        return redirect('/')
    f = request.FILES.get('file')
    ext = os.path.splitext(getattr(f, 'name', '') or '')[1].lower()
    if f is None or ext not in IMPORT_EXTS:
        messages.error(request, _("ارفع ملف إكسيل أو صورة أو PDF."))
        return redirect(_safe_back(request))
    if f.size > IMPORT_MAX_MB * 1024 * 1024:
        messages.error(request, _("الملف أكبر من %(n)s ميجا — ابعته على كذا ملف.") % {'n': IMPORT_MAX_MB})
        return redirect(_safe_back(request))
    blocked, _wait = _throttle(f"import_assist:{tenant.schema_name}", limit=10, window=3600)
    if blocked:
        messages.error(request, _("🚫 بعت ملفات كتير في الساعة دي — جرّب تاني بعد شوية."))
        return redirect(_safe_back(request))
    from django_tenants.utils import schema_context
    from clients.models import ImportAssistRequest
    from clients.services import sales_alerts
    with schema_context(get_public_schema_name()):
        req = ImportAssistRequest.objects.create(
            tenant=tenant, file=f, original_name=f.name[:255],
            note=(request.POST.get('note') or '').strip()[:2000],
            contact_phone=(request.POST.get('phone') or tenant.phone or '').strip()[:20])
        sales_alerts.import_request(req)
    messages.success(request, _("✅ وصلنا ملفك — فريقنا هيرفع بضاعتك ويكلّمك قريب."))
    return redirect(_safe_back(request))
