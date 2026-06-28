"""
Onboarding, login routing, account recovery, and the public landing pages.
"""
from __future__ import annotations

import logging
import os
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import connection, models, transaction
from django.shortcuts import redirect, render
from django.utils.text import slugify
from django_tenants.utils import schema_context

from clients.forms import TenantSignupForm
from clients.models import Client, DesignPackage, Domain

logger = logging.getLogger('mouss_tec_core')
User = get_user_model()
ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal')


# =====================================================================
# 🛡️ Auth security helpers (rate-limit, throttle, client IP)
# =====================================================================
def _client_ip(request) -> str:
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def _with_request_port(domain: str, request) -> str:
    """Carry the incoming request's non-standard port onto a tenant domain.

    The Domain model stores the bare host (``shop.localhost``) with no port.
    In production the request arrives on 80/443 so nothing changes. But in
    local dev the runserver is on :8000, and a cross-subdomain auto-login
    redirect to the bare host lands on port 80 → ERR_CONNECTION_REFUSED.
    We append the current request's port only when it's non-standard and the
    target domain doesn't already carry one. Production is untouched.
    """
    if ':' in domain:
        return domain
    host = request.get_host()  # e.g. "auto-garage-test.localhost:8000"
    port = host.rsplit(':', 1)[1] if ':' in host else ''
    if port and port not in ('80', '443'):
        return f"{domain}:{port}"
    return domain


def _throttle(key: str, limit: int, window: int) -> tuple[bool, int]:
    """Cache-backed sliding-window throttle.

    Returns (blocked, retry_after_seconds). Uses `cache.add` + `incr` so it
    is atomic across workers on Redis/Memcached. Falls back to a coarse
    counter if the backend doesn't support incr.
    """
    try:
        added = cache.add(key, 1, timeout=window)
        if added:
            return False, 0
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=window)
            return False, 0
        if count > limit:
            return True, window
        return False, 0
    except Exception:
        # Never let cache backend take down the login flow.
        return False, 0


