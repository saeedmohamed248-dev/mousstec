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
from clients.models import Client, Domain

logger = logging.getLogger('mouss_tec_core')
User = get_user_model()
ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal')


# =====================================================================
# 🏢 محرك التخليق الآلي للمؤسسات المعزولة (Automated Onboarding Engine)
# =====================================================================
def register_new_tenant_saas(request):
    """
    محرك التأسيس السحابي (SaaS Onboarding Engine) مزود بنواة ضخ البيانات الذكية (Smart Seeding).
    """
    if request.method == 'POST':
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
                        default_plan = 'print_pro' if industry == 'printing' else 'gold'

                        tenant = Client.objects.create(
                            schema_name=schema_name,
                            name=company_name,
                            owner_name=data.get('full_name', company_name),
                            email=data['email'],
                            phone=data.get('phone', ''),
                            industry=industry,
                            business_type=business_type,
                            plan=default_plan,
                            is_active=True,
                        )

                        with schema_context(schema_name):
                            name_parts = data['full_name'].split(' ', 1)
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
                                if industry == 'automotive':
                                    from inventory.models import EmployeeProfile, Branch
                                    branch = Branch.objects.filter(name="الفرع الرئيسي").first()
                                    EmployeeProfile.objects.get_or_create(
                                        user=admin_user,
                                        defaults={'role': 'admin', 'branch': branch, 'can_edit_posted_invoices': True},
                                    )
                            except Exception:
                                pass

                        base_domain = os.getenv('BASE_DOMAIN', 'mousstec.com')
                        url_safe_slug = schema_name.replace('_', '-')
                        Domain.objects.get_or_create(
                            domain=f"{url_safe_slug}.{base_domain}",
                            defaults={'tenant': tenant, 'is_primary': True},
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
                return render(request, 'clients/signup_success.html', {
                    'company_name': company_name,
                    'target_url': f"https://{url_safe_final}.{os.getenv('BASE_DOMAIN', 'mousstec.com')}/{ADMIN_URL}/",
                    'admin_email': data['email'],
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
    """
    يُوجِّه المستخدم بذكاء بعد تسجيل الدخول:
    - السوبر أدمن → /superadmin/
    - مستخدم Tenant → /system/dashboard/
    - مستخدم على الـ Public Schema بدون صلاحيات → /login/
    """
    tenant = getattr(request, 'tenant', None)
    schema = getattr(connection, 'schema_name', 'public')

    if request.user.is_superuser and schema == 'public':
        return redirect('/superadmin/')

    if tenant and schema != 'public':
        industry = getattr(tenant, 'industry', 'automotive')
        if industry == 'printing':
            admin_url = os.getenv('ADMIN_URL', 'secure-portal')
            return redirect(f'/{admin_url}/')
        return redirect('/system/dashboard/')

    return redirect('/login/')


def client_login_finder(request):
    """
    صفحة 'جد حسابك' — يدخل العميل بريده، النظام يعيد توجيهه للـ Subdomain الصحيح.
    """
    error = None
    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        if email:
            tenant = Client.objects.filter(email__iexact=email).exclude(schema_name='public').first()
            if not tenant:
                tenant = Client.objects.filter(phone=email).exclude(schema_name='public').first()
            if not tenant:
                tenant = Client.objects.filter(
                    models.Q(name__icontains=email) | models.Q(schema_name=email)
                ).exclude(schema_name='public').first()

            if tenant:
                safe_slug = tenant.schema_name.replace('_', '-')
                request_host = request.get_host()
                host_parts = request_host.split('.')
                if len(host_parts) > 2:
                    base_host = '.'.join(host_parts[-2:])
                else:
                    base_host = request_host
                protocol = request.scheme
                admin_slug = ADMIN_URL
                login_url = f"{protocol}://{safe_slug}.{base_host}/{admin_slug}/login/"
                return render(request, 'clients/login_finder.html', {
                    'found_tenant': tenant,
                    'login_url': login_url,
                })

            error = "لا يوجد حساب مرتبط بهذا البريد أو رقم الموبايل. تأكد من البيانات أو أنشئ حساباً جديداً."
    return render(request, 'clients/login_finder.html', {'error': error})


# =====================================================================
# 📰 Landing pages
# =====================================================================
def mousstec_landing_page(request):
    return render(request, 'clients/landing.html')


def automotive_landing_page(request):
    """صفحة تعريفية كاملة بقطاع السيارات — مميزات، أسعار، وطريقة التسجيل"""
    return render(request, 'clients/auto_landing.html')


def printing_landing_page(request):
    """صفحة تعريفية كاملة بقطاع المطابع والتصميم — مميزات، أسعار، وطريقة التسجيل"""
    return render(request, 'clients/print_landing.html')


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

            otp_code = f"{secrets.randbelow(900000) + 100000}"
            cache_key = f"recovery_otp_{tenant.schema_name}"
            cache.set(cache_key, otp_code, timeout=600)

            recovery_email = tenant.email
            if not recovery_email and matched_user:
                recovery_email = matched_user.email

            email_sent = False
            if recovery_email and getattr(settings, 'EMAIL_HOST', ''):
                try:
                    from django.core.mail import send_mail
                    send_mail(
                        subject='كود استرجاع حسابك | Mouss Tec',
                        message=f'كود التحقق الخاص بك هو: {otp_code}\n\nصالح لمدة 10 دقائق.\n\nMouss Tec',
                        from_email=None,
                        recipient_list=[recovery_email],
                        fail_silently=True,
                    )
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
                'otp_hint': otp_code if (not email_sent and settings.DEBUG) else '',
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

            if not correct_otp or otp_input != correct_otp:
                context = {
                    'step': 'verify',
                    'tenant_name': tenant.name,
                    'tenant_schema': schema_name,
                    'error': 'كود التحقق خاطئ أو منتهي الصلاحية. حاول مرة أخرى.',
                }
                return render(request, 'clients/account_recovery.html', context)

            reset_token = secrets.token_urlsafe(32)
            cache.set(f"recovery_reset_{schema_name}", reset_token, timeout=600)
            cache.delete(cache_key)

            users_list = []
            with schema_context(schema_name):
                for u in User.objects.filter(is_active=True).order_by('-is_superuser', 'username'):
                    users_list.append({
                        'id': u.id,
                        'username': u.username,
                        'full_name': u.get_full_name() or u.username,
                        'is_superuser': u.is_superuser,
                    })

            context = {
                'step': 'reset',
                'tenant_name': tenant.name,
                'tenant_schema': schema_name,
                'reset_token': reset_token,
                'users': users_list,
            }
            return render(request, 'clients/account_recovery.html', context)

        elif step == 'reset':
            schema_name = request.POST.get('tenant_schema', '')
            reset_token = request.POST.get('reset_token', '')
            user_id = request.POST.get('user_id', '')
            new_password = request.POST.get('new_password', '')
            confirm_password = request.POST.get('confirm_password', '')

            correct_token = cache.get(f"recovery_reset_{schema_name}")
            if not correct_token or reset_token != correct_token:
                context['error'] = 'انتهت صلاحية الجلسة. ابدأ من جديد.'
                return render(request, 'clients/account_recovery.html', context)

            if not new_password or len(new_password) < 8:
                tenant = Client.objects.filter(schema_name=schema_name).first()
                context = {
                    'step': 'reset',
                    'tenant_name': tenant.name if tenant else '',
                    'tenant_schema': schema_name,
                    'reset_token': reset_token,
                    'error': 'كلمة السر يجب أن تكون 8 أحرف على الأقل.',
                }
                return render(request, 'clients/account_recovery.html', context)

            if new_password.isdigit():
                tenant = Client.objects.filter(schema_name=schema_name).first()
                context = {
                    'step': 'reset',
                    'tenant_name': tenant.name if tenant else '',
                    'tenant_schema': schema_name,
                    'reset_token': reset_token,
                    'error': 'كلمة المرور ضعيفة جداً. يرجى دمج حروف وأرقام.',
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

            try:
                with schema_context(schema_name):
                    user = User.objects.get(id=user_id)
                    user.set_password(new_password)
                    user.save()

                cache.delete(f"recovery_reset_{schema_name}")

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
