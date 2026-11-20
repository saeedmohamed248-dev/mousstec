"""🚪 أدوات السوبر أدمن — الدخول كمالك المنصة على أي شركة."""
from __future__ import annotations

import logging
import os
import secrets

from clients.permissions import platform_owner_required
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.csrf import csrf_exempt
from django_tenants.utils import schema_context

from clients.models import (
    Client,
    Domain,
    PlatformEvent,
)


logger = logging.getLogger('mouss_tec_core')
User = get_user_model()


# =====================================================================
# 🚪 الدخول كمالك المنصة على أي شركة (Tenant Impersonation)
# =====================================================================

@login_required
@platform_owner_required
def enter_tenant(request, schema_name):
    """
    Super Admin → يدخل على أي شركة مباشرة.
    يولّد توكن دخول مؤقت (صالح 60 ثانية) ويحول للـ subdomain.
    الـ impersonation view على الـ tenant يتحقق من التوكن ويعمل login تلقائي.
    """
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access Denied')

    tenant = get_object_or_404(Client, schema_name=schema_name)
    domain = Domain.objects.filter(tenant=tenant).first()
    if not domain:
        messages.error(request, f'لا يوجد نطاق مسجل لشركة «{tenant.name}».')
        return redirect('super_admin_dashboard')

    # --- إنشاء توكن دخول مؤقت (self-contained signed token) ---
    # ⚠️ لا نستخدم cache لأن الـ cache key function تضيف schema_name
    # فالتوكن المحفوظ على public لا يُقرأ من tenant schema.
    # بدلاً من ذلك: Django Signing — التوكن يحتوي على البيانات مشفرة.
    from django.core import signing
    import time

    token = signing.dumps({
        'schema_name': schema_name,
        'superuser_id': request.user.id,
        'superuser_name': request.user.username,
        'created': int(time.time()),
    }, salt='impersonate-login-token')

    # --- Log the impersonation ---
    PlatformEvent.objects.create(
        event_type='login',
        tenant_schema=schema_name,
        tenant_name=tenant.name,
        user_name=request.user.username,
        description=f"دخول Super Admin «{request.user.username}» على شركة «{tenant.name}»",
    )

    protocol = 'https' if request.is_secure() else 'http'
    target_url = f'{protocol}://{domain.domain}/impersonate-login/?token={token}'

    return redirect(target_url)


@csrf_exempt
def impersonate_login(request):
    """
    GET /impersonate-login/?token=xxx
    يُستدعى من الـ tenant subdomain — يتحقق من التوكن ويعمل login تلقائي كأدمن.
    """
    token = request.GET.get('token', '').strip()
    admin_url = os.getenv('ADMIN_URL', 'secure-portal')
    if not token:
        # No token — redirect to login page instead of blank forbidden
        return redirect(f'/{admin_url}/login/')

    from django.core import signing
    try:
        token_data = signing.loads(token, salt='impersonate-login-token', max_age=120)
    except (signing.BadSignature, signing.SignatureExpired):
        return redirect(f'/{admin_url}/login/?msg=token_expired')

    # 🐛 [FIX]: التوكن كان ينفع يتعاد استخدامه طول الـ 120 ثانية (لو اتسرّب من لوج/Referer/History
    # أي حد يدخل كأدمن). دلوقتي استخدام واحد بس — cache.add ذرّي (atomic) على Redis.
    import hashlib
    from django.core.cache import cache
    _used_key = 'impersonate_used:' + hashlib.sha256(token.encode()).hexdigest()
    if not cache.add(_used_key, 1, timeout=180):
        return redirect(f'/{admin_url}/login/?msg=token_expired')

    schema_name = token_data.get('schema_name', '')
    current_schema = getattr(connection, 'schema_name', 'public')

    if current_schema == 'public' or current_schema != schema_name:
        return redirect(f'/{admin_url}/login/')

    # --- إيجاد أو إنشاء admin user على هذا الـ tenant ---
    from django.contrib.auth import login as auth_login, logout as auth_logout

    # 🛡️ تنظيف الـ session الحالي قبل الدخول لمنع تسرب بيانات من session سابق
    # (السبب وراء «أحياناً بيدخل علي صفحة تانية» — session قديم لمستخدم آخر)
    if request.user.is_authenticated:
        auth_logout(request)
    request.session.flush()

    # 🎯 ابحث أولاً عن أدمن الشركة الحقيقي عن طريق إيميل الشركة (أدق من «آخر staff»)
    admin_user = None
    try:
        with schema_context('public'):
            tenant_obj = Client.objects.filter(schema_name=schema_name).first()
        tenant_email = (getattr(tenant_obj, 'email', '') or '').strip().lower()
        if tenant_email:
            admin_user = User.objects.filter(
                email__iexact=tenant_email, is_active=True
            ).first()
    except Exception:
        logger.warning(
            "enter_tenant: failed to look up tenant admin by email for schema=%s",
            schema_name, exc_info=True,
        )
        admin_user = None

    # 🔁 fallback: لو ملقيناش، استخدم أقدم superuser (مالك الشركة غالباً أول واحد)
    if not admin_user:
        admin_user = (
            User.objects.filter(is_superuser=True, is_active=True)
            .exclude(username__startswith='mousstec_admin_')
            .order_by('date_joined')
            .first()
        )

    # 🔁 fallback أخير: أقدم staff (مع استبعاد auto-created admins)
    if not admin_user:
        admin_user = (
            User.objects.filter(is_staff=True, is_active=True)
            .exclude(username__startswith='mousstec_admin_')
            .order_by('date_joined')
            .first()
        )

    if not admin_user:
        # ⚠️ Tenant ليس له أدمن حقيقي — ننشئ بمعرّف مميز عشوائي (مش اسم ثابت قابل للتخمين)
        # ونسجّل الحدث للمراقبة الأمنية.
        from django.db import connection as _conn
        admin_user = User.objects.create_superuser(
            username=f"mousstec_admin_{secrets.token_hex(4)}",
            email=f"admin+{_conn.schema_name}@mousstec.com",
            password=secrets.token_urlsafe(32),
            first_name="Mouss Tec",
            last_name="Platform Admin",
        )
        logger.warning(
            f"🔐 [IMPERSONATE]: Auto-created tenant admin '{admin_user.username}' "
            f"on schema '{_conn.schema_name}' (tenant had no staff)"
        )

    # Login
    auth_login(request, admin_user, backend='clients.backends.CaseInsensitiveEmailBackend')
    # 🎥 صاحب المنصة داخل على الشركة ⇒ مايتسجّلش في «تسجيل الشاشة» كأنه موظف عندهم
    request.session['platform_owner_entered'] = True

    admin_url = os.getenv('ADMIN_URL', 'secure-portal')
    return redirect(f'/{admin_url}/')