# =====================================================================
# 🏢 محرك التخليق الآلي للمؤسسات المعزولة (Automated Onboarding Engine)
# =====================================================================
def register_new_tenant_saas(request):
    """
    محرك التأسيس السحابي (SaaS Onboarding Engine) مزود بنواة ضخ البيانات الذكية (Smart Seeding).
    """
    if request.method == 'POST':
        # 🛡️ IP throttle: 5 signups / hour / IP — يمنع بناء tenants عشوائية.
        ip = _client_ip(request)
        blocked, _ = _throttle(f"signup_ip:{ip}", limit=5, window=3600)
        if blocked:
            form = TenantSignupForm(request.POST)
            form.add_error(None, "🚫 محاولات تسجيل كثيرة من نفس الجهاز. حاول بعد ساعة.")
            return render(request, 'clients/signup_register.html', {'form': form})

        form = TenantSignupForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            company_name = data['company_name']
            industry = data.get('industry', 'automotive')
            business_type = data.get('business_type', 'service_center')

            subdomain_slug = slugify(company_name).replace('-', '_')
            if not subdomain_slug:
                subdomain_slug = f"mt_{secrets.token_hex(3)}"
            if subdomain_slug[0].isdigit():
                subdomain_slug = f"tenant_{subdomain_slug}"

            schema_name = subdomain_slug
            success = False
            attempts = 0

            while not success and attempts < 10:
                try:
                    with transaction.atomic():
                        # 🎁 خلال التجربة بنديهم أعلى باقة (empire / print_enterprise)
                        # عشان يجرّبوا كل features. لما يدفعوا، بيختاروا الباقة اللي تناسبهم.
                        default_plan = 'print_enterprise' if industry == 'printing' else 'empire'

                        tenant = Client.objects.create(
                            schema_name=schema_name,
                            name=company_name,
                            owner_name=data.get('full_name', company_name),
                            email=data['email'],
                            phone=data.get('phone', ''),
                            industry=industry,
                            business_type=business_type,
                            plan=default_plan,
                            # 🔒 Email-verification gate: لازم العميل يأكد إيميله
                            # قبل ما الـ tenant يبقى فعّال (مطابق لـ Stripe/Slack).
                            is_active=False,
                        )

                        with schema_context(schema_name):
                            # 🐛 [FIX]: full_name قد يجي فاضي بعد strip — نمنع IndexError.
                            full_name_safe = (data.get('full_name') or company_name).strip()
                            name_parts = full_name_safe.split(' ', 1) if full_name_safe else [company_name]
                            admin_user, created = User.objects.get_or_create(
                                username=data['email'],
                                defaults={
                                    'email': data['email'],
                                    'first_name': name_parts[0],
                                    'last_name': name_parts[1] if len(name_parts) > 1 else '',
                                    'is_staff': True,
                                    'is_superuser': True,
                                },
                            )
                            admin_user.set_password(data['password'])
                            admin_user.first_name = name_parts[0]
                            admin_user.last_name = name_parts[1] if len(name_parts) > 1 else ''
                            admin_user.is_staff = True
                            admin_user.is_superuser = True
                            admin_user.save()

                            try:
                                # 👤 مالك الشركة لازم يبقى role='admin' بصلاحيات كاملة.
                                # الـ inventory signal (post_save على User) بيـ auto-create
                                # EmployeeProfile بـ role='cashier' الافتراضي قبل ما نوصل
                                # هنا، فـ get_or_create يرجع الموجود من غير ما يحدّث.
                                # update_or_create يضمن الـ role الصح حتى لو موجود مسبقاً.
                                from inventory.models import EmployeeProfile, Branch
                                branch = Branch.objects.filter(name="الفرع الرئيسي").first()
                                EmployeeProfile.objects.update_or_create(
                                    user=admin_user,
                                    defaults={
                                        'role': 'admin',
                                        'branch': branch,
                                        'can_edit_posted_invoices': True,
                                    },
                                )
                            except Exception as profile_err:
                                # لو table الـ EmployeeProfile مش موجود (مطابع) — تجاهل بصمت
                                logger.warning(f"⚠️ [SIGNUP]: EmployeeProfile setup skipped — {profile_err}")

                        base_domain = os.getenv('BASE_DOMAIN', 'mousstec.com')
                        url_safe_slug = schema_name.replace('_', '-')
                        Domain.objects.get_or_create(
                            domain=f"{url_safe_slug}.{base_domain}",
                            defaults={'tenant': tenant, 'is_primary': True},
                        )

                        # 💳 سجل اشتراك ابتدائي — لازم يتعمل من signup عشان يظهر في
                        # لوحة الإدارة الذكية (TenantSubscription admin)، وعلشان نظام
                        # الفوترة و الـ entitlements يقدر يقرأ منه. غير مفعّل افتراضياً
                        # — الأدمن بيفعّل الباقة يدوياً أو لما العميل يدفع.
                        # 🎁 [Trial Fix]: لازم نـ link الـ Plan الفعلي عشان
                        # effective_entitlements يرجّع features الـ empire/enterprise
                        # خلال التجربة. بدون كده الـ EntitlementService.has() = False
                        # على كل feature → العميل يلاقي كل حاجة مقفولة.
                        from clients.models import TenantSubscription, Plan
                        from clients.services.plan_mapping import resolve_plan_slug
                        trial_plan_slug = resolve_plan_slug(default_plan)
                        trial_plan_obj = (
                            Plan.objects.filter(slug=trial_plan_slug, is_active=True).first()
                            if trial_plan_slug else None
                        )
                        TenantSubscription.objects.get_or_create(
                            tenant=tenant,
                            defaults={
                                'plan': trial_plan_obj,
                                'is_active': False,
                            },
                        )

                    success = True

                except Exception as e:
                    if "already exists" in str(e).lower() or "unique constraint" in str(e).lower():
                        attempts += 1
                        schema_name = f"{subdomain_slug}_{secrets.token_hex(2)}"
                    else:
                        logger.error(f"🔴 [SaaS PROVISIONING CRASH]: {str(e)}")
                        form.add_error(None, "🛑 عذراً، تعذر بناء مساحة العمل. يرجى المحاولة لاحقاً.")
                        return render(request, 'clients/signup_register.html', {'form': form})

            if success:
                url_safe_final = schema_name.replace('_', '-')
                base_domain = os.getenv('BASE_DOMAIN', 'mousstec.com')

                # 🔒 Email verification gate:
                # نـ generate signed token (max-age 48h) و نبعت لينك تأكيد للإيميل.
                # لما يضغط، الـ tenant بيتفعّل (is_active=True) ويعمل auto-login.
                from django.core import signing
                import time
                verify_token = signing.dumps({
                    'schema_name': schema_name,
                    'user_id': admin_user.id,
                    'email': data['email'],
                    'created': int(time.time()),
                }, salt='tenant-email-verify')

                # Build verify URL on the public domain
                public_host = request.get_host()
                verify_url = f"{request.scheme}://{public_host}/account/verify-email/?token={verify_token}"

                # Send email — fail-silently (we still show the user the link page)
                # 🐛 [FIX]: لازم EmailMessage مع encoding='utf-8' عشان SMTP يبعت
                # subject/body عربي. send_mail لوحدها كانت بترمي
                # 'ascii' codec can't encode على الـ SMTP transmission.
                try:
                    from django.core.mail import EmailMessage
                    msg = EmailMessage(
                        subject='✉️ أكّد بريدك الإلكتروني | Mouss Tec',
                        body=(
                            f"أهلاً {data['full_name']}،\n\n"
                            f"اضغط الرابط لتفعيل حساب {company_name}:\n\n"
                            f"{verify_url}\n\n"
                            f"الرابط صالح لمدة 48 ساعة.\n\nMouss Tec"
                        ),
                        from_email=None,
                        to=[data['email']],
                    )
                    msg.encoding = 'utf-8'
                    msg.send(fail_silently=True)
                except Exception as e:
                    logger.warning(f"[SIGNUP] verification email failed: {e}")

                display_url = f"https://{url_safe_final}.{base_domain}/{ADMIN_URL}/"
                return render(request, 'clients/signup_success.html', {
                    'company_name': company_name,
                    'target_url': verify_url,
                    'display_url': display_url,
                    'admin_email': data['email'],
                    'verification_pending': True,
                })
            else:
                form.add_error(None, "🛑 فشل التأسيس: الأسماء مقفلة، جرب اسماً مختلفاً.")
    else:
        initial_industry = request.GET.get('industry', 'automotive')
        if initial_industry not in ('automotive', 'printing'):
            initial_industry = 'automotive'
        default_btype = 'service_center' if initial_industry == 'automotive' else 'print_shop'
        form = TenantSignupForm(initial={
            'industry': initial_industry,
            'business_type': default_btype,
        })

    return render(request, 'clients/signup_register.html', {'form': form})


# =====================================================================
# 🌍 Smart post-login redirect
# =====================================================================
@login_required(login_url='/secure-portal/login/')
def smart_post_login_redirect(request):
    """Universal post-login landing for tenant users.

    Per business requirement (Week 4 polish): EVERY tenant user — regardless
    of role (admin, manager, sales, cashier, accountant, stock, tech, engineer,
    hr) — lands on `/system/dashboard/` after login. The dashboard itself
    surfaces only role-appropriate KPIs / tiles via strict template-level
    `{% if %}` gates. This removes the previous role-based workspace splits.

    Precedence:
      1. Superuser on public schema → /superadmin/
      2. Tenant (printing industry)  → admin URL (sector divergence preserved)
      3. ANY tenant user (automotive) → /system/dashboard/
      4. Public schema, non-admin     → /login/
    """
    tenant = getattr(request, 'tenant', None)
    schema = getattr(connection, 'schema_name', 'public')

    if request.user.is_superuser and schema == 'public':
        return redirect('/superadmin/')

    if tenant and schema != 'public':
        # Printing tenants live in a different sector entirely — keep them
        # on their existing admin landing.
        if getattr(tenant, 'industry', 'automotive') == 'printing':
            admin_url = os.getenv('ADMIN_URL', 'secure-portal')
            # 🆕 Role-based fast landing — if the user is a Designer
            # (has an Employee + Designer profile or HR Employee w/ designer
            # role), route them straight to /hr/designer/ for the fast
            # dashboard instead of the heavy admin index.
            try:
                from hr.models import Employee
                emp = Employee.objects.filter(user=request.user, is_active=True).first()
                if emp is not None and not request.user.is_superuser:
                    # المصمم → اللوحة السريعة
                    if emp.department == 'design':
                        return redirect('/hr/designer/')
                    # HR Manager أو قسم HR → لوحة المدير
                    if emp.is_hr_manager or emp.department == 'hr':
                        return redirect('/hr/manager/')
                    # الباقي (printing/sales/accounting) → الـ admin
                    # (مستقبلاً ممكن نعمل لوحات لكل قسم)
            except Exception:
                # Don't break login if HR module is unavailable — fall through
                pass
            return redirect(f'/{admin_url}/')

        # 🎯 [Unified Landing Policy] All automotive-tenant users land here.
        # No more per-role workspace branching — the dashboard handles RBAC.
        return redirect('/system/dashboard/')

    return redirect('/login/')


def _find_user_across_tenants(identifier: str):
    """
    يدوّر على كل tenant active/trial ويلاقي User بنفس الإيميل (أو username).
    بيرجع dict {'tenant': Client, 'user_id': int} أو None.

    🚀 تحسين: نتذكر الـ mapping (email → tenant) في الكاش لمدة 12 ساعة
    عشان نوقف الـ O(N tenants) loop في كل محاولة دخول. الكاش يبقى صالح
    طول ما الـ user مش متنقل بين tenants — وهو نادر جداً.
    """
    cache_key = f"login_lookup:{identifier}"
    cached = cache.get(cache_key)
    if cached:
        try:
            t = Client.objects.filter(
                schema_name=cached['schema_name'], status__in=['active', 'trial']
            ).only('id', 'schema_name', 'name', 'status').first()
            if t:
                with schema_context(t.schema_name):
                    if User.objects.filter(pk=cached['user_id'], is_active=True).exists():
                        return {'tenant': t, 'user_id': cached['user_id']}
        except Exception:
            pass
        cache.delete(cache_key)

    tenants = (
        Client.objects.exclude(schema_name='public')
        .filter(status__in=['active', 'trial'])
        .only('id', 'schema_name', 'name', 'status')
    )
    for t in tenants:
        try:
            with schema_context(t.schema_name):
                u = (
                    User.objects.filter(email__iexact=identifier, is_active=True).first()
                    or User.objects.filter(username__iexact=identifier, is_active=True).first()
                )
                if u:
                    cache.set(cache_key, {'schema_name': t.schema_name, 'user_id': u.id}, timeout=43200)
                    return {'tenant': t, 'user_id': u.id}
        except Exception:
            continue
    return None


def _build_login_url_for_tenant(request, tenant) -> str | None:
    """يبني الـ URL الكامل لصفحة login شركة معينة (يستخدم Domain أو fallback subdomain)."""
    domain = Domain.objects.filter(tenant=tenant).first()
    if domain:
        host = domain.domain
    else:
        safe_slug = tenant.schema_name.replace('_', '-')
        request_host = request.get_host()
        host_parts = request_host.split('.')
        base_host = '.'.join(host_parts[-2:]) if len(host_parts) > 2 else request_host
        host = f"{safe_slug}.{base_host}"
    return f"{request.scheme}://{host}/{ADMIN_URL}/login/"


def client_login_finder(request):
    """
    صفحة دخول موحّدة بأسلوب Odoo:
    - يدخل المستخدم email + password → النظام يلاقي شركته ويسجّل دخوله مباشرة
      عبر redirect موقّع للـ subdomain (auto-login).
    - لو ادخل phone/email بدون password → fallback للسلوك القديم (يلاقي الشركة
      ويعرضله رابط دخولها).
    """
    error = None
    if request.method == 'POST':
        identifier = request.POST.get('email', '').strip().lower()
        # 🐛 [FIX]: ما نـ strip() الـ password — لو فيه مسافة في الآخر مقصودة
        # هنكسر الدخول. Django check_password بيقارن byte-by-byte.
        password = request.POST.get('password', '')

        if not identifier:
            return render(request, 'clients/login_finder.html', {'error': 'أدخل البريد أو رقم الموبايل'})

        # 🛡️ Rate-limit: 10 محاولات / 5 دقايق لكل IP + 5 لكل email
        # يحمي من brute-force على الـ universal login.
        ip = _client_ip(request)
        ip_blocked, _ = _throttle(f"login_ip:{ip}", limit=10, window=300)
        email_blocked, _ = _throttle(f"login_email:{identifier}", limit=5, window=300)
        if ip_blocked or email_blocked:
            return render(request, 'clients/login_finder.html', {
                'error': '🚫 محاولات دخول كثيرة. حاول بعد 5 دقايق أو استرجع حسابك.',
                'last_email': identifier,
            })

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # المسار الأساسي (Odoo-style): email + password → دخول تلقائي
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if password:
            match = _find_user_across_tenants(identifier)
            if match:
                tenant = match['tenant']
                user_id = match['user_id']

                # تحقّق من كلمة السر داخل schema الشركة
                password_ok = False
                with schema_context(tenant.schema_name):
                    try:
                        u = User.objects.get(pk=user_id, is_active=True)
                        password_ok = u.check_password(password)
                    except User.DoesNotExist:
                        password_ok = False

                if not password_ok:
                    return render(request, 'clients/login_finder.html', {
                        'error': 'كلمة السر غير صحيحة. حاول مرة أخرى أو استرجع حسابك.',
                        'last_email': identifier,
                    })

                # 🔐 MFA challenge — لو المستخدم مفعّل 2FA، نوجّهه للـ challenge
                # قبل ما نديله auto-login token. السيكرت مش بيخرج من schema الـ tenant.
                mfa_required = False
                try:
                    with schema_context(tenant.schema_name):
                        from inventory.models import UserMFA
                        mfa_required = UserMFA.objects.filter(
                            user_id=user_id, is_enabled=True
                        ).exists()
                except Exception:
                    mfa_required = False

                if mfa_required:
                    next_url_raw = (request.POST.get('next') or request.GET.get('next') or '').strip()
                    if not next_url_raw.startswith('/') or next_url_raw.startswith('//'):
                        next_url_raw = ''
                    from clients.views.mfa_views import issue_mfa_challenge
                    chal_token = issue_mfa_challenge(tenant, user_id, next_url_raw)
                    return redirect(f'/account/mfa/challenge/?token={chal_token}')

                # 🔒 Email-verification gate — يمنع دخول tenants لم تُؤكَّد
                # بعد. نعرض زرّ resend بدل ما نسيب المستخدم تايه.
                if not tenant.is_active:
                    return render(request, 'clients/login_finder.html', {
                        'error': 'إيميلك لسه مش متفعّل. ابعت الرابط من جديد.',
                        'last_email': identifier,
                        'verification_required': True,
                        'verify_email': identifier,
                    })

                # ✅ توقيع توكن دخول مؤقت (120 ثانية) + redirect للـ subdomain
                from django.core import signing
                import time
                # 🐛 [Bug #2 FIX] Preserve ?next=… across the cross-tenant
                # login round-trip. Without this, every click on a tile from
                # an expired session sends the user to /system/dashboard/
                # instead of their intended target — felt like a "logout".
                next_url = (
                    request.POST.get('next')
                    or request.GET.get('next')
                    or ''
                ).strip()
                # Only honor safe same-host paths to block open-redirect abuse.
                if not next_url.startswith('/') or next_url.startswith('//'):
                    next_url = ''
                token = signing.dumps({
                    'schema_name': tenant.schema_name,
                    'user_id': user_id,
                    'created': int(time.time()),
                    'next': next_url,
                }, salt='tenant-auto-login-token')

                domain = Domain.objects.filter(tenant=tenant).first()
                if not domain:
                    return render(request, 'clients/login_finder.html', {
                        'error': 'خطأ في إعدادات نطاق الشركة. اتصل بالدعم.',
                    })
                protocol = 'https' if request.is_secure() else request.scheme
                host = _with_request_port(domain.domain, request)
                target_url = f"{protocol}://{host}/auto-login/?token={token}"
                return redirect(target_url)

            # مفيش user بنفس الإيميل في أي شركة — هاتي رسالة موحّدة
            return render(request, 'clients/login_finder.html', {
                'error': 'لا يوجد حساب بهذا البريد أو كلمة السر غير صحيحة.',
                'last_email': identifier,
            })

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # Fallback (بدون password): البحث القديم بإيميل/موبايل الشركة
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        tenant = Client.objects.filter(email__iexact=identifier).exclude(schema_name='public').first()
        if not tenant:
            tenant = Client.objects.filter(phone=identifier).exclude(schema_name='public').first()
        if not tenant:
            tenant = Client.objects.filter(
                models.Q(name__icontains=identifier) | models.Q(schema_name=identifier)
            ).exclude(schema_name='public').first()

        if tenant:
            return render(request, 'clients/login_finder.html', {
                'found_tenant': tenant,
                'login_url': _build_login_url_for_tenant(request, tenant),
            })

        error = "لا يوجد حساب مرتبط بهذا البريد أو رقم الموبايل. تأكد من البيانات أو أنشئ حساباً جديداً."
    return render(request, 'clients/login_finder.html', {'error': error})


# =====================================================================
# 🚪 Auto-Login على الـ Tenant Subdomain (نتيجة universal login من public)
# =====================================================================
def tenant_auto_login(request):
    """
    GET /auto-login/?token=xxx
    يُستدعى من الـ subdomain بعد ما المستخدم نجح في universal login على
    الـ public. يتحقق من التوكن ويسجّل دخول المستخدم الفعلي (مش admin
    impersonation) ثم يوجّهه للوحة المناسبة.
    """
    from django.contrib.auth import login as auth_login, logout as auth_logout
    from django.core import signing

    token = request.GET.get('token', '').strip()
    if not token:
        return redirect(f'/{ADMIN_URL}/login/')

    try:
        data = signing.loads(token, salt='tenant-auto-login-token', max_age=120)
    except (signing.BadSignature, signing.SignatureExpired):
        return redirect(f'/{ADMIN_URL}/login/?msg=token_expired')

    schema_name = data.get('schema_name')
    user_id = data.get('user_id')
    current_schema = getattr(connection, 'schema_name', 'public')

    # لازم نكون على الـ subdomain الصحيح
    if not schema_name or current_schema == 'public' or current_schema != schema_name:
        return redirect(f'/{ADMIN_URL}/login/')

    # نظّف الـ session قبل الدخول (نفس منطق impersonate_login لمنع تسرّب جلسة)
    if request.user.is_authenticated:
        auth_logout(request)
    request.session.flush()

    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return redirect(f'/{ADMIN_URL}/login/')

    auth_login(request, user, backend='clients.backends.CaseInsensitiveEmailBackend')

    # 🐛 [Bug #2 FIX] Honor the `next` URL the user originally clicked.
    # Safe-path validation already happened at sign time, but defence in depth.
    next_url = (data.get('next') or '').strip()
    if next_url.startswith('/') and not next_url.startswith('//'):
        return redirect(next_url)

    # توجيه ذكي: staff → admin، عادي → dashboard
    if user.is_staff or user.is_superuser:
        return redirect(f'/{ADMIN_URL}/')
    return redirect('/system/dashboard/')


# =====================================================================
# 📰 Landing pages
# =====================================================================
def mousstec_landing_page(request):
    return render(request, 'clients/landing.html')


def automotive_landing_page(request):
    """صفحة تعريفية كاملة بقطاع السيارات — مميزات، أسعار، وطريقة التسجيل"""
    return render(request, 'clients/auto_landing.html')


def printing_landing_page(request):
    """صفحة تعريفية كاملة بقطاع المطابع والتصميم.

    كروت باقات التصاميم بتيجي من DesignPackage model، مفصولة بـ target_audience:
      • customer_packages → باقات الأفراد (الجزء العلوي)
      • designer_packages → باقات المصممين والاستوديوهات (الجزء السفلي)
    أي تعديل من Super Admin يظهر فوراً — مفيش hardcoded.
    """
    base_qs = DesignPackage.objects.filter(is_active=True).order_by('sort_order', 'designs_count')

    customer_packages = list(base_qs.filter(target_audience='customer'))
    designer_packages = list(base_qs.filter(target_audience='designer'))

    # Featured fallback: لو مفيش is_featured متعبّى نعتبر التاني من ضمن 3 أو الـ middle = الأكثر طلباً
    def _mark_popular(pkgs):
        if not pkgs:
            return
        if any(p.is_featured for p in pkgs):
            return  # عند الأدمن control كامل
        if len(pkgs) >= 3:
            pkgs[len(pkgs) // 2].is_featured = True  # in-memory only

    _mark_popular(customer_packages)
    _mark_popular(designer_packages)

    return render(request, 'clients/print_landing.html', {
        'customer_packages': customer_packages,
        'designer_packages': designer_packages,
    })


# =====================================================================
# 🔑 استرجاع كلمة السر / العثور على الحساب (Password Recovery)
# =====================================================================
def account_recovery(request):
    """
    نظام استرجاع الحساب متعدد الخطوات:
    الخطوة 1: البحث بالموبايل أو الإيميل → عرض الحساب
    الخطوة 2: إرسال كود تحقق (OTP) عبر الإيميل
    الخطوة 3: إعادة تعيين كلمة السر
    """
    context = {'step': 'search'}

    if request.method == 'POST':
        step = request.POST.get('step', 'search')

        if step == 'search':
            query = request.POST.get('query', '').strip()
            if not query:
                context['error'] = 'أدخل رقم الموبايل أو البريد الإلكتروني'
                return render(request, 'clients/account_recovery.html', context)

            # 🛡️ Rate-limit OTP send: 3 طلبات / 10 دقايق / IP + 3 / tenant.
            ip = _client_ip(request)
            ip_blocked, _ = _throttle(f"recovery_ip:{ip}", limit=3, window=600)
            if ip_blocked:
                context['error'] = '🚫 طلبات استرجاع كثيرة. حاول بعد 10 دقايق.'
                return render(request, 'clients/account_recovery.html', context)

            tenant = None
            matched_user = None

            tenant = Client.objects.filter(phone=query).exclude(schema_name='public').first()

            if not tenant:
                tenant = Client.objects.filter(email__iexact=query).exclude(schema_name='public').first()

            if not tenant:
                tenant = Client.objects.filter(schema_name=query).exclude(schema_name='public').first()

            if not tenant:
                tenant = Client.objects.filter(name__icontains=query).exclude(schema_name='public').first()

            if not tenant:
                context['error'] = 'لا يوجد حساب مرتبط بهذا الرقم أو البريد. تأكد من البيانات أو أنشئ حساباً جديداً.'
                return render(request, 'clients/account_recovery.html', context)

            # 🛡️ Resend cooldown: لو فيه OTP فعّال للـ tenant خلال آخر 60ث،
            # ما نـ regenerate-ش (يمنع flood للإيميل + خداع OTP enumeration).
            cache_key = f"recovery_otp_{tenant.schema_name}"
            existing_otp = cache.get(cache_key)
            if existing_otp:
                otp_code = existing_otp
            else:
                otp_code = f"{secrets.randbelow(900000) + 100000}"
                cache.set(cache_key, otp_code, timeout=600)
                # reset attempt counter for new OTP
                cache.delete(f"recovery_otp_attempts_{tenant.schema_name}")

            # 🚀 Auto-resolve المستخدم الفعلي (owner أولاً ثم superuser ثم أول
            # نشط) — بدل ما نعرض list of users في صفحة الـ reset (privacy leak).
            matched_user = None
            try:
                with schema_context(tenant.schema_name):
                    matched_user = (
                        User.objects.filter(email__iexact=tenant.email, is_active=True).first()
                        or User.objects.filter(email__iexact=query, is_active=True).first()
                        or User.objects.filter(is_superuser=True, is_active=True).order_by('id').first()
                        or User.objects.filter(is_active=True).order_by('id').first()
                    )
            except Exception:
                matched_user = None
            if matched_user:
                cache.set(
                    f"recovery_target_user_{tenant.schema_name}",
                    matched_user.id, timeout=900,
                )

            recovery_email = tenant.email
            if not recovery_email and matched_user:
                recovery_email = matched_user.email

            email_sent = False
            if recovery_email and getattr(settings, 'EMAIL_HOST', ''):
                try:
                    # 🐛 UTF-8 encoding عشان الـ subject/body عربي يوصل صح عبر SMTP
                    from django.core.mail import EmailMessage
                    msg = EmailMessage(
                        subject='كود استرجاع حسابك | Mouss Tec',
                        body=f'كود التحقق الخاص بك هو: {otp_code}\n\nصالح لمدة 10 دقائق.\n\nMouss Tec',
                        from_email=None,
                        to=[recovery_email],
                    )
                    msg.encoding = 'utf-8'
                    msg.send(fail_silently=True)
                    email_sent = True
                except Exception as e:
                    logger.warning(f"[RECOVERY] Failed to send OTP email: {e}")

            masked_email = ''
            if recovery_email:
                parts = recovery_email.split('@')
                if len(parts) == 2:
                    name = parts[0]
                    masked_name = name[:2] + '***' + (name[-1] if len(name) > 2 else '')
                    masked_email = f"{masked_name}@{parts[1]}"

            context = {
                'step': 'verify',
                'tenant_name': tenant.name,
                'tenant_schema': tenant.schema_name,
                'masked_email': masked_email if email_sent else '',
                'otp_hint': '',
                'email_sent': email_sent,
            }
            return render(request, 'clients/account_recovery.html', context)

        elif step == 'verify':
            schema_name = request.POST.get('tenant_schema', '')
            otp_input = request.POST.get('otp', '').strip()
            cache_key = f"recovery_otp_{schema_name}"
            correct_otp = cache.get(cache_key)

            tenant = Client.objects.filter(schema_name=schema_name).first()
            if not tenant:
                context['error'] = 'خطأ في البيانات. حاول مرة أخرى.'
                return render(request, 'clients/account_recovery.html', context)

            # 🛡️ Attempt counter — 5 محاولات max لكل OTP، بعدها نـ invalidate.
            attempts_key = f"recovery_otp_attempts_{schema_name}"
            attempts = cache.get(attempts_key, 0)
            if attempts >= 5:
                cache.delete(cache_key)
                cache.delete(attempts_key)
                context = {
                    'step': 'search',
                    'error': '🚫 محاولات تحقق كثيرة. اطلب كود جديد.',
                }
                return render(request, 'clients/account_recovery.html', context)

            if not correct_otp or otp_input != correct_otp:
                cache.set(attempts_key, attempts + 1, timeout=600)
                context = {
                    'step': 'verify',
                    'tenant_name': tenant.name,
                    'tenant_schema': schema_name,
                    'error': f'كود التحقق خاطئ. متبقي {4 - attempts} محاولات.',
                }
                return render(request, 'clients/account_recovery.html', context)

            reset_token = secrets.token_urlsafe(32)
            cache.set(f"recovery_reset_{schema_name}", reset_token, timeout=600)
            cache.delete(cache_key)
            cache.delete(attempts_key)

            # 🔒 Privacy: ما نـ expose قائمة المستخدمين. نـ resolve المستخدم
            # المحدّد من خطوة الـ search (auto-pick: owner/superuser/أول نشط).
            target_user_id = cache.get(f"recovery_target_user_{schema_name}")
            target_username = ''
            if target_user_id:
                try:
                    with schema_context(schema_name):
                        u = User.objects.get(pk=target_user_id, is_active=True)
                        target_username = u.get_full_name() or u.username
                except Exception:
                    target_user_id = None

            if not target_user_id:
                context = {
                    'step': 'search',
                    'error': 'تعذّر تحديد الحساب. ابدأ من جديد.',
                }
                return render(request, 'clients/account_recovery.html', context)

            context = {
                'step': 'reset',
                'tenant_name': tenant.name,
                'tenant_schema': schema_name,
                'reset_token': reset_token,
                'target_user_id': target_user_id,
                'target_username': target_username,
            }
            return render(request, 'clients/account_recovery.html', context)

        elif step == 'reset':
            schema_name = request.POST.get('tenant_schema', '')
            reset_token = request.POST.get('reset_token', '')
            # 🔒 لا نثق بـ user_id من الفورم — نقرأه من الكاش المرتبط بالـ OTP
            # المعتمد. يمنع المهاجم من إعادة تعيين كلمة سر أي مستخدم بمجرد
            # امتلاك reset_token صالح.
            user_id = cache.get(f"recovery_target_user_{schema_name}")
            new_password = request.POST.get('new_password', '')
            confirm_password = request.POST.get('confirm_password', '')

            correct_token = cache.get(f"recovery_reset_{schema_name}")
            if not correct_token or reset_token != correct_token:
                context['error'] = 'انتهت صلاحية الجلسة. ابدأ من جديد.'
                return render(request, 'clients/account_recovery.html', context)

            # 🔒 موحّد: نفس validators بتاعت signup + change-password
            from django.contrib.auth.password_validation import validate_password
            from django.core.exceptions import ValidationError as _VE
            try:
                validate_password(new_password)
            except _VE as e:
                tenant = Client.objects.filter(schema_name=schema_name).first()
                context = {
                    'step': 'reset',
                    'tenant_name': tenant.name if tenant else '',
                    'tenant_schema': schema_name,
                    'reset_token': reset_token,
                    'error': ' • '.join(e.messages),
                }
                return render(request, 'clients/account_recovery.html', context)

            if new_password != confirm_password:
                tenant = Client.objects.filter(schema_name=schema_name).first()
                context = {
                    'step': 'reset',
                    'tenant_name': tenant.name if tenant else '',
                    'tenant_schema': schema_name,
                    'reset_token': reset_token,
                    'error': 'كلمة السر وتأكيدها غير متطابقتين.',
                }
                return render(request, 'clients/account_recovery.html', context)

            if not user_id:
                context['error'] = 'انتهت صلاحية الجلسة. ابدأ من جديد.'
                return render(request, 'clients/account_recovery.html', context)

            try:
                with schema_context(schema_name):
                    user = User.objects.get(id=user_id)
                    user.set_password(new_password)
                    user.save()
                    # 🔒 إبطال كل sessions القديمة للمستخدم بعد إعادة التعيين.
                    try:
                        from django.contrib.sessions.models import Session
                        from django.utils import timezone
                        now = timezone.now()
                        for s in Session.objects.filter(expire_date__gt=now):
                            data = s.get_decoded()
                            if str(data.get('_auth_user_id', '')) == str(user.id):
                                s.delete()
                    except Exception as sess_err:
                        logger.warning(f"[RECOVERY] session purge skipped: {sess_err}")

                cache.delete(f"recovery_reset_{schema_name}")
                cache.delete(f"recovery_target_user_{schema_name}")
                # invalidate the email→tenant memoize for safety
                if user.email:
                    cache.delete(f"login_lookup:{user.email.lower()}")

                tenant = Client.objects.filter(schema_name=schema_name).first()
                safe_slug = schema_name.replace('_', '-')
                request_host = request.get_host()
                host_parts = request_host.split('.')
                base_host = '.'.join(host_parts[-2:]) if len(host_parts) > 2 else request_host
                login_url = f"{request.scheme}://{safe_slug}.{base_host}/{ADMIN_URL}/login/"

                context = {
                    'step': 'success',
                    'tenant_name': tenant.name if tenant else '',
                    'login_url': login_url,
                    'username': user.username,
                }
                return render(request, 'clients/account_recovery.html', context)

            except User.DoesNotExist:
                context['error'] = 'المستخدم غير موجود.'
            except Exception as e:
                logger.error(f"[RECOVERY] Password reset failed: {e}")
                context['error'] = 'حدث خطأ. حاول مرة أخرى.'

    return render(request, 'clients/account_recovery.html', context)


# =====================================================================
# 🔐 تغيير كلمة السر للمستخدم المسجل دخوله (Logged-in Password Change)
# =====================================================================
@login_required
def change_password(request):
    """
    تغيير كلمة سر المستخدم نفسه. يتطلب كلمة السر القديمة (Re-auth) لمنع
    الاستيلاء على حساب لو فضل المستخدم سايب جهازه مفتوح. بعد التغيير:
      • update_session_auth_hash يحافظ على جلسة المستخدم الحالي.
      • نـ purge كل sessions تانية للمستخدم (logout other devices).
    """
    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    ctx = {}
    if request.method == 'POST':
        old_pw = request.POST.get('old_password', '')
        new_pw = request.POST.get('new_password', '')
        confirm = request.POST.get('confirm_password', '')

        # 🛡️ Throttle: 5 محاولات / 15 دقيقة (يمنع تخمين كلمة السر القديمة)
        ip = _client_ip(request)
        blocked, _ = _throttle(f"chpw:{request.user.id}:{ip}", limit=5, window=900)
        if blocked:
            ctx['error'] = '🚫 محاولات كثيرة. حاول بعد 15 دقيقة.'
            return render(request, 'clients/change_password.html', ctx)

        if not request.user.check_password(old_pw):
            ctx['error'] = 'كلمة السر الحالية غير صحيحة.'
            return render(request, 'clients/change_password.html', ctx)

        if new_pw != confirm:
            ctx['error'] = 'كلمة السر الجديدة وتأكيدها غير متطابقتين.'
            return render(request, 'clients/change_password.html', ctx)

        if old_pw and new_pw == old_pw:
            ctx['error'] = 'كلمة السر الجديدة لازم تختلف عن القديمة.'
            return render(request, 'clients/change_password.html', ctx)

        try:
            validate_password(new_pw, user=request.user)
        except ValidationError as e:
            ctx['error'] = ' • '.join(e.messages)
            return render(request, 'clients/change_password.html', ctx)

        request.user.set_password(new_pw)
        request.user.save()
        update_session_auth_hash(request, request.user)

        # 🔒 Logout other sessions
        try:
            from django.contrib.sessions.models import Session
            from django.utils import timezone
            current_key = request.session.session_key
            now = timezone.now()
            for s in Session.objects.filter(expire_date__gt=now):
                if s.session_key == current_key:
                    continue
                data = s.get_decoded()
                if str(data.get('_auth_user_id', '')) == str(request.user.id):
                    s.delete()
        except Exception as sess_err:
            logger.warning(f"[CHPW] session purge skipped: {sess_err}")

        if request.user.email:
            cache.delete(f"login_lookup:{request.user.email.lower()}")

        ctx['success'] = '✅ تم تغيير كلمة السر بنجاح. باقي الأجهزة اتعملها logout.'

    return render(request, 'clients/change_password.html', ctx)


# =====================================================================
# ✉️ تأكيد الإيميل بعد التسجيل (Email Verification Gate)
# =====================================================================
def verify_email(request):
    """
    GET /account/verify-email/?token=...
    يفعّل الـ tenant بعد ما العميل يضغط الرابط في إيميله.
    """
    from django.core import signing
    token = request.GET.get('token', '').strip()
    if not token:
        return render(request, 'clients/verify_email_result.html', {
            'ok': False, 'message': 'الرابط غير صالح.',
        })

    try:
        data = signing.loads(token, salt='tenant-email-verify', max_age=60 * 60 * 48)
    except signing.SignatureExpired:
        return render(request, 'clients/verify_email_result.html', {
            'ok': False, 'message': 'انتهت صلاحية الرابط. اطلب رابطاً جديداً.',
            'expired': True, 'email': '',
        })
    except signing.BadSignature:
        return render(request, 'clients/verify_email_result.html', {
            'ok': False, 'message': 'الرابط غير صحيح أو تم العبث به.',
        })

    schema_name = data.get('schema_name')
    tenant = Client.objects.filter(schema_name=schema_name).first()
    if not tenant:
        return render(request, 'clients/verify_email_result.html', {
            'ok': False, 'message': 'الحساب غير موجود.',
        })

    if not tenant.is_active:
        tenant.is_active = True
        tenant.save(update_fields=['is_active'])

    # 🚪 Auto-login token (نفس آلية signup الأصلية)
    from django.core import signing as _s
    import time
    auto_token = _s.dumps({
        'schema_name': schema_name,
        'user_id': data.get('user_id'),
        'created': int(time.time()),
    }, salt='tenant-auto-login-token')

    base_domain = os.getenv('BASE_DOMAIN', 'mousstec.com')
    url_safe = schema_name.replace('_', '-')
    target_url = f"https://{url_safe}.{base_domain}/auto-login/?token={auto_token}"

    return render(request, 'clients/verify_email_result.html', {
        'ok': True,
        'tenant_name': tenant.name,
        'target_url': target_url,
    })


def resend_verification(request):
    """POST /account/verify-email/resend/  body: email"""
    if request.method != 'POST':
        return redirect('/login/')

    email = request.POST.get('email', '').strip().lower()
    if not email:
        return redirect('/login/?msg=verify_missing_email')

    # 🛡️ Throttle: 3 إعادات / ساعة / إيميل
    blocked, _ = _throttle(f"verify_resend:{email}", limit=3, window=3600)
    if blocked:
        return render(request, 'clients/verify_email_result.html', {
            'ok': False, 'message': '🚫 طلبات كثيرة. حاول بعد ساعة.',
        })

    tenant = Client.objects.filter(email__iexact=email, is_active=False).first()
    if not tenant:
        # Don't leak whether the email is registered — generic OK
        return render(request, 'clients/verify_email_result.html', {
            'ok': True, 'message': 'لو الإيميل مسجل، هتلاقي الرابط في صندوق الوارد.',
            'tenant_name': '', 'target_url': '',
        })

    admin_user = None
    try:
        with schema_context(tenant.schema_name):
            admin_user = User.objects.filter(email__iexact=email, is_superuser=True).first()
    except Exception:
        pass
    if not admin_user:
        return redirect('/login/?msg=verify_user_missing')

    from django.core import signing
    import time
    verify_token = signing.dumps({
        'schema_name': tenant.schema_name,
        'user_id': admin_user.id,
        'email': email,
        'created': int(time.time()),
    }, salt='tenant-email-verify')

    public_host = request.get_host()
    verify_url = f"{request.scheme}://{public_host}/account/verify-email/?token={verify_token}"

    try:
        # 🐛 UTF-8 encoding عشان الـ subject/body عربي يوصل صح عبر SMTP
        from django.core.mail import EmailMessage
        msg = EmailMessage(
            subject='✉️ رابط تأكيد جديد | Mouss Tec',
            body=f"اضغط الرابط لتفعيل حساب {tenant.name}:\n\n{verify_url}\n\nصالح 48 ساعة.",
            from_email=None,
            to=[email],
        )
        msg.encoding = 'utf-8'
        msg.send(fail_silently=True)
    except Exception as e:
        logger.warning(f"[RESEND_VERIFY] email send failed: {e}")

    return render(request, 'clients/verify_email_result.html', {
        'ok': True, 'tenant_name': tenant.name,
        'message': 'تم إرسال رابط جديد. تحقق من بريدك.',
        'target_url': '',
    })
