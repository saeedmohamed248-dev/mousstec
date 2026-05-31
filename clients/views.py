from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.http import JsonResponse, HttpResponseForbidden
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Count, Min, Sum, F, Avg, Max
from django.utils import timezone
from django.db import models, transaction, connection
from django.contrib.auth import get_user_model
from django.contrib import messages
from django.core.cache import cache
from django.conf import settings
from django.utils.text import slugify
from django_tenants.utils import schema_context
from decimal import Decimal
from datetime import timedelta
import json
import logging
import uuid
import os
import secrets
import hmac
import hashlib

# الاستدعاء الصريح والمباشر للاستمارات والموديلات
from clients.forms import TenantSignupForm
from clients.models import (
    Client, Domain, GlobalB2BMarketplace, BlindBiddingRequest, BidOffer,
    EscrowLedger, PlatformEvent, VisitorLog,
    MarketplaceCustomer, ServiceRequest, TenderOffer,
    DesignPackage, DesignPurchase, CustomerDesign,
)

logger = logging.getLogger('mouss_tec_core')
User = get_user_model()
ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal')

# =====================================================================
# 🏢 1. محرك التخليق الآلي للمؤسسات المعزولة (Automated Onboarding Engine)
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
                        # Auto-assign default plan based on industry
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
                            is_active=True
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
                                }
                            )
                            # ⚠️ دائماً نعيد كتابة الباسورد بالقيمة اللي اليوزر دخّلها
                            admin_user.set_password(data['password'])
                            admin_user.first_name = name_parts[0]
                            admin_user.last_name = name_parts[1] if len(name_parts) > 1 else ''
                            admin_user.is_staff = True
                            admin_user.is_superuser = True
                            admin_user.save()

                            # ربط الـ EmployeeProfile حسب القطاع
                            try:
                                if industry == 'automotive':
                                    from inventory.models import EmployeeProfile, Branch
                                    branch = Branch.objects.filter(name="الفرع الرئيسي").first()
                                    EmployeeProfile.objects.get_or_create(
                                        user=admin_user,
                                        defaults={'role': 'admin', 'branch': branch, 'can_edit_posted_invoices': True}
                                    )
                            except Exception:
                                pass

                        # ── إنشاء سجل الدومين حتى يعرف TenantMainMiddleware يوجه الـ subdomain ──
                        base_domain = os.getenv('BASE_DOMAIN', 'mousstec.com')
                        url_safe_slug = schema_name.replace('_', '-')
                        Domain.objects.get_or_create(
                            domain=f"{url_safe_slug}.{base_domain}",
                            defaults={'tenant': tenant, 'is_primary': True}
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
                    'admin_email': data['email']
                })
            else:
                form.add_error(None, "🛑 فشل التأسيس: الأسماء مقفلة، جرب اسماً مختلفاً.")
    else:
        # قراءة القطاع من الـ URL parameter لتحديد القطاع والنشاط الافتراضي
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
# 🌍 2. واجهة الإمبراطورية المفتوحة (Public SaaS Landing Page)
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

    # مالك المنصة (superuser على public) → لوحة السوبر أدمن
    if request.user.is_superuser and schema == 'public':
        return redirect('/superadmin/')

    # مستخدم tenant — التوجيه حسب الصناعة
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
    يبني الرابط من schema_name + الدومين الحالي بدلاً من الاعتماد على جدول Domain.
    """
    error = None
    if request.method == 'POST':
        email = request.POST.get('email', '').strip().lower()
        if email:
            # بحث بالإيميل أو رقم الموبايل في Client
            tenant = Client.objects.filter(email__iexact=email).exclude(schema_name='public').first()
            if not tenant:
                tenant = Client.objects.filter(phone=email).exclude(schema_name='public').first()
            if not tenant:
                # بحث بالاسم أو الـ schema
                tenant = Client.objects.filter(
                    models.Q(name__icontains=email) | models.Q(schema_name=email)
                ).exclude(schema_name='public').first()

            if tenant:
                safe_slug = tenant.schema_name.replace('_', '-')
                request_host = request.get_host()
                # إزالة أي subdomain موجود (مثال: www.mousstec.com → mousstec.com)
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


def mousstec_landing_page(request):
    return render(request, 'clients/landing.html')


def automotive_landing_page(request):
    """صفحة تعريفية كاملة بقطاع السيارات — مميزات، أسعار، وطريقة التسجيل"""
    return render(request, 'clients/auto_landing.html')


def printing_landing_page(request):
    """صفحة تعريفية كاملة بقطاع المطابع والتصميم — مميزات، أسعار، وطريقة التسجيل"""
    return render(request, 'clients/print_landing.html')


# =====================================================================
# 🔑 2.5. استرجاع كلمة السر / العثور على الحساب (Password Recovery)
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

        # ── الخطوة 1: البحث عن الحساب ──
        if step == 'search':
            query = request.POST.get('query', '').strip()
            if not query:
                context['error'] = 'أدخل رقم الموبايل أو البريد الإلكتروني'
                return render(request, 'clients/account_recovery.html', context)

            tenant = None
            matched_user = None

            # بحث بالموبايل في Client
            tenant = Client.objects.filter(phone=query).exclude(schema_name='public').first()

            # بحث بالإيميل في Client
            if not tenant:
                tenant = Client.objects.filter(email__iexact=query).exclude(schema_name='public').first()

            # بحث بالـ schema_name (اسم الشركة كرابط)
            if not tenant:
                tenant = Client.objects.filter(schema_name=query).exclude(schema_name='public').first()

            # بحث بالاسم الجزئي
            if not tenant:
                tenant = Client.objects.filter(name__icontains=query).exclude(schema_name='public').first()

            if not tenant:
                context['error'] = 'لا يوجد حساب مرتبط بهذا الرقم أو البريد. تأكد من البيانات أو أنشئ حساباً جديداً.'
                return render(request, 'clients/account_recovery.html', context)

            # إنشاء كود تحقق OTP وحفظه في الكاش
            otp_code = f"{secrets.randbelow(900000) + 100000}"  # 6 أرقام
            cache_key = f"recovery_otp_{tenant.schema_name}"
            cache.set(cache_key, otp_code, timeout=600)  # 10 دقائق

            # إرسال الكود عبر الإيميل (إذا متاح)
            recovery_email = tenant.email
            if not recovery_email and matched_user:
                recovery_email = matched_user.email

            # محاولة إرسال الكود بالإيميل (فقط إذا كان SMTP مُعد)
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

            # إخفاء جزء من الإيميل
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
                # 🛡️ إظهار الكود فقط في وضع التطوير — في الإنتاج يجب إعداد SMTP
                'otp_hint': otp_code if (not email_sent and settings.DEBUG) else '',
                'email_sent': email_sent,
            }
            return render(request, 'clients/account_recovery.html', context)

        # ── الخطوة 2: التحقق من كود OTP ──
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

            # الكود صحيح — انتقل لإعادة تعيين كلمة السر
            # إنشاء توكن مؤقت
            reset_token = secrets.token_urlsafe(32)
            cache.set(f"recovery_reset_{schema_name}", reset_token, timeout=600)
            cache.delete(cache_key)  # حذف الـ OTP

            # جلب المستخدمين من الـ tenant
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

        # ── الخطوة 3: إعادة تعيين كلمة السر ──
        elif step == 'reset':
            schema_name = request.POST.get('tenant_schema', '')
            reset_token = request.POST.get('reset_token', '')
            user_id = request.POST.get('user_id', '')
            new_password = request.POST.get('new_password', '')
            confirm_password = request.POST.get('confirm_password', '')

            # التحقق من التوكن
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

            # 🛡️ رفض كلمات السر الرقمية فقط (مطابقة لسياسة التسجيل في forms.py)
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

                # بناء رابط تسجيل الدخول
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
# 🌐 3. الموزع المركزي للإشعارات الخارجية (FinTech Webhook Multiplexer)
# =====================================================================
@csrf_exempt
def universal_webhook_multiplexer(request):
    """
    بوابة FinTech محصنة: تمنع التكرار (Idempotency) وتطبق سياسات مكافحة غسيل الأموال (AML).
    """
    if request.method != 'POST': return HttpResponseForbidden("POST Only")

    # 🛡️ HMAC signature verification for webhook security
    import hmac as _hmac
    import hashlib as _hashlib
    secret = getattr(settings, 'WEBHOOK_HMAC_SECRET', None)
    if secret:
        received_sig = request.META.get('HTTP_X_WEBHOOK_SIGNATURE', '')
        if not received_sig:
            logger.warning("[WEBHOOK] Missing signature header — rejected.")
            return HttpResponseForbidden("Missing signature")
        computed = _hmac.new(secret.encode('utf-8'), request.body, _hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(computed, received_sig):
            logger.warning("[WEBHOOK] HMAC verification failed — rejected.")
            return HttpResponseForbidden("Invalid signature")
    
    try:
        payload = json.loads(request.body)
        event_id = payload.get('id', f'evt_{uuid.uuid4().hex[:12]}')
        
        if cache.get(f"webhook_processed_{event_id}"):
            return JsonResponse({"status": "duplicate"})
        
        if payload.get('type') == 'payment_intent.succeeded':
            client_id = payload['data']['metadata']['client_id']
            amount = Decimal(str(payload['data']['amount_received'])) / 100
            
            with transaction.atomic():
                tenant = Client.objects.select_for_update().get(id=client_id)
                
                # 🚀 ابتكار AML (مكافحة الاحتيال): إذا كان المبلغ ضخماً جداً، يتم تعليقه لحين المراجعة اليدوية
                if amount > Decimal('100000'):
                    logger.warning(f"🚨 [AML ALERT]: Large suspicious deposit of {amount} for {tenant.schema_name}.")
                    EscrowLedger.objects.create(client=tenant, transaction_type='hold', amount=amount, description=f"إيداع معلق للمراجعة الأمنية ({event_id})")
                    tenant.is_fraud_flagged = True
                    tenant.save(update_fields=['is_fraud_flagged'])
                else:
                    tenant.wallet_balance = F('wallet_balance') + amount
                    tenant.save(update_fields=['wallet_balance'])
                    EscrowLedger.objects.create(client=tenant, transaction_type='deposit', amount=amount, description=f"إيداع سحابي ({event_id})")
            
            cache.set(f"webhook_processed_{event_id}", "processed", timeout=86400)
            return JsonResponse({"status": "success"})

        return JsonResponse({"status": "ignored"})
    except Exception as e:
        logger.error(f"🚨 Webhook Failure: {e}")
        return JsonResponse({"error": "Internal Error"}, status=500)

# =====================================================================
# 🛒 4. محرك بحث سوق التجار (B2B Global Search API)
# =====================================================================
@login_required(login_url='/secure-portal/')
def b2b_market_search_api(request):
    if connection.schema_name == 'public' and not request.user.is_superuser:
        return JsonResponse({"error": "غير مصرح"}, status=403)

    part_number = request.GET.get('part_number', '').strip()
    if not part_number: return JsonResponse({"error": "برجاء تزويد رقم القطعة"}, status=400)

    results = GlobalB2BMarketplace.objects.filter(
        part_number__iexact=part_number, available_qty__gt=0, 
        tenant__is_active=True, tenant__is_marketplace_active=True, tenant__is_fraud_flagged=False
    ).select_related('tenant').order_by('-tenant__is_verified_merchant', 'wholesale_price')[:15]

    data = [{
        "dealer_name": item.tenant.name, "is_verified": item.tenant.is_verified_merchant,
        "rating": float(item.tenant.market_rating or 5.0), "price": float(item.wholesale_price), 
        "qty_available": item.available_qty, "condition": item.get_condition_display()
    } for item in results]

    return JsonResponse({"status": "success", "results_count": len(data), "dealers": data})

# =====================================================================
# ⚖️ 5. محرك المزادات العكسية والترسية الذكية (Dynamic Blind Bidding)
# =====================================================================
@login_required(login_url='/secure-portal/')
def active_blind_bids_api(request):
    active_bids = BlindBiddingRequest.objects.filter(status='open', expires_at__gt=timezone.now()).select_related('buyer').order_by('-created_at')
    data = [{"bid_id": b.id, "part_number": b.part_number, "required_qty": b.required_qty, "buyer_name": "مشتري سري", "urgency": "High" if (b.expires_at - timezone.now()).total_seconds() < 7200 else "Normal"} for b in active_bids]
    return JsonResponse({"status": "success", "bids": data})

@login_required(login_url='/secure-portal/')
def submit_bid_offer_api(request):
    """
    🚀 ابتكار الذكاء التنافسي: وزن الخوارزمية يتغير ديناميكياً بناءً على سرعة التوصيل وعمر المزاد.
    """
    if request.method != 'POST': return JsonResponse({"error": "POST Only"}, status=400)
    if not hasattr(request, 'tenant') or request.tenant.schema_name == 'public': return JsonResponse({"error": "للشركات فقط"}, status=403)

    try:
        data = json.loads(request.body)
        bid_id, offer_price, delivery_days = data.get('bid_id'), Decimal(str(data.get('offer_price', 0))), int(data.get('delivery_days', 1))

        with transaction.atomic():
            bid = get_object_or_404(BlindBiddingRequest.objects.select_for_update(), id=bid_id, status='open')
            buyer_tenant = Client.objects.select_for_update().get(id=bid.buyer_id)
            seller_tenant = request.tenant
            
            if buyer_tenant == seller_tenant: return JsonResponse({"error": "لا يمكنك المزايدة على طلبك"}, status=400)

            # 🤖 خوارزمية الترسية الديناميكية (Dynamic Weights)
            target = bid.target_price or offer_price
            base_price_score = min((target / offer_price) * 100, 100) if offer_price > 0 else 0
            
            # إذا كان التوصيل فورياً (0 أو 1 يوم)، نعطي وزن التوصيل أولوية قصوى (50%)، السعر (30%)، الثقة (20%)
            if delivery_days <= 1:
                final_match_score = Decimal(str((base_price_score * 0.3) + 50 + ((getattr(seller_tenant, 'ai_trust_score', 100) / 100) * 20)))
            else:
                # وزن عادي: سعر (50%)، ثقة (30%)، توصيل (20%)
                del_score = max(20 - (delivery_days * 2), 0)
                final_match_score = Decimal(str((base_price_score * 0.5) + del_score + ((getattr(seller_tenant, 'ai_trust_score', 100) / 100) * 30)))

            offer, _ = BidOffer.objects.update_or_create(bidding_request=bid, seller=seller_tenant, defaults={'offer_price': offer_price, 'estimated_delivery_days': delivery_days, 'ai_match_score': final_match_score})

            if not bid.ai_recommended_winner or final_match_score > bid.ai_recommended_winner.ai_match_score:
                bid.ai_recommended_winner = offer
                bid.save(update_fields=['ai_recommended_winner'])

            # التنفيذ المالي والمقاصة
            if bid.auto_award and bid.target_price and offer_price <= bid.target_price:
                total_req = (offer_price * bid.required_qty) * (Decimal('1') + getattr(buyer_tenant, 'platform_fee_rate', Decimal('2.5')) / 100)
                if buyer_tenant.wallet_balance >= total_req:
                    bid.status, bid.winner, bid.winning_price = 'escrow_held', seller_tenant, offer_price
                    bid.save(update_fields=['status', 'winner', 'winning_price'])
                    offer.is_winner = True
                    offer.save(update_fields=['is_winner'])
                    EscrowLedger.objects.create(client=buyer_tenant, bidding_request=bid, transaction_type='hold', amount=total_req, description=f"ضمان مزاد #{bid.id}")
                    return JsonResponse({"status": "auto_awarded", "message": "تم الترسية وحجز الضمان!"})

        return JsonResponse({"status": "success", "message": "تم تقديم عرضك بنجاح.", "ai_score": float(final_match_score)})
    except Exception as e:
        logger.error("[BID] submit_bid_offer_api error: %s", e)
        return JsonResponse({"error": "حدث خطأ أثناء تقديم العرض. حاول مرة أخرى."}, status=500)

# =====================================================================
# 🛡️ 6. محفظة الضامن المالي (Escrow Ledger)
# =====================================================================
@login_required(login_url='/secure-portal/')
def my_escrow_wallet_api(request):
    if not hasattr(request, 'tenant') or request.tenant.schema_name == 'public': return JsonResponse({"error": "متاح للمؤسسات فقط."}, status=403)
    return JsonResponse({"status": "success", "wallet": {"available": float(request.tenant.wallet_balance), "held": float(request.tenant.escrow_held)}})

# =====================================================================
# 🌍 7. رادار التنبؤ (Advanced Market Demand AI Predictor)
# =====================================================================
@login_required(login_url='/secure-portal/')
def market_demand_predictor_api(request):
    """
    🚀 ابتكار: استبعاد القيم الشاذة (Outliers) لحساب متوسط الأسعار بدقة أعلى.
    """
    thirty_days_ago = timezone.now() - timedelta(days=30)
    
    trending_parts = BlindBiddingRequest.objects.filter(created_at__gte=thirty_days_ago, status__in=['completed', 'escrow_held']) \
        .values('part_number').annotate(request_count=Count('id'), avg_win_price=Avg('winning_price'), min_win_price=Min('winning_price'), max_win_price=Max('winning_price')).order_by('-request_count')[:5]

    data = []
    for part in trending_parts:
        # استبعاد التذبذبات السعرية الوهمية من الرادار
        if part['max_win_price'] > (part['avg_win_price'] * Decimal('3.0')):
            part['max_win_price'] = part['avg_win_price'] * Decimal('1.5')

        data.append({
            "part_number": part['part_number'],
            "demand_heat": part['request_count'],
            "pricing_band": {
                "lowest": float(part['min_win_price']) if part['min_win_price'] else 0,
                "highest": float(part['max_win_price']) if part['max_win_price'] else 0,
                "suggested": float(part['avg_win_price']) if part['avg_win_price'] else 0
            }
        })
    return JsonResponse({"status": "success", "trending_parts": data})

# =====================================================================
# 💳 8. بوابة الاشتراكات والباقات (SaaS Pricing & Retention)
# =====================================================================
def saas_pricing_page(request):
    shop_schema = request.GET.get('shop', '')
    tenant = Client.objects.filter(schema_name=shop_schema).first() if shop_schema else None

    if request.method == 'POST':
        selected_plan = request.POST.get('plan')
        shop_post = request.POST.get('shop', '').strip()
        target_tenant = Client.objects.filter(schema_name=shop_post).first() if shop_post else None

        valid_plans = [c[0] for c in Client.SUBSCRIPTION_CHOICES]

        # سيناريو 1: زائر جديد (مافيش shop) → يروح لصفحة التسجيل مع الباقة محددة مسبقاً
        if not target_tenant and selected_plan in valid_plans:
            return redirect(f"{reverse('saas_customer_signup')}?plan={selected_plan}")

        # سيناريو 2: عميل موجود يجدد أو يغير الباقة
        # حماية أمنية: فقط السوبر أدمن أو مستخدم مصادق عليه يمكنه تغيير الاشتراك
        if target_tenant and selected_plan in valid_plans:
            if not request.user.is_authenticated:
                messages.error(request, "🔒 يجب تسجيل الدخول أولاً لإدارة الاشتراك.")
                return redirect(f"{reverse('client_login_finder')}")
            if not request.user.is_superuser:
                messages.error(request, "🛑 غير مصرح — فقط السوبر أدمن يمكنه تغيير الاشتراك مباشرة.")
                return redirect(reverse('saas_pricing') + f'?shop={shop_post}')
            with transaction.atomic():
                target_tenant.plan, target_tenant.status = selected_plan, 'active'
                base_date = max(target_tenant.subscription_end_date or timezone.localdate(), timezone.localdate())

                # مكافأة ولاء: 5 أيام مجانية عند التجديد المبكر
                bonus_days = 5 if (target_tenant.subscription_end_date and target_tenant.subscription_end_date > timezone.localdate()) else 0
                target_tenant.subscription_end_date = base_date + timedelta(days=30 + bonus_days)

                target_tenant.save()
                from django.conf import settings as _cfg2
                _bd = getattr(_cfg2, 'BASE_DOMAIN', 'mousstec.com')
                return redirect(f"https://{target_tenant.schema_name.replace('_', '-')}.{_bd}/{ADMIN_URL}/")

        messages.error(request, "🛑 فشل تنفيذ عملية الاشتراك.")

    # نظام خصومات الفترات الطويلة
    billing_discounts = {
        'monthly':      {'label': 'شهري',       'months': 1,  'discount': 0},
        'quarterly':    {'label': 'ربع سنوي',   'months': 3,  'discount': 9},
        'semi_annual':  {'label': 'نصف سنوي',   'months': 6,  'discount': 12.5},
        'annual':       {'label': 'سنوي',       'months': 12, 'discount': 25},
    }

    return render(request, 'clients/pricing.html', {
        'tenant': tenant, 'shop': shop_schema,
        'pricing': {
            'silver': {'price': 780, 'original_price': 1000, 'users': 1, 'branches': 1, 'treasuries': 1, 'limited_offer': True},
            'gold': {'price': 1250, 'original_price': 2000, 'users': 4, 'branches': 2, 'treasuries': 2},
            'empire': {'price': 1800, 'original_price': 3000},
            'addon_price': 125,
            'free_trial_days': 3,
            'vodafone_cash': '',
            'billing_discounts': billing_discounts,
        }
    })


# =====================================================================
# 💳 8.5 بوابة الدفع عبر Paymob (Visa / Mastercard)
# =====================================================================
def paymob_checkout(request):
    """
    إنشاء طلب دفع عبر Paymob وتوجيه العميل لصفحة الدفع الآمنة.
    يتطلب تكوين PAYMOB_API_KEY و PAYMOB_INTEGRATION_ID في البيئة.
    """
    if request.method != 'POST':
        return redirect('saas_pricing')

    plan = request.POST.get('plan', '')
    amount = request.POST.get('amount', '0')
    shop = request.POST.get('shop', '')
    billing_period = request.POST.get('billing_period', 'monthly')

    paymob_api_key = getattr(settings, 'PAYMOB_API_KEY', '') or os.getenv('PAYMOB_API_KEY', '')
    paymob_integration_id = getattr(settings, 'PAYMOB_INTEGRATION_ID', '') or os.getenv('PAYMOB_INTEGRATION_ID', '')
    paymob_iframe_id = getattr(settings, 'PAYMOB_IFRAME_ID', '') or os.getenv('PAYMOB_IFRAME_ID', '')

    if not paymob_api_key:
        logger.error(f"[PAYMOB] API key not configured. Settings: API_KEY={bool(paymob_api_key)}, INT_ID={bool(paymob_integration_id)}, IFRAME={bool(paymob_iframe_id)}")
        messages.error(request, "الدفع الإلكتروني بالفيزا غير متاح حالياً. يرجى الدفع عبر فودافون كاش أو التواصل مع الدعم الفني.")
        return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))

    import requests as http_requests

    try:
        # Step 1: Auth token
        auth_res = http_requests.post('https://accept.paymob.com/api/auth/tokens', json={
            'api_key': paymob_api_key
        }, timeout=15)
        auth_token = auth_res.json().get('token')

        # Step 2: Create order
        amount_cents = int(float(amount) * 100)
        order_res = http_requests.post('https://accept.paymob.com/api/ecommerce/orders', json={
            'auth_token': auth_token,
            'delivery_needed': 'false',
            'amount_cents': amount_cents,
            'currency': 'EGP',
            'items': [{'name': f'Mouss Tec {plan} Plan', 'amount_cents': amount_cents, 'quantity': '1'}],
            'merchant_order_id': f'mousstec_{plan}_{uuid.uuid4().hex[:8]}',
        }, timeout=15)
        order_id = order_res.json().get('id')

        # Step 3: Payment key
        billing = {
            'first_name': shop or 'Customer', 'last_name': 'MoussTec',
            'email': 'customer@mousstec.com', 'phone_number': '01000000000',
            'apartment': 'NA', 'floor': 'NA', 'street': 'NA', 'building': 'NA',
            'shipping_method': 'NA', 'postal_code': 'NA', 'city': 'Cairo',
            'country': 'EG', 'state': 'Cairo',
        }
        key_res = http_requests.post('https://accept.paymob.com/api/acceptance/payment_keys', json={
            'auth_token': auth_token,
            'amount_cents': amount_cents,
            'expiration': 3600,
            'order_id': order_id,
            'billing_data': billing,
            'currency': 'EGP',
            'integration_id': int(paymob_integration_id),
            'lock_order_when_paid': 'true',
        }, timeout=15)
        payment_token = key_res.json().get('token')

        # Store plan info in cache for callback
        cache.set(f'paymob_order_{order_id}', {
            'plan': plan, 'shop': shop, 'amount': amount,
            'billing_period': billing_period,
        }, timeout=7200)

        # Step 4: Redirect to Paymob iframe
        iframe_url = f'https://accept.paymob.com/api/acceptance/iframes/{paymob_iframe_id}?payment_token={payment_token}'
        return redirect(iframe_url)

    except Exception as e:
        logger.error(f"Paymob checkout error: {e}")
        messages.error(request, "حدث خطأ اثناء الاتصال ببوابة الدفع. يرجى المحاولة مرة اخرى او الدفع عبر فودافون كاش.")
        return redirect(reverse('saas_pricing') + (f'?shop={shop}' if shop else ''))


@csrf_exempt
def paymob_callback(request):
    """
    استقبال نتيجة الدفع من Paymob بعد إتمام العملية.
    🛡️ يتحقق من توقيع HMAC لمنع التلاعب بالطلبات.
    """
    data = request.GET.dict() if request.method == 'GET' else json.loads(request.body) if request.body else {}

    # ── 🛡️ التحقق من توقيع HMAC (يمنع تزوير طلبات الدفع) ──
    paymob_hmac_secret = os.getenv('PAYMOB_HMAC_SECRET', '')
    received_hmac = request.GET.get('hmac', '') or data.get('hmac', '')

    if paymob_hmac_secret:
        # Paymob HMAC concatenation order (alphabetical by key name):
        # amount_cents, created_at, currency, error_occured, has_parent_transaction,
        # id, integration_id, is_3d_secure, is_auth, is_capture, is_refunded,
        # is_standalone_payment, is_voided, order.id, owner, pending,
        # source_data.pan, source_data.sub_type, source_data.type, success
        obj = data.get('obj', {})
        if not obj and request.method == 'GET':
            # GET callback — obj fields are flat in query params
            hmac_fields = [
                str(data.get('amount_cents', '')),
                str(data.get('created_at', '')),
                str(data.get('currency', '')),
                str(data.get('error_occured', '')),
                str(data.get('has_parent_transaction', '')),
                str(data.get('id', '')),
                str(data.get('integration_id', '')),
                str(data.get('is_3d_secure', '')),
                str(data.get('is_auth', '')),
                str(data.get('is_capture', '')),
                str(data.get('is_refunded', '')),
                str(data.get('is_standalone_payment', '')),
                str(data.get('is_voided', '')),
                str(data.get('order', '')),
                str(data.get('owner', '')),
                str(data.get('pending', '')),
                str(data.get('source_data.pan', data.get('source_data_pan', ''))),
                str(data.get('source_data.sub_type', data.get('source_data_sub_type', ''))),
                str(data.get('source_data.type', data.get('source_data_type', ''))),
                str(data.get('success', '')),
            ]
        else:
            # POST callback — obj is nested JSON
            source_data = obj.get('source_data', {})
            order_obj = obj.get('order', {})
            hmac_fields = [
                str(obj.get('amount_cents', '')),
                str(obj.get('created_at', '')),
                str(obj.get('currency', '')),
                str(obj.get('error_occured', '')),
                str(obj.get('has_parent_transaction', '')),
                str(obj.get('id', '')),
                str(obj.get('integration_id', '')),
                str(obj.get('is_3d_secure', '')),
                str(obj.get('is_auth', '')),
                str(obj.get('is_capture', '')),
                str(obj.get('is_refunded', '')),
                str(obj.get('is_standalone_payment', '')),
                str(obj.get('is_voided', '')),
                str(order_obj.get('id', '')),
                str(obj.get('owner', '')),
                str(obj.get('pending', '')),
                str(source_data.get('pan', '')),
                str(source_data.get('sub_type', '')),
                str(source_data.get('type', '')),
                str(obj.get('success', '')),
            ]

        concatenated = ''.join(hmac_fields)
        computed_hmac = hmac.new(
            paymob_hmac_secret.encode('utf-8'),
            concatenated.encode('utf-8'),
            hashlib.sha512,
        ).hexdigest()

        if not hmac.compare_digest(computed_hmac, received_hmac):
            logger.critical(f"🚨 [PAYMOB HMAC MISMATCH] IP: {request.META.get('REMOTE_ADDR')} — Possible payment forgery attempt!")
            return redirect(reverse('saas_pricing') + '?payment=failed&reason=signature')
    else:
        logger.warning("⚠️ [PAYMOB] PAYMOB_HMAC_SECRET not configured — HMAC verification skipped!")

    success = data.get('success', data.get('obj', {}).get('success', 'false'))
    order_id = data.get('order', data.get('obj', {}).get('order', {}).get('id', ''))

    if str(success).lower() == 'true' and order_id:
        # Check if this is a design store purchase
        design_info = cache.get(f'paymob_design_{order_id}')
        if design_info:
            try:
                purchase = DesignPurchase.objects.get(pk=design_info['purchase_id'])
                if purchase.status != 'paid':
                    purchase.status = 'paid'
                    purchase.payment_reference = str(order_id)
                    purchase.save(update_fields=['status', 'payment_reference'])
                    logger.info(f"[PAYMOB/DESIGN] Purchase #{purchase.pk} paid via card")
                cache.delete(f'paymob_design_{order_id}')
                return redirect(f'/marketplace/design-store/my-designs/?payment=success')
            except DesignPurchase.DoesNotExist:
                logger.error(f"[PAYMOB/DESIGN] Purchase {design_info['purchase_id']} not found")

        order_info = cache.get(f'paymob_order_{order_id}')
        if order_info:
            plan = order_info.get('plan')
            shop = order_info.get('shop')
            billing_period = order_info.get('billing_period', 'monthly')
            # ── خريطة أيام الفترة ──
            period_days_map = {
                'monthly': 30, 'quarterly': 90,
                'semi_annual': 180, 'annual': 365,
            }
            days_to_add = period_days_map.get(billing_period, 30)

            if shop:
                try:
                    with transaction.atomic():
                        tenant = Client.objects.select_for_update().get(schema_name=shop)
                        tenant.plan = plan
                        tenant.status = 'active'
                        tenant.is_active = True
                        base_date = max(tenant.subscription_end_date or timezone.localdate(), timezone.localdate())
                        tenant.subscription_end_date = base_date + timedelta(days=days_to_add)
                        tenant.save(update_fields=['plan', 'status', 'is_active', 'subscription_end_date'])
                        cache.delete(f'paymob_order_{order_id}')
                        logger.info(f"✅ Paymob payment success: {shop} -> {plan} ({billing_period}, +{days_to_add} days)")
                except Client.DoesNotExist:
                    logger.error(f"🔴 Paymob callback: tenant {shop} not found")

            return redirect(reverse('saas_pricing') + f'?shop={shop}&payment=success')

    return redirect(reverse('saas_pricing') + '?payment=failed')


# =====================================================================
# 🧩 9. محرك شراء الإضافات بالتناسب الزمني (Pro-Rated Addon Engine)
# =====================================================================
@login_required(login_url='/secure-portal/')
def manage_subscription(request):
    tenant = getattr(request, 'tenant', None)
    if not tenant or tenant.schema_name == 'public':
        return redirect('/')

    addon_labels = {'employee': 'موظف', 'branch': 'فرع', 'treasury': 'خزينة'}
    result_msg = None

    if request.method == 'POST' and request.user.is_superuser:
        addon_type = request.POST.get('addon_type')
        qty = int(request.POST.get('quantity', 1))
        if addon_type in addon_labels and 1 <= qty <= 10:
            prorated = tenant.calculate_prorated_addon_cost()
            total_cost = prorated * qty
            with transaction.atomic():
                t = Client.objects.select_for_update().get(pk=tenant.pk)
                if addon_type == 'employee':
                    t.extra_users_purchased += qty
                elif addon_type == 'branch':
                    t.extra_branches_purchased += qty
                elif addon_type == 'treasury':
                    t.extra_treasuries_purchased += qty
                t.save()
                EscrowLedger.objects.create(
                    client=t, transaction_type='fee_deduction', amount=total_cost,
                    description=f"شراء {qty} {addon_labels[addon_type]} إضافي — {prorated} ج.م/وحدة (تناسبي)"
                )
            tenant.refresh_from_db()
            result_msg = f"تم إضافة {qty} {addon_labels[addon_type]} بنجاح — التكلفة: {total_cost} ج.م"

    prorated_cost = tenant.calculate_prorated_addon_cost()
    remaining_days = 0
    if tenant.subscription_end_date:
        remaining_days = max((tenant.subscription_end_date - timezone.now().date()).days, 0)

    # ── Build available plans for this industry ──
    industry = getattr(tenant, 'industry', 'automotive')
    if industry == 'printing':
        available_plans = [
            {'key': 'print_basic', 'name': 'Print Basic', 'desc': 'للمطابع الصغيرة واستوديوهات التصميم', 'price': 550, 'users': 2, 'branches': 1, 'treasuries': 1, 'icon': 'fa-print', 'color': 'pink'},
            {'key': 'print_pro', 'name': 'Print Pro', 'desc': 'للمطابع المتوسطة ومكاتب التصميم', 'price': 880, 'users': 5, 'branches': 2, 'treasuries': 2, 'icon': 'fa-palette', 'color': 'purple'},
            {'key': 'print_enterprise', 'name': 'Print Enterprise', 'desc': 'للمطابع الكبيرة ومجموعات التصميم', 'price': 2000, 'users': 15, 'branches': 5, 'treasuries': 5, 'icon': 'fa-building', 'color': 'amber'},
        ]
    else:
        available_plans = [
            {'key': 'silver', 'name': 'سيلفر', 'desc': 'لمراكز الصيانة وتجار قطع الغيار', 'price': 685, 'users': 1, 'branches': 1, 'treasuries': 1, 'icon': 'fa-car', 'color': 'slate'},
            {'key': 'gold', 'name': 'جولد', 'desc': 'لمراكز الصيانة وتجار قطع الغيار الشامل', 'price': 1185, 'users': 4, 'branches': 2, 'treasuries': 2, 'icon': 'fa-crown', 'color': 'yellow'},
            {'key': 'empire', 'name': 'Empire', 'desc': 'لتجار القطع والشركات الكبيرة', 'price': 3000, 'users': 15, 'branches': 5, 'treasuries': 5, 'icon': 'fa-gem', 'color': 'purple'},
        ]

    # ── AI Design packages (one-time purchase from DesignPackage model) ──
    from clients.models import DesignPackage
    customer_ai_pkgs = list(DesignPackage.objects.filter(
        is_active=True, target_audience='customer'
    ).order_by('sort_order'))
    designer_ai_pkgs = list(DesignPackage.objects.filter(
        is_active=True, target_audience='designer'
    ).order_by('sort_order'))

    return render(request, 'clients/manage_subscription.html', {
        'tenant': tenant,
        'prorated_cost': prorated_cost,
        'full_addon_price': float(Client.ADDON_PRICE_PER_MONTH),
        'remaining_days': remaining_days,
        'result_msg': result_msg,
        'available_plans': available_plans,
        'customer_ai_pkgs': customer_ai_pkgs,
        'designer_ai_pkgs': designer_ai_pkgs,
        'current_plan': tenant.plan,
        'ADMIN_URL': os.getenv('ADMIN_URL', 'secure-portal'),
    })


@login_required(login_url='/secure-portal/')
def purchase_addon_api(request):
    if request.method != 'POST':
        return JsonResponse({"error": "POST Only"}, status=400)
    tenant = getattr(request, 'tenant', None)
    if not tenant or tenant.schema_name == 'public':
        return JsonResponse({"error": "متاح للمؤسسات فقط"}, status=403)
    if not request.user.is_superuser:
        return JsonResponse({"error": "فقط المدير المسؤول يمكنه شراء الإضافات"}, status=403)

    try:
        data = json.loads(request.body)
        addon_type = data.get('addon_type')
        qty = int(data.get('quantity', 1))
        addon_labels = {'employee': 'موظف', 'branch': 'فرع', 'treasury': 'خزينة'}

        if addon_type not in addon_labels:
            return JsonResponse({"error": "نوع الإضافة غير صالح"}, status=400)
        if qty < 1 or qty > 10:
            return JsonResponse({"error": "الكمية يجب أن تكون بين 1 و 10"}, status=400)

        prorated = tenant.calculate_prorated_addon_cost()
        total_cost = prorated * qty

        with transaction.atomic():
            t = Client.objects.select_for_update().get(pk=tenant.pk)
            if addon_type == 'employee':
                t.extra_users_purchased += qty
            elif addon_type == 'branch':
                t.extra_branches_purchased += qty
            elif addon_type == 'treasury':
                t.extra_treasuries_purchased += qty
            t.save()
            EscrowLedger.objects.create(
                client=t, transaction_type='fee_deduction', amount=total_cost,
                description=f"شراء {qty} {addon_labels[addon_type]} إضافي — {prorated} ج.م/وحدة (تناسبي)"
            )

        remaining_days = 0
        if tenant.subscription_end_date:
            remaining_days = max((tenant.subscription_end_date - timezone.now().date()).days, 0)

        return JsonResponse({
            "status": "success", "addon_type": addon_type, "quantity": qty,
            "cost_per_unit": float(prorated), "total_cost": float(total_cost),
            "remaining_days": remaining_days,
            "message": f"تم إضافة {qty} {addon_labels[addon_type]} بنجاح — التكلفة: {total_cost} ج.م"
        })
    except Exception as e:
        logger.error("[ADDON] purchase_addon_api error: %s", e)
        return JsonResponse({"error": "حدث خطأ أثناء شراء الإضافة. حاول مرة أخرى."}, status=500)


# =====================================================================
# 👑 Super Admin — لوحة إدارة كل الشركات
# =====================================================================
def _is_platform_owner(user):
    """التحقق من أن المستخدم هو مالك المنصة فعلياً (superuser على الـ public schema فقط)"""
    if not user.is_active or not user.is_superuser:
        return False
    from django.db import connection as db_conn
    return getattr(db_conn, 'schema_name', 'public') == 'public'

@user_passes_test(_is_platform_owner, login_url='/secure-portal/login/')
def super_admin_dashboard(request):

    # حماية مزدوجة: حتى لو عدى الـ decorator، نتأكد إنه على public schema
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('<h1>403 — Access Denied</h1><p>هذه الصفحة مخصصة لمالك المنصة فقط.</p>')

    action = request.POST.get('action')
    tenant_id = request.POST.get('tenant_id')

    if request.method == 'POST' and tenant_id:
        target = get_object_or_404(Client, id=tenant_id)
        if action == 'suspend':
            target.status = 'suspended'
            target.is_active = False
            target.save(update_fields=['status', 'is_active'])
            PlatformEvent.objects.create(
                event_type='suspension', tenant_schema=target.schema_name,
                tenant_name=target.name, user_name=request.user.username,
                description=f"تعليق حساب «{target.name}» بواسطة {request.user.username}",
            )
        elif action == 'activate':
            target.status = 'active'
            target.is_active = True
            target.save(update_fields=['status', 'is_active'])
        elif action == 'flag_fraud':
            target.is_fraud_flagged = True
            target.save(update_fields=['is_fraud_flagged'])
            PlatformEvent.objects.create(
                event_type='fraud_flag', tenant_schema=target.schema_name,
                tenant_name=target.name, user_name=request.user.username,
                description=f"تعليم احتيال على «{target.name}»",
            )
        elif action == 'unflag_fraud':
            target.is_fraud_flagged = False
            target.save(update_fields=['is_fraud_flagged'])
        elif action == 'extend_trial':
            base_date = target.trial_ends_at or timezone.localdate()
            target.trial_ends_at = base_date + timedelta(days=3)
            target.save(update_fields=['trial_ends_at'])
        elif action == 'activate_subscription':
            plan = request.POST.get('plan', 'silver')
            billing_period = request.POST.get('billing_period', 'monthly')
            plan_prices = {'silver': 780, 'gold': 1250, 'empire': 1800}
            period_days = {'monthly': 30, 'quarterly': 90, 'semi_annual': 180, 'annual': 365}
            period_discounts = {'monthly': Decimal('0'), 'quarterly': Decimal('0.09'),
                                'semi_annual': Decimal('0.125'), 'annual': Decimal('0.25')}
            period_labels = {'monthly': 'شهري', 'quarterly': 'ربع سنوي',
                             'semi_annual': 'نصف سنوي', 'annual': 'سنوي'}
            months_map = {'monthly': 1, 'quarterly': 3, 'semi_annual': 6, 'annual': 12}

            base_price = Decimal(str(plan_prices.get(plan, 780)))
            discount = period_discounts.get(billing_period, Decimal('0'))
            months = months_map.get(billing_period, 1)
            total = (base_price * months * (1 - discount)).quantize(Decimal('1'))
            days = period_days.get(billing_period, 30)

            target.plan = plan
            target.status = 'active'
            target.is_active = True
            target.subscription_end_date = timezone.localdate() + timedelta(days=days)
            target.save(update_fields=['plan', 'status', 'is_active', 'subscription_end_date'])

            PlatformEvent.objects.create(
                event_type='subscription', tenant_schema=target.schema_name,
                tenant_name=target.name, user_name=request.user.username,
                description=f"تفعيل اشتراك «{target.name}» — {plan} {period_labels.get(billing_period)} — {total} ج.م",
                metadata={'plan': plan, 'period': billing_period, 'total': str(total)},
            )

            messages.success(request,
                f'تم تفعيل اشتراك «{target.name}» — باقة {target.get_plan_display()} '
                f'({period_labels.get(billing_period, billing_period)}) — {total} ج.م — '
                f'ينتهي {target.subscription_end_date}')

        elif action == 'renew_subscription':
            # تجديد الاشتراك الحالي بنفس الباقة لمدة 30 يوم إضافية
            if target.subscription_end_date:
                base_date = max(target.subscription_end_date, timezone.localdate())
            else:
                base_date = timezone.localdate()
            target.subscription_end_date = base_date + timedelta(days=30)
            target.status = 'active'
            target.is_active = True
            target.save(update_fields=['subscription_end_date', 'status', 'is_active'])
            PlatformEvent.objects.create(
                event_type='subscription', tenant_schema=target.schema_name,
                tenant_name=target.name, user_name=request.user.username,
                description=f"تجديد اشتراك «{target.name}» — {target.get_plan_display()} — حتى {target.subscription_end_date}",
            )
            messages.success(request, f'تم تجديد اشتراك «{target.name}» حتى {target.subscription_end_date}')

        elif action == 'activate_ai_addon':
            # تفعيل حزمة AI Studio للشركة
            from clients.models import TenantSubscription, AIAddonPackage
            addon_slug = request.POST.get('ai_addon_slug', 'ai-basic')
            addon = AIAddonPackage.objects.filter(slug=addon_slug, is_active=True).first()
            if not addon:
                messages.error(request, 'حزمة AI غير موجودة.')
            else:
                sub, _ = TenantSubscription.objects.get_or_create(tenant=target)
                sub.ai_addon = addon
                sub.is_active = True
                sub.save(update_fields=['ai_addon', 'is_active', 'updated_at'])
                PlatformEvent.objects.create(
                    event_type='subscription', tenant_schema=target.schema_name,
                    tenant_name=target.name, user_name=request.user.username,
                    description=f"تفعيل حزمة AI «{addon.name}» لشركة «{target.name}»",
                    metadata={'ai_addon': addon_slug},
                )
                messages.success(request, f'تم تفعيل حزمة AI «{addon.name}» لشركة «{target.name}»')

        elif action == 'deactivate_ai_addon':
            # إلغاء حزمة AI Studio
            from clients.models import TenantSubscription
            try:
                sub = TenantSubscription.objects.get(tenant=target)
                old_addon = sub.ai_addon.name if sub.ai_addon else ''
                sub.ai_addon = None
                sub.save(update_fields=['ai_addon', 'updated_at'])
                PlatformEvent.objects.create(
                    event_type='subscription', tenant_schema=target.schema_name,
                    tenant_name=target.name, user_name=request.user.username,
                    description=f"إلغاء حزمة AI «{old_addon}» من شركة «{target.name}»",
                )
                messages.success(request, f'تم إلغاء حزمة AI من «{target.name}»')
            except TenantSubscription.DoesNotExist:
                messages.error(request, 'لا يوجد اشتراك لهذه الشركة.')

        elif action == 'grant_ai_bonus':
            # 🎁 هدية رصيد AI Studio من السوبر أدمن
            from clients.models import AIBonusGrant
            try:
                designs = int(request.POST.get('grant_designs', 0) or 0)
                whatsapp_n = int(request.POST.get('grant_whatsapp', 0) or 0)
                watermarks_n = int(request.POST.get('grant_watermarks', 0) or 0)
            except ValueError:
                designs = whatsapp_n = watermarks_n = 0

            reason = request.POST.get('grant_reason', '').strip()
            expires_days = request.POST.get('grant_expires_days', '').strip()
            expires_at = None
            if expires_days:
                try:
                    expires_at = timezone.now() + timedelta(days=int(expires_days))
                except ValueError:
                    pass

            if designs + whatsapp_n + watermarks_n <= 0:
                messages.error(request, '❌ يجب تحديد رصيد واحد على الأقل (تصاميم / واتساب / علامات مائية).')
            else:
                grant = AIBonusGrant.objects.create(
                    tenant=target,
                    granted_designs=designs,
                    granted_whatsapp=whatsapp_n,
                    granted_watermarks=watermarks_n,
                    reason=reason or 'هدية من إدارة المنصة',
                    granted_by=request.user,
                    expires_at=expires_at,
                )
                PlatformEvent.objects.create(
                    event_type='other', tenant_schema=target.schema_name,
                    tenant_name=target.name, user_name=request.user.username,
                    description=f"🎁 منح هدية لشركة «{target.name}» — {designs} تصميم، {whatsapp_n} واتساب، {watermarks_n} علامة مائية",
                )
                messages.success(
                    request,
                    f'🎁 تم منح «{target.name}» هدية: {designs} تصميم + {whatsapp_n} واتساب + {watermarks_n} علامة مائية' +
                    (f' (تنتهي خلال {expires_days} يوم)' if expires_at else '')
                )

        elif action == 'revoke_bonus':
            from clients.models import AIBonusGrant
            grant_id = request.POST.get('grant_id')
            try:
                grant = AIBonusGrant.objects.get(pk=grant_id, tenant=target)
                grant.is_active = False
                grant.save(update_fields=['is_active'])
                messages.success(request, f'تم إلغاء الهدية #{grant.pk} من «{target.name}»')
            except AIBonusGrant.DoesNotExist:
                messages.error(request, 'الهدية غير موجودة.')

        elif action == 'delete_tenant':
            # ⚠️ حذف نهائي للشركة — يحذف الـ schema بالكامل
            confirm_name = request.POST.get('confirm_name', '').strip()
            if confirm_name != target.schema_name:
                messages.error(request, 'فشل الحذف: اسم التأكيد لا يتطابق مع اسم الـ Schema.')
            else:
                tenant_name = target.name
                schema = target.schema_name
                try:
                    # حذف السجلات المرتبطة بـ PROTECT FK قبل حذف الـ Tenant
                    EscrowLedger.objects.filter(client=target).delete()
                    GlobalB2BMarketplace.objects.filter(tenant=target).delete()
                    BidOffer.objects.filter(seller=target).delete()
                    BlindBiddingRequest.objects.filter(buyer=target).update(winner=None)
                    Domain.objects.filter(tenant=target).delete()
                    target.delete(force_drop=True)
                    PlatformEvent.objects.create(
                        event_type='other', tenant_schema=schema,
                        tenant_name=tenant_name, user_name=request.user.username,
                        description=f"🗑️ حذف نهائي لشركة «{tenant_name}» (schema: {schema})",
                    )
                    messages.success(request, f'تم حذف شركة «{tenant_name}» نهائياً.')
                except Exception as e:
                    logger.error("[SUPER ADMIN] Failed to delete tenant %s: %s", schema, e)
                    messages.error(request, f'فشل حذف الشركة: {e}')

        return redirect('super_admin_dashboard')

    # ══════════════════════════════════════════════════════════════
    # DATA AGGREGATION — الداتا الضخمة للوحة التحكم
    # ══════════════════════════════════════════════════════════════
    tenants = Client.objects.exclude(schema_name='public').order_by('-created_on')
    today = timezone.localdate()
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # --- Tenant Summary ---
    summary = {
        'total': tenants.count(),
        'trial': tenants.filter(status='trial').count(),
        'active': tenants.filter(status='active').count(),
        'suspended': tenants.filter(status='suspended').count(),
        'fraud': tenants.filter(is_fraud_flagged=True).count(),
        'automotive': tenants.filter(industry='automotive').count(),
        'printing': tenants.filter(industry='printing').count(),
    }

    # --- New signups this month ---
    new_this_month = tenants.filter(created_on__gte=today.replace(day=1)).count()
    new_this_week = tenants.filter(created_on__gte=(today - timedelta(days=7))).count()

    # --- Expiring soon (next 7 days) ---
    expiring_soon = tenants.filter(
        status='active',
        subscription_end_date__isnull=False,
        subscription_end_date__lte=today + timedelta(days=7),
        subscription_end_date__gte=today,
    ).count()

    # --- Trial expired but still in trial status ---
    trial_expired = tenants.filter(status='trial', trial_ends_at__lt=today).count()

    # --- Visitor Analytics ---
    visitor_stats = {}
    try:
        visitors_today = VisitorLog.objects.filter(timestamp__gte=today_start).count()
        unique_ips_today = VisitorLog.objects.filter(
            timestamp__gte=today_start
        ).values('ip_address').distinct().count()
        visitors_week = VisitorLog.objects.filter(timestamp__gte=week_ago).count()
        unique_ips_week = VisitorLog.objects.filter(
            timestamp__gte=week_ago
        ).values('ip_address').distinct().count()

        # أكثر الصفحات زيارة
        top_pages = list(
            VisitorLog.objects.filter(timestamp__gte=week_ago)
            .values('path')
            .annotate(hits=Count('id'))
            .order_by('-hits')[:10]
        )

        # أكثر الشركات نشاطاً
        top_tenants = list(
            VisitorLog.objects.filter(timestamp__gte=week_ago)
            .exclude(tenant_schema='public')
            .exclude(tenant_schema='')
            .values('tenant_schema')
            .annotate(hits=Count('id'))
            .order_by('-hits')[:10]
        )

        # Device breakdown
        device_breakdown = list(
            VisitorLog.objects.filter(timestamp__gte=week_ago)
            .values('device_type')
            .annotate(count=Count('id'))
            .order_by('-count')
        )

        # Average response time
        avg_response = VisitorLog.objects.filter(
            timestamp__gte=today_start, response_time_ms__isnull=False,
        ).aggregate(avg=Avg('response_time_ms'))['avg'] or 0

        # آخر 50 زائر (Live Feed)
        recent_visitors = list(
            VisitorLog.objects.filter(timestamp__gte=today_start)
            .order_by('-timestamp')[:50]
            .values(
                'timestamp', 'ip_address', 'path', 'method',
                'status_code', 'tenant_schema', 'device_type',
                'response_time_ms', 'user__username',
            )
        )

        visitor_stats = {
            'today': visitors_today,
            'unique_today': unique_ips_today,
            'week': visitors_week,
            'unique_week': unique_ips_week,
            'top_pages': top_pages,
            'top_tenants': top_tenants,
            'device_breakdown': device_breakdown,
            'avg_response_ms': round(avg_response),
            'recent': recent_visitors,
        }
    except Exception:
        pass

    # --- Platform Events (Activity Feed) ---
    recent_events = []
    try:
        recent_events = list(
            PlatformEvent.objects.order_by('-timestamp')[:30]
            .values('timestamp', 'event_type', 'tenant_name', 'user_name', 'description')
        )
    except Exception:
        pass

    # --- Tenant Deep Details (per tenant) ---
    from clients.models import TenantSubscription, AIAddonPackage
    tenants_enriched = []
    for t in tenants:
        users_count = 0
        try:
            with schema_context(t.schema_name):
                users_count = User.objects.count()
        except Exception:
            pass

        # جلب حالة AI addon
        ai_addon_name = ''
        try:
            sub = TenantSubscription.objects.get(tenant=t)
            if sub.ai_addon:
                ai_addon_name = sub.ai_addon.name
        except TenantSubscription.DoesNotExist:
            pass

        # 🎁 جلب رصيد الهدايا النشطة
        from clients.models import AIBonusGrant
        active_grants = AIBonusGrant.objects.filter(tenant=t, is_active=True)
        bonus_designs_remaining = 0
        bonus_whatsapp_remaining = 0
        bonus_watermarks_remaining = 0
        for g in active_grants:
            if not g.is_valid:
                continue
            bonus_designs_remaining += g.remaining_designs
            bonus_whatsapp_remaining += g.remaining_whatsapp
            bonus_watermarks_remaining += g.remaining_watermarks

        tenants_enriched.append({
            'obj': t,
            'users_count': users_count,
            'ai_addon_name': ai_addon_name,
            'bonus_designs': bonus_designs_remaining,
            'bonus_whatsapp': bonus_whatsapp_remaining,
            'bonus_watermarks': bonus_watermarks_remaining,
            'active_grants': active_grants,
            'days_left': (t.subscription_end_date - today).days + 1 if t.subscription_end_date and t.subscription_end_date >= today else (
                (t.trial_ends_at - today).days + 1 if t.status == 'trial' and t.trial_ends_at and t.trial_ends_at >= today else 0
            ),
        })

    # --- طلبات شراء التصاميم المعلّقة ---
    pending_design_purchases = DesignPurchase.objects.filter(
        status__in=['pending', 'awaiting_confirm']
    ).select_related('customer', 'package').order_by('-created_at')[:20]

    # --- طلبات طباعة التصاميم ---
    from clients.models import DesignPrintRequest
    pending_print_requests = DesignPrintRequest.objects.filter(
        status__in=['pending', 'quoted']
    ).select_related('customer', 'design').order_by('-created_at')[:30]

    # --- حزم AI المتاحة ---
    ai_addons = list(AIAddonPackage.objects.filter(is_active=True).order_by('sort_order').values('slug', 'name', 'monthly_price'))

    # --- الباقات للمودال ---
    plan_prices_json = json.dumps({'silver': 780, 'gold': 1250, 'empire': 1800})
    period_discounts_json = json.dumps({'monthly': 0, 'quarterly': 0.09, 'semi_annual': 0.125, 'annual': 0.25})
    period_months_json = json.dumps({'monthly': 1, 'quarterly': 3, 'semi_annual': 6, 'annual': 12})

    return render(request, 'clients/super_admin.html', {
        'tenants': tenants_enriched,
        'summary': summary,
        'today': today,
        'new_this_month': new_this_month,
        'new_this_week': new_this_week,
        'expiring_soon': expiring_soon,
        'trial_expired': trial_expired,
        'visitor_stats': visitor_stats,
        'recent_events': recent_events,
        'plan_prices_json': plan_prices_json,
        'period_discounts_json': period_discounts_json,
        'period_months_json': period_months_json,
        'ai_addons': ai_addons,
        'pending_design_purchases': pending_design_purchases,
        'pending_print_requests': pending_print_requests,
    })


# =====================================================================
# 🚪 الدخول كمالك المنصة على أي شركة (Tenant Impersonation)
# =====================================================================

@login_required
@user_passes_test(lambda u: u.is_superuser)
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

    schema_name = token_data.get('schema_name', '')
    current_schema = getattr(connection, 'schema_name', 'public')

    if current_schema == 'public' or current_schema != schema_name:
        return redirect(f'/{admin_url}/login/')

    # --- إيجاد أو إنشاء admin user على هذا الـ tenant ---
    from django.contrib.auth import login as auth_login

    # ابحث عن أول superuser/staff على الـ tenant
    admin_user = User.objects.filter(is_staff=True, is_active=True).order_by('-is_superuser', '-date_joined').first()

    if not admin_user:
        # أنشئ superuser مؤقت إذا مفيش
        admin_user = User.objects.create_superuser(
            username=f"mousstec_admin",
            email="admin@mousstec.com",
            password=secrets.token_urlsafe(20),
            first_name="Mouss Tec",
            last_name="Platform Admin",
        )

    # Login
    auth_login(request, admin_user, backend='clients.backends.CaseInsensitiveEmailBackend')

    admin_url = os.getenv('ADMIN_URL', 'secure-portal')
    return redirect(f'/{admin_url}/')


# =====================================================================
# 📚 صفحة المميزات الكاملة
# =====================================================================
def features_page(request):
    return render(request, 'clients/features.html')


# =====================================================================
# 🤖 AI Assistant — مساعد ذكي للموقع الرئيسي (Landing Page)
# يعمل بـ Gemini (مجاني) — يعرف كل شيء عن القطاعين
# =====================================================================

LANDING_BOT_KNOWLEDGE = """أنت "مساعد Mouss Tec الذكي" — مساعد مبيعات وتعليم متخصص في منصة Mouss Tec ERP.
أنت موجود على الموقع الرئيسي (Landing Page) للمنصة. مهمتك:
1. تعليم الزوار كل شيء عن النظام بالتفصيل
2. الإجابة عن أسئلة الأسعار والباقات بدقة
3. إقناع الزوار بتجربة النظام مجاناً
4. شرح الفرق بين القطاعين (سيارات + طباعة)
5. تعلّم من أسئلة الناس — إذا سألك حد سؤال مش عارف إجابته، قوله بصراحة وارشده للدعم

🏭 القطاع الأول: السيارات وقطع الغيار
نظام شامل لمراكز الصيانة وتجار قطع الغيار يشمل:
- فواتير مبيعات ومشتريات ومرتجعات (مذكرات دائنة/مدينة)
- مخزون ذكي: باركود، جرد، تنبيه نقص، تحويل بين فروع
- محاسبة كاملة: قيد مزدوج، دليل حسابات، أرباح وخسائر
- خزائن ومدفوعات: تحصيل من عملاء، صرف لموردين، تحويل بين خزائن
- كروت صيانة: إنشاء كرت → إضافة خدمات + قطع غيار → إغلاق = فاتورة تلقائية
- سجل مركبات العملاء: كل عميل مربوط بسياراته وتاريخ صيانتها
- سوق B2B المركزي: عرض/طلب قطع غيار + مزادات عكسية (الموردون يتنافسون على أفضل سعر)
- تقارير شاملة: مبيعات، مخزون، كشف حساب عميل/مورد
- مستشار ذكي (AI Copilot): بوت داخلي يرد على أسئلتك من بيانات شركتك الفعلية

🎨 القطاع الثاني: المطابع والتصميم
نظام متخصص لشركات الطباعة ومكاتب التصميم:
- طلبات طباعة: إنشاء طلب → إضافة مهام (تيشرت، كروت، بوسترات...) → تسعير تلقائي
- إدارة المصممين: سجل أعمال كل مصمم + تقييمات + ساعات العمل
- ماكينات الطباعة: حاسبة تكلفة CMYK لكل ماكينة → تعرف ربحك الحقيقي
- مخزون الخامات: ورق، أحبار، خامات تيشرت — مع تنبيه نقص تلقائي
- ملفات المشاريع: رفع ملفات التصميم وحفظها مع كل طلب
- صلاحيات الموظفين: تحكم كامل — مين يشوف الخزينة، مين يعدل الطلبات
- AI Studio (إضافة مدفوعة): توليد تصاميم بالذكاء الاصطناعي + علامة مائية ذكية
- مستشار ذكي (AI Copilot): بوت داخلي متصل بالداتابيز — اسأله "بيعنا كام؟" ويرد من بياناتك الحقيقية

💰 الباقات والأسعار:

📌 باقات السيارات:
- سيلفر (780 ج/شهر — بدل 1,000): فرع واحد + موظف واحد + خزينة واحدة — مناسب للورش الصغيرة
- جولد (1,250 ج/شهر — بدل 2,000): فرعين + 4 موظفين + خزينتين + سوق B2B + تقارير متقدمة — الأكثر طلباً
- Empire (1,800 ج/شهر — بدل 3,000): غير محدود + مزادات B2B + Escrow مالي + دعم أولوية — للشركات الكبيرة

📌 باقات الطباعة:
- Print Basic (550 ج/شهر): فرع + مصمم + ماكينة — للاستوديوهات الصغيرة
- Print Pro (880 ج/شهر): فرعين + 4 مصممين + 3 ماكينات + سجل أعمال + CMYK — الأكثر طلباً
- Print Enterprise (2,000 ج/شهر): غير محدود + تقارير ربحية كل ماكينة + دعم أولوية

📌 نظام الإضافات: إضافة موظف/فرع/خزينة/مصمم/ماكينة بـ 125 ج/شهر في أي وقت
📌 خصومات: 9% ربع سنوي | 12.5% نصف سنوي | 25% سنوي
📌 تجربة مجانية: 3 أيام بدون دفع لكل الباقات
📌 مكافأة ولاء: التجديد قبل انتهاء الاشتراك = 5 أيام مجانية إضافية

📌 باقات متجر التصميم بالذكاء الاصطناعي (AI Design Store — دفعة واحدة):
👤 للعملاء: 2 تصميم = 99 ج.م | 4 تصاميم = 189 ج.م ⭐ | 8 تصاميم = 369 ج.م
🎨 للمصممين: 15 تصميم = 599 ج.م | 25 تصميم = 949 ج.م ⭐ | 50 تصميم = 1,849 ج.م | 100 تصميم = 3,249 ج.م
🎁 تصميم واحد مجاني للتجربة بدون دفع

🔒 طرق الدفع:
- فودافون كاش: حوّل المبلغ وابعت الإيصال على واتساب
- فيزا/ماستركارد: دفع فوري آمن عبر Paymob

🚫 قواعد صارمة:
- لا تخترع معلومات — إذا مش عارف، قل "أنصحك تتواصل مع فريق الدعم"
- كن ودود ومفيد واستخدم عربي مصري بسيط
- ردودك قصيرة ومفيدة (3-5 جمل كحد أقصى)
- لما حد يسأل عن الأسعار، اسأله عن نشاطه الأول (سيارات ولا طباعة) عشان تديله الباقة المناسبة
- شجّع الزوار على التجربة المجانية 3 أيام
- استخدم إيموجي باعتدال
"""


@csrf_exempt
def ai_assistant_api(request):
    """🤖 API endpoint للمساعد الذكي — يعمل بـ Gemini (مجاني)"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    # ── Rate limiting بسيط بالـ IP (10 رسائل في الدقيقة) ──
    client_ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
    rate_key = f'ai_chat_rate:{client_ip}'
    request_count = cache.get(rate_key, 0)
    if request_count >= 10:
        return JsonResponse({
            'reply': '⚠️ عدد الرسائل كثير. حاول مرة أخرى بعد دقيقة.',
            'status': 'rate_limited'
        }, status=429)
    cache.set(rate_key, request_count + 1, 60)

    try:
        data = json.loads(request.body)
        user_message = data.get('message', '').strip()
        conversation_history = data.get('history', [])

        if not user_message:
            return JsonResponse({'error': 'الرسالة فارغة'}, status=400)

        if len(user_message) > 1000:
            return JsonResponse({'error': 'الرسالة طويلة جداً'}, status=400)

        # ── Gemini API call (مجاني) ──
        gemini_reply = None
        try:
            from inventory.ai_services import call_gemini_layer

            ai_enabled = getattr(settings, 'ENABLE_AI_PREDICTIONS', False)
            api_key = getattr(settings, 'AI_VISION_API_KEY', None)

            if ai_enabled and api_key:
                # بناء سجل المحادثة (آخر 6 رسائل فقط)
                messages = [
                    {"role": "system", "content": LANDING_BOT_KNOWLEDGE},
                ]
                for msg in conversation_history[-6:]:
                    role = msg.get('role', 'user')
                    content = msg.get('content', '')
                    if role == 'user' and content:
                        messages.append({"role": "user", "content": content})
                    elif role == 'assistant' and content:
                        messages.append({"role": "assistant", "content": content})

                messages.append({"role": "user", "content": user_message})
                gemini_reply = call_gemini_layer(messages, json_mode=False, max_retries=1)
        except Exception as e:
            logger.warning(f'AI Assistant Gemini fallback: {e}')

        if gemini_reply:
            return JsonResponse({'reply': gemini_reply, 'status': 'ok'})

        # ── Fallback ذكي بدون Gemini ──
        reply = _landing_bot_local_reply(user_message)
        return JsonResponse({'reply': reply, 'status': 'ok'})

    except json.JSONDecodeError:
        return JsonResponse({'error': 'بيانات غير صالحة'}, status=400)
    except Exception as e:
        logger.error(f'AI Assistant error: {e}')
        return JsonResponse({
            'reply': '⚠️ حدث خطأ مؤقت. حاول مرة أخرى بعد قليل.',
            'status': 'error'
        })


def _landing_bot_local_reply(msg):
    """ردود محلية ذكية للبوت لما Gemini مش متاح"""
    q = msg.lower()

    # تحيات
    if any(k in q for k in ['hi', 'hello', 'اهلا', 'أهلا', 'مرحبا', 'سلام', 'ازيك', 'إزيك', 'صباح', 'مساء']):
        return (
            'أهلاً بيك! 👋 أنا مساعد Mouss Tec.\n'
            'أقدر أساعدك تعرف كل حاجة عن النظام:\n'
            '🔧 نظام ورش السيارات وقطع الغيار\n'
            '🎨 نظام المطابع وشركات التصميم\n'
            '💰 الباقات والأسعار\n'
            '🚀 التجربة المجانية 3 أيام\n\n'
            'اسألني عن أي حاجة!'
        )

    # أسعار / باقات
    if any(k in q for k in ['سعر', 'أسعار', 'اسعار', 'باقة', 'باقات', 'كام', 'تكلفة', 'price', 'plan', 'فرق']):
        return (
            '💰 باقاتنا مرنة وتناسب كل الأحجام:\n\n'
            '🔧 باقات السيارات:\n'
            '• سيلفر: 780 ج/شهر (فرع + موظف)\n'
            '• جولد: 1,250 ج/شهر (فرعين + 4 موظفين + B2B) ⭐\n'
            '• Empire: 1,800 ج/شهر (غير محدود)\n\n'
            '🎨 باقات الطباعة:\n'
            '• Print Basic: 550 ج/شهر\n'
            '• Print Pro: 880 ج/شهر ⭐\n'
            '• Print Enterprise: 2,000 ج/شهر\n\n'
            '🎁 خصم 9% ربع سنوي | 12.5% نصف سنوي | 25% سنوي\n'
            '🆓 جرّب مجاناً 3 أيام بدون دفع!'
        )

    # سيارات
    if any(k in q for k in ['سيار', 'ورش', 'صيان', 'قطع غيار', 'ميكانيك', 'كرت', 'garage', 'auto', 'car']):
        return (
            '🔧 نظام Mouss Tec للسيارات يشمل:\n\n'
            '• فواتير مبيعات ومشتريات ومرتجعات\n'
            '• مخزون ذكي مع باركود وتنبيه نقص\n'
            '• كروت صيانة: افتح كرت → أضف خدمات وقطع غيار → أغلقه = فاتورة تلقائية\n'
            '• سجل مركبات العملاء وتاريخ الصيانة\n'
            '• سوق B2B: اطلب قطع غيار من تجار تانيين\n'
            '• خزائن ومحاسبة كاملة\n'
            '• تقارير أرباح وخسائر\n\n'
            '🆓 جرّب النظام مجاناً 3 أيام من صفحة الأسعار!'
        )

    # طباعة / تصميم
    if any(k in q for k in ['طباع', 'مطبع', 'تصميم', 'مصمم', 'print', 'design', 'بوستر', 'كارت', 'تيشرت']):
        return (
            '🎨 نظام Mouss Tec للمطابع يشمل:\n\n'
            '• طلبات طباعة مع مهام مخصصة (تيشرت، كروت، بوسترات...)\n'
            '• إدارة المصممين + سجل أعمال + تقييمات\n'
            '• حاسبة تكلفة CMYK لكل ماكينة\n'
            '• مخزون خامات (ورق، حبر) مع تنبيه نقص\n'
            '• رفع ملفات المشاريع وحفظها\n'
            '• صلاحيات موظفين دقيقة\n'
            '• AI Studio: توليد تصاميم بالذكاء الاصطناعي (إضافة اختيارية)\n'
            '• متجر التصميم AI: باقات تصميم للعملاء (99-369 ج.م) والمصممين (599-3,249 ج.م)\n\n'
            '🆓 جرّب مجاناً 3 أيام + تصميم AI مجاني!'
        )

    # تجربة مجانية
    if any(k in q for k in ['مجان', 'تجرب', 'trial', 'free', 'جرب', 'ابدأ', 'اشتراك', 'سجل']):
        return (
            '🚀 التجربة المجانية سهلة جداً:\n\n'
            '1. اذهب لصفحة الأسعار\n'
            '2. اختر الباقة المناسبة (سيارات أو طباعة)\n'
            '3. اضغط "جرّب مجاناً 3 أيام"\n'
            '4. سجّل بياناتك وابدأ فوراً!\n\n'
            '✅ بدون بطاقة ائتمان\n'
            '✅ كل المميزات متاحة\n'
            '✅ لو عجبك، اشترك من داخل النظام'
        )

    # دفع
    if any(k in q for k in ['دفع', 'فيزا', 'فودافون', 'كاش', 'payment', 'pay', 'تحويل']):
        return (
            '💳 طرق الدفع المتاحة:\n\n'
            '1. فودافون كاش: حوّل المبلغ وابعت الإيصال على واتساب\n'
            '2. فيزا/ماستركارد: دفع فوري آمن عبر Paymob\n\n'
            'اختر الباقة من صفحة الأسعار وهتلاقي كل التفاصيل!'
        )

    # فاتورة / مخزون / محاسبة
    if any(k in q for k in ['فاتور', 'مبيعات', 'مشتريات', 'مخزون', 'محاسب', 'خزين', 'تقرير']):
        return (
            '📊 النظام يشمل كل ما تحتاجه:\n\n'
            '• فواتير مبيعات ومشتريات ومرتجعات\n'
            '• مخزون مع باركود وجرد وتحويل بين فروع\n'
            '• محاسبة كاملة: قيد مزدوج + أرباح وخسائر\n'
            '• خزائن ومدفوعات مع تحصيل وصرف\n'
            '• تقارير شاملة لكل شيء\n\n'
            'عاوز تعرف تفاصيل أكتر عن حاجة معينة؟'
        )

    # Fallback
    return (
        'أقدر أساعدك تعرف أكتر عن:\n\n'
        '🔧 نظام السيارات والورش\n'
        '🎨 نظام المطابع والتصميم\n'
        '💰 الباقات والأسعار\n'
        '🚀 التجربة المجانية\n'
        '💳 طرق الدفع\n\n'
        'اسألني عن أي حاجة من دول! 😊'
    )


# =====================================================================
# 🛍️ سوق العملاء والمناقصات المجهولة (Customer Marketplace)
# =====================================================================

def _marketplace_auth(request):
    """Verify marketplace customer session token from cookie."""
    token = request.COOKIES.get('mp_session')
    if not token:
        return None
    try:
        return MarketplaceCustomer.objects.get(session_token=token, is_verified=True, is_blocked=False)
    except MarketplaceCustomer.DoesNotExist:
        return None


def marketplace_home(request):
    """
    الصفحة الرئيسية المحايدة — تخيير المستخدم بين القطاعين.
    لو مسجل، يروح للوحة مباشرة.
    """
    customer = _marketplace_auth(request)
    if customer:
        return redirect('/marketplace/dashboard/')
    return render(request, 'clients/marketplace/choose_sector.html')


def marketplace_automotive(request):
    """صفحة دخول/تسجيل سوق السيارات."""
    customer = _marketplace_auth(request)
    if customer:
        return redirect('/marketplace/dashboard/')
    return render(request, 'clients/marketplace/automotive.html', {'sector': 'automotive'})


def marketplace_printing(request):
    """صفحة دخول/تسجيل سوق الطباعة والتصميم."""
    customer = _marketplace_auth(request)
    if customer:
        return redirect('/marketplace/dashboard/')
    return render(request, 'clients/marketplace/printing.html', {'sector': 'printing'})


@csrf_exempt
def marketplace_register(request):
    """تسجيل عميل جديد في السوق — خطوة 1."""
    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    # 🛡️ Rate limiting — 3 registrations per minute per IP (prevent spam)
    client_ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
    reg_rate_key = f'otp_reg_rate:{client_ip}'
    reg_count = cache.get(reg_rate_key, 0)
    if reg_count >= 3:
        return JsonResponse({"error": "طلبات كثيرة. انتظر دقيقة ثم حاول مرة أخرى."}, status=429)
    cache.set(reg_rate_key, reg_count + 1, 60)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "بيانات غير صالحة"}, status=400)

    customer_type = data.get('customer_type', 'individual')
    full_name = data.get('full_name', '').strip()
    company_name = data.get('company_name', '').strip()
    phone = data.get('phone', '').strip()
    job_title = data.get('job_title', '').strip()
    sector = data.get('sector', 'automotive')
    city = data.get('city', '').strip()
    email = data.get('email', '').strip()

    if not full_name or not phone:
        return JsonResponse({"error": "الاسم ورقم الموبايل مطلوبان"}, status=400)

    if customer_type == 'company' and not company_name:
        return JsonResponse({"error": "اسم الشركة مطلوب"}, status=400)

    if sector not in ('automotive', 'printing'):
        return JsonResponse({"error": "قطاع غير صالح"}, status=400)

    # Normalize phone — Egyptian format: +20 + 10-digit mobile (starts with 1)
    cleaned_phone = phone
    if not phone.startswith('+'):
        digits = phone.lstrip('0')
        if len(digits) == 10 and digits.startswith('1'):
            cleaned_phone = f'+20{digits}'  # 10-digit mobile → +20 prefix
        elif len(digits) == 11 and digits.startswith('01'):
            cleaned_phone = f'+2{digits}'   # 11-digit (with leading 0) → +2 prefix
        elif len(digits) == 12 and digits.startswith('201'):
            cleaned_phone = f'+{digits}'    # already has country code
        else:
            cleaned_phone = phone           # keep as-is, validation will catch bad ones

    # Check existing — redirect to login
    existing = MarketplaceCustomer.objects.filter(phone=cleaned_phone).first()
    if existing:
        return JsonResponse({
            "status": "existing",
            "message": "الرقم مسجل بالفعل. ادخل من تسجيل الدخول.",
            "is_existing": True,
        }, status=409)

    try:
        customer = MarketplaceCustomer.objects.create(
            customer_type=customer_type,
            full_name=full_name,
            company_name=company_name,
            phone=cleaned_phone,
            email=email or None,
            job_title=job_title,
            sector=sector,
            city=city,
            is_verified=True,
        )
    except Exception as e:
        logger.error(f"[MARKETPLACE] Failed to create customer: {e}")
        return JsonResponse({"error": "فشل التسجيل. حاول مرة أخرى."}, status=500)

    logger.info(f"[MARKETPLACE] New customer {cleaned_phone[:6]}*** registered")
    response = JsonResponse({
        "status": "verified",
        "message": "تم تسجيلك بنجاح!",
        "redirect": "/marketplace/dashboard/",
        "is_existing": False,
    })
    response.set_cookie(
        'mp_session', str(customer.session_token),
        max_age=60 * 60 * 24 * 30, httponly=True, samesite='Lax',
        secure=not settings.DEBUG,
    )
    return response


def _send_otp_via_channel(phone, otp, **kwargs):
    """
    إرسال OTP عبر Twilio SMS، Vonage، WhatsApp، أو Email.
    يتم اختيار البوابة بناءً على settings.OTP_DELIVERY_PROVIDER.
    Provider options: 'twilio', 'vonage', 'whatsapp_meta', 'email', 'console' (default).
    """
    provider = getattr(settings, 'OTP_DELIVERY_PROVIDER', 'console')
    message = f"كود التحقق Mouss Tec: {otp}\nصالح لمدة 10 دقائق."

    if provider == 'twilio':
        try:
            from twilio.rest import Client as TwilioClient
            account_sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
            auth_token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
            from_number = getattr(settings, 'TWILIO_FROM_NUMBER', '')
            if not all([account_sid, auth_token, from_number]):
                logger.error("[OTP/Twilio] Missing credentials in settings")
                return False
            client = TwilioClient(account_sid, auth_token)
            client.messages.create(body=message, from_=from_number, to=phone)
            logger.info(f"[OTP/Twilio] SMS sent to {phone}")
            return True
        except ImportError:
            logger.error("[OTP/Twilio] twilio package not installed: pip install twilio")
            return False
        except Exception as e:
            logger.error(f"[OTP/Twilio] Failed: {e}")
            return False

    elif provider == 'vonage':
        try:
            import vonage
            api_key = getattr(settings, 'VONAGE_API_KEY', '')
            api_secret = getattr(settings, 'VONAGE_API_SECRET', '')
            sender = getattr(settings, 'VONAGE_SENDER', 'MoussTec')
            if not all([api_key, api_secret]):
                logger.error("[OTP/Vonage] Missing credentials")
                return False
            client = vonage.Client(key=api_key, secret=api_secret)
            sms = vonage.Sms(client)
            sms.send_message({'from': sender, 'to': phone.lstrip('+'), 'text': message})
            logger.info(f"[OTP/Vonage] SMS sent to {phone}")
            return True
        except ImportError:
            logger.error("[OTP/Vonage] vonage package not installed: pip install vonage")
            return False
        except Exception as e:
            logger.error(f"[OTP/Vonage] Failed: {e}")
            return False

    elif provider == 'whatsapp_meta':
        try:
            import requests as _req
            access_token = getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '')
            phone_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '')
            if not all([access_token, phone_id]):
                logger.error("[OTP/WhatsApp] Missing WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID")
                return False
            url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
            # Try text message first (works if user messaged you first within 24h)
            # Falls back to template if configured
            template_name = getattr(settings, 'WHATSAPP_OTP_TEMPLATE', '')
            if template_name:
                payload = {
                    "messaging_product": "whatsapp",
                    "to": phone.lstrip('+'),
                    "type": "template",
                    "template": {
                        "name": template_name,
                        "language": {"code": "ar"},
                        "components": [{"type": "body", "parameters": [{"type": "text", "text": str(otp)}]}],
                    },
                }
            else:
                payload = {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": phone.lstrip('+'),
                    "type": "text",
                    "text": {"body": message},
                }
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            r = _req.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                logger.info(f"[OTP/WhatsApp] sent to {phone[:6]}***")
                return True
            logger.error(f"[OTP/WhatsApp] HTTP {r.status_code}: {r.text[:300]}")
            return False
        except Exception as e:
            logger.error(f"[OTP/WhatsApp] Failed: {e}")
            return False

    elif provider == 'email':
        try:
            from django.core.mail import send_mail
            email_addr = kwargs.get('email', '')
            if not email_addr:
                logger.error("[OTP/Email] No email address provided")
                return False
            send_mail(
                subject='Mouss Tec — كود التحقق',
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email_addr],
                html_message=f'''
                <div dir="rtl" style="font-family:Arial,sans-serif;max-width:400px;margin:0 auto;padding:20px;">
                    <h2 style="color:#2563eb;">Mouss Tec</h2>
                    <p>كود التحقق الخاص بك:</p>
                    <div style="background:#f1f5f9;padding:15px;border-radius:8px;text-align:center;margin:15px 0;">
                        <span style="font-size:32px;font-weight:bold;letter-spacing:8px;color:#1e293b;">{otp}</span>
                    </div>
                    <p style="color:#64748b;font-size:13px;">صالح لمدة 10 دقائق. لا تشاركه مع أحد.</p>
                </div>
                ''',
                fail_silently=False,
            )
            logger.info(f"[OTP/Email] sent to {email_addr[:4]}***")
            return True
        except Exception as e:
            logger.error(f"[OTP/Email] Failed: {e}")
            return False

    # Default: console mode (no real delivery)
    logger.warning(
        f"[OTP-CONSOLE] phone={phone[:6]}*** — "
        f"اضبط OTP_DELIVERY_PROVIDER في settings (twilio/vonage/whatsapp_meta/email)"
    )
    return False


@csrf_exempt
def marketplace_verify_otp(request):
    """التحقق من كود OTP — معطل (تم إلغاء OTP)."""
    return JsonResponse({"error": "OTP verification is disabled. Use direct login."}, status=410)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "بيانات غير صالحة"}, status=400)

    phone = data.get('phone', '')
    code = data.get('otp', '')

    customer = MarketplaceCustomer.objects.filter(phone=phone).first()
    if not customer:
        return JsonResponse({"error": "رقم غير مسجل"}, status=404)

    if customer.verify_otp(code):
        response = JsonResponse({
            "status": "verified",
            "message": "تم التحقق بنجاح!",
            "redirect": "/marketplace/dashboard/",
        })
        response.set_cookie(
            'mp_session', str(customer.session_token),
            max_age=60 * 60 * 24 * 30, httponly=True, samesite='Lax',
            secure=not settings.DEBUG,
        )
        return response
    else:
        return JsonResponse({"error": "كود التحقق غير صحيح أو منتهي"}, status=400)


@csrf_exempt
def marketplace_login(request):
    """دخول عميل حالي — إرسال OTP فقط."""
    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    # 🛡️ Rate limiting — 5 OTP requests per minute per IP (prevent OTP flood)
    client_ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
    otp_rate_key = f'otp_login_rate:{client_ip}'
    otp_count = cache.get(otp_rate_key, 0)
    if otp_count >= 5:
        return JsonResponse({"error": "طلبات كثيرة. انتظر دقيقة ثم حاول مرة أخرى."}, status=429)
    cache.set(otp_rate_key, otp_count + 1, 60)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "بيانات غير صالحة"}, status=400)

    phone = data.get('phone', '').strip()
    name = data.get('name', '').strip()
    if not phone:
        return JsonResponse({"error": "رقم الموبايل مطلوب"}, status=400)
    if not name:
        return JsonResponse({"error": "الاسم مطلوب للتحقق"}, status=400)

    # Normalize — Egyptian phone normalization (consistent with marketplace_register)
    cleaned = phone
    if not phone.startswith('+'):
        digits = phone.lstrip('0')
        if len(digits) == 10 and digits.startswith('1'):
            cleaned = f'+20{digits}'
        elif len(digits) == 11 and digits.startswith('01'):
            cleaned = f'+2{digits}'
        elif len(digits) == 12 and digits.startswith('201'):
            cleaned = f'+{digits}'
        else:
            cleaned = phone

    customer = MarketplaceCustomer.objects.filter(phone=cleaned).first()
    if not customer:
        return JsonResponse({"error": "رقم غير مسجل. سجل حساب جديد."}, status=404)

    # 🛡️ Verify identity — name must match (case-insensitive, partial match OK)
    stored_name = (customer.full_name or '').strip().lower()
    input_name = name.strip().lower()
    if not stored_name or input_name not in stored_name:
        # Log failed attempt
        logger.warning(f"[MARKETPLACE] Login failed — name mismatch for {cleaned[:6]}***")
        return JsonResponse({"error": "الاسم غير مطابق للحساب المسجل"}, status=403)

    customer.is_verified = True
    customer.session_token = uuid.uuid4()
    customer.save(update_fields=['is_verified', 'session_token'])
    logger.info(f"[MARKETPLACE] Login: {cleaned[:6]}***")
    response = JsonResponse({
        "status": "verified",
        "message": "تم الدخول بنجاح!",
        "redirect": "/marketplace/dashboard/",
    })
    response.set_cookie(
        'mp_session', str(customer.session_token),
        max_age=60 * 60 * 24 * 30, httponly=True, samesite='Lax',
        secure=not settings.DEBUG,
    )
    return response


def marketplace_dashboard(request):
    """لوحة العميل — طلباته وعروضه."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/')

    requests_qs = customer.requests.all().order_by('-created_at')

    # Auto-expire old requests
    for req in requests_qs.filter(status='open'):
        req.auto_expire()

    context = {
        'customer': customer,
        'requests': requests_qs[:20],
        'stats': {
            'total': requests_qs.count(),
            'open': requests_qs.filter(status='open').count(),
            'accepted': requests_qs.filter(status='accepted').count(),
            'completed': requests_qs.filter(status='completed').count(),
        },
    }
    return render(request, 'clients/marketplace/dashboard.html', context)


@csrf_exempt
def marketplace_create_request(request):
    """إنشاء طلب خدمة / مناقصة جديدة."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول أولاً"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    title = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    urgency = request.POST.get('urgency', 'normal')
    wants_images = request.POST.get('wants_images') == 'true'
    max_budget = request.POST.get('max_budget', '').strip()

    # Validation
    if not title or len(title) < 5:
        return JsonResponse({"error": "العنوان يجب أن يكون 5 أحرف على الأقل"}, status=400)
    if len(title) > 300:
        return JsonResponse({"error": "العنوان طويل جداً (300 حرف كحد أقصى)"}, status=400)
    if not description or len(description) < 10:
        return JsonResponse({"error": "التفاصيل يجب أن تكون 10 أحرف على الأقل"}, status=400)
    if len(description) > 5000:
        return JsonResponse({"error": "التفاصيل طويلة جداً (5000 حرف كحد أقصى)"}, status=400)
    if urgency not in ('normal', 'soon', 'urgent'):
        urgency = 'normal'

    # Parse budget safely
    budget_value = None
    if max_budget:
        try:
            budget_value = Decimal(max_budget)
            if budget_value < 0:
                return JsonResponse({"error": "الميزانية لا يمكن أن تكون سالبة"}, status=400)
        except (ValueError, Exception):
            return JsonResponse({"error": "الميزانية يجب أن تكون رقم صحيح"}, status=400)

    # Validate file sizes (max 5 MB each)
    for fkey in ('attachment_1', 'attachment_2'):
        f = request.FILES.get(fkey)
        if f and f.size > 5 * 1024 * 1024:
            return JsonResponse({"error": f"حجم الصورة {fkey} أكبر من 5 ميجابايت"}, status=400)

    # Rate limit: max 10 open requests per customer at a time
    open_count = customer.requests.filter(status__in=('open', 'reviewing')).count()
    if open_count >= 10:
        return JsonResponse({"error": "وصلت للحد الأقصى من الطلبات المفتوحة (10). أغلق طلبات أولاً."}, status=429)

    # Expiry based on urgency
    expiry_map = {'urgent': 1, 'soon': 3, 'normal': 7}
    days = expiry_map.get(urgency, 7)

    try:
        svc_request = ServiceRequest.objects.create(
            customer=customer,
            sector=customer.sector,
            title=title,
            description=description,
            urgency=urgency,
            wants_images=wants_images,
            customer_city=customer.city or '',
            max_budget=budget_value,
            expires_at=timezone.now() + timedelta(days=days),
        )
    except Exception as e:
        logger.error(f"[MARKETPLACE] Failed to create request for {customer.phone}: {e}")
        return JsonResponse({"error": "فشل إنشاء الطلب. حاول مرة أخرى."}, status=500)

    # 🔔 إشعار كل التجار المؤهلين
    _notify_merchants_of_new_request(svc_request)

    # Handle attachments
    if request.FILES.get('attachment_1'):
        svc_request.attachment_1 = request.FILES['attachment_1']
    if request.FILES.get('attachment_2'):
        svc_request.attachment_2 = request.FILES['attachment_2']
    if request.FILES.get('attachment_1') or request.FILES.get('attachment_2'):
        svc_request.save()

    # Update stats
    MarketplaceCustomer.objects.filter(pk=customer.pk).update(total_requests=F('total_requests') + 1)

    return JsonResponse({
        "status": "success",
        "message": "تم نشر طلبك بنجاح! سيبدأ التجار في تقديم عروضهم.",
        "request_code": str(svc_request.request_code),
    })


def marketplace_request_detail(request, request_code):
    """تفاصيل طلب + العروض المقدمة (للعميل)."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/')

    svc_request = get_object_or_404(ServiceRequest, request_code=request_code, customer=customer)
    offers = svc_request.offers.all().order_by('price')

    context = {
        'customer': customer,
        'svc_request': svc_request,
        'offers': offers,
    }
    return render(request, 'clients/marketplace/request_detail.html', context)


@csrf_exempt
def marketplace_accept_offer(request, offer_code):
    """قبول عرض من تاجر."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    offer = get_object_or_404(TenderOffer, offer_code=offer_code)

    if offer.service_request.customer != customer:
        return JsonResponse({"error": "غير مصرح"}, status=403)

    if offer.service_request.status != 'open' and offer.service_request.status != 'reviewing':
        return JsonResponse({"error": "هذا الطلب لم يعد مفتوحاً"}, status=400)

    with transaction.atomic():
        # Accept this offer
        offer.status = 'accepted'
        offer.save(update_fields=['status'])

        # Reject all other offers
        offer.service_request.offers.exclude(pk=offer.pk).update(status='rejected')

        # Update request
        offer.service_request.status = 'accepted'
        offer.service_request.accepted_offer = offer
        offer.service_request.save(update_fields=['status', 'accepted_offer'])

        # Calculate commission
        commission = (offer.price * offer.service_request.platform_commission_rate) / Decimal('100')
        ServiceRequest.objects.filter(pk=offer.service_request.pk).update(
            platform_commission_earned=commission
        )

        # Update customer stats
        MarketplaceCustomer.objects.filter(pk=customer.pk).update(
            total_accepted_offers=F('total_accepted_offers') + 1
        )

        # Bump merchant deal count
        Client.objects.filter(pk=offer.merchant.pk).update(
            successful_deals=F('successful_deals') + 1
        )

    return JsonResponse({
        "status": "success",
        "message": "تم قبول العرض بنجاح! سيتم التواصل معك من التاجر.",
        "merchant_name": offer.merchant.name,
        "merchant_phone": offer.merchant.phone,
        "merchant_address": offer.merchant_address,
    })


@csrf_exempt
def marketplace_rate_offer(request, offer_code):
    """تقييم العرض بعد الانتهاء."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "بيانات غير صالحة"}, status=400)

    offer = get_object_or_404(TenderOffer, offer_code=offer_code, status='accepted')
    if offer.service_request.customer != customer:
        return JsonResponse({"error": "غير مصرح"}, status=403)

    rating = int(data.get('rating', 0))
    review = data.get('review', '').strip()

    if rating < 1 or rating > 5:
        return JsonResponse({"error": "التقييم يجب أن يكون من 1 إلى 5"}, status=400)

    offer.customer_rating = rating
    offer.customer_review = review
    offer.save(update_fields=['customer_rating', 'customer_review'])

    # Mark request completed
    offer.service_request.status = 'completed'
    offer.service_request.completed_at = timezone.now()
    offer.service_request.save(update_fields=['status', 'completed_at'])

    # Update merchant rating
    merchant = offer.merchant
    avg = TenderOffer.objects.filter(
        merchant=merchant, customer_rating__isnull=False
    ).aggregate(avg=Avg('customer_rating'))['avg'] or Decimal('5.00')
    Client.objects.filter(pk=merchant.pk).update(market_rating=avg)

    return JsonResponse({"status": "success", "message": "شكراً لتقييمك!"})


# ------------------------------------------------------------------
# 🏪 Merchant-facing views (for tenants/merchants to see requests and submit offers)
# ------------------------------------------------------------------

def marketplace_merchant_feed(request):
    """
    عرض الطلبات المفتوحة للتجار — يدعم filtering مرن + diagnostics.
    Query params:
      ?show=all       → اعرض كل القطاعات (مش بس قطاعك)
      ?include_expired=1 → اعرض الطلبات المنتهية
      ?include_offered=1 → اعرض الطلبات اللي قدمت فيها عروض
    """
    if not request.user.is_authenticated:
        return redirect('/secure-portal/')

    try:
        tenant = Client.objects.get(schema_name=connection.schema_name)
    except Client.DoesNotExist:
        return redirect('/')

    industry = tenant.industry  # automotive or printing
    show_all_sectors = request.GET.get('show') == 'all'
    include_expired = request.GET.get('include_expired') == '1'
    include_offered = request.GET.get('include_offered') == '1'

    # 🔍 Auto-expire any requests past their expiry date (lazy cleanup)
    ServiceRequest.objects.filter(
        status='open', expires_at__lte=timezone.now()
    ).update(status='expired')

    # Base query — all open requests
    qs = ServiceRequest.objects.select_related('customer').order_by('-created_at')

    if not include_expired:
        qs = qs.filter(status='open', expires_at__gt=timezone.now())

    if not show_all_sectors:
        qs = qs.filter(sector=industry)

    # Exclude requests the merchant already offered on (unless explicitly asked)
    already_offered_ids = list(TenderOffer.objects.filter(merchant=tenant)
                               .values_list('service_request_id', flat=True))
    if not include_offered:
        qs = qs.exclude(id__in=already_offered_ids)

    # Diagnostic counters — show why the feed may look empty
    diagnostics = {
        'total_in_db': ServiceRequest.objects.count(),
        'open_in_db': ServiceRequest.objects.filter(status='open').count(),
        'open_in_my_sector': ServiceRequest.objects.filter(sector=industry, status='open').count(),
        'expired_in_my_sector': ServiceRequest.objects.filter(sector=industry, status='expired').count(),
        'i_already_offered_count': len(already_offered_ids),
        'my_industry': industry,
        'show_all_sectors': show_all_sectors,
        'include_expired': include_expired,
        'include_offered': include_offered,
    }

    context = {
        'requests': qs[:50],
        'tenant': tenant,
        'my_offers': TenderOffer.objects.filter(merchant=tenant)
                                       .select_related('service_request', 'service_request__customer')
                                       .order_by('-created_at')[:20],
        'total_open_count': qs.count(),
        'diagnostics': diagnostics,
    }
    return render(request, 'clients/marketplace/merchant_feed.html', context)


@csrf_exempt
def marketplace_submit_offer(request, request_code):
    """تقديم عرض سعر من تاجر على طلب عميل."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    try:
        tenant = Client.objects.get(schema_name=connection.schema_name)
    except Client.DoesNotExist:
        return JsonResponse({"error": "مستأجر غير صالح"}, status=400)

    svc_request = get_object_or_404(ServiceRequest, request_code=request_code, status='open')

    if svc_request.sector != tenant.industry:
        return JsonResponse({"error": "هذا الطلب ليس في قطاعك"}, status=403)

    if TenderOffer.objects.filter(service_request=svc_request, merchant=tenant).exists():
        return JsonResponse({"error": "لقد قدمت عرضاً بالفعل على هذا الطلب"}, status=400)

    price = request.POST.get('price', '').strip()
    description = request.POST.get('description', '').strip()
    estimated_days = request.POST.get('estimated_days', '1').strip()
    warranty_days = request.POST.get('warranty_days', '0').strip()
    merchant_city = request.POST.get('merchant_city', '').strip()
    merchant_address = request.POST.get('merchant_address', '').strip()

    if not price or not description or not merchant_city:
        return JsonResponse({"error": "السعر والتفاصيل والمدينة مطلوبين"}, status=400)

    try:
        price_val = Decimal(price)
        if price_val <= 0:
            raise ValueError
    except (ValueError, Exception):
        return JsonResponse({"error": "سعر غير صالح"}, status=400)

    # Check if images are required
    if svc_request.wants_images and not request.FILES.get('image_1'):
        return JsonResponse({"error": "العميل يطلب صور مع العرض. يرجى إرفاق صورة واحدة على الأقل."}, status=400)

    offer = TenderOffer.objects.create(
        service_request=svc_request,
        merchant=tenant,
        price=price_val,
        description=description,
        estimated_days=int(estimated_days),
        warranty_days=int(warranty_days),
        merchant_city=merchant_city,
        merchant_address=merchant_address,
    )

    # Handle images
    if request.FILES.get('image_1'):
        offer.image_1 = request.FILES['image_1']
    if request.FILES.get('image_2'):
        offer.image_2 = request.FILES['image_2']
    if request.FILES.get('image_3'):
        offer.image_3 = request.FILES['image_3']
    if request.FILES.get('file_attachment'):
        offer.file_attachment = request.FILES['file_attachment']
    if any(request.FILES.get(k) for k in ('image_1', 'image_2', 'image_3', 'file_attachment')):
        offer.save()

    # Update offers count
    ServiceRequest.objects.filter(pk=svc_request.pk).update(offers_count=F('offers_count') + 1)

    return JsonResponse({
        "status": "success",
        "message": "تم تقديم عرضك بنجاح! ستتلقى إشعار عند قبول العميل.",
    })


@csrf_exempt
def marketplace_logout(request):
    """
    تسجيل خروج عميل السوق.
    🛡️ يبطل الـ session token في الداتابيز + يحذف الكوكي.
    """
    # Invalidate session token in database (prevents reuse of stolen cookies)
    customer = _marketplace_auth(request)
    if customer:
        customer.session_token = uuid.uuid4()  # Rotate token so old one is invalid
        customer.save(update_fields=['session_token'])

    response = redirect('/marketplace/')
    response.delete_cookie('mp_session')
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return response


# ------------------------------------------------------------------
# 🔔 Notification endpoints for merchant dashboard
# ------------------------------------------------------------------

def marketplace_merchant_feed_count(request):
    """
    عداد الطلبات المفتوحة في قطاع التاجر التي لم يقدم عرض عليها بعد.
    يستخدم في الـ polling badge على dashboard الشركة.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"new_count": 0, "authenticated": False})

    try:
        tenant = Client.objects.get(schema_name=connection.schema_name)
    except Client.DoesNotExist:
        return JsonResponse({"new_count": 0})

    industry = tenant.industry
    open_requests = ServiceRequest.objects.filter(
        sector=industry,
        status='open',
        expires_at__gt=timezone.now(),
    )
    already_offered = TenderOffer.objects.filter(merchant=tenant).values_list('service_request_id', flat=True)
    new_count = open_requests.exclude(id__in=already_offered).count()

    return JsonResponse({
        "new_count": new_count,
        "industry": industry,
        "authenticated": True,
    })


@csrf_exempt
def marketplace_merchant_create_request(request):
    """
    🆕 السماح للتجار بإنشاء طلبات B2B (مثلاً: مطبعة تطلب 10 آلاف تيشرت من مصنع).
    التاجر يبقى مجهول للتجار التانية، زي ما العميل النهائي مجهول.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    try:
        tenant = Client.objects.get(schema_name=connection.schema_name)
    except Client.DoesNotExist:
        return JsonResponse({"error": "مستأجر غير صالح"}, status=400)

    title = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    urgency = request.POST.get('urgency', 'normal')
    target_sector = request.POST.get('target_sector', tenant.industry)
    max_budget = request.POST.get('max_budget', '').strip()
    wants_images = request.POST.get('wants_images') == 'true'

    if not title or len(title) < 5:
        return JsonResponse({"error": "العنوان قصير جداً"}, status=400)
    if not description or len(description) < 10:
        return JsonResponse({"error": "التفاصيل قصيرة جداً"}, status=400)
    if target_sector not in ('automotive', 'printing'):
        return JsonResponse({"error": "قطاع غير صالح"}, status=400)
    if urgency not in ('normal', 'soon', 'urgent'):
        urgency = 'normal'

    budget_value = None
    if max_budget:
        try:
            budget_value = Decimal(max_budget)
        except Exception:
            return JsonResponse({"error": "الميزانية رقم غير صالح"}, status=400)

    # 🔗 ربط الطلب بـ MarketplaceCustomer التابع للشركة (يُنشأ تلقائياً لو ما اتعملش قبل كده)
    # الفكرة: التاجر يحجز نفسه كـ "buyer" في النظام علشان يقدم طلبات
    merchant_buyer, _ = MarketplaceCustomer.objects.get_or_create(
        phone=f'+B2B{tenant.id}',  # phone صناعي للتمييز
        defaults={
            'customer_type': 'company',
            'full_name': tenant.owner_name or tenant.name,
            'company_name': tenant.name,
            'sector': target_sector,
            'city': '',
            'is_verified': True,
            'job_title': 'B2B Buyer',
        },
    )

    expiry_map = {'urgent': 1, 'soon': 3, 'normal': 7}
    days = expiry_map.get(urgency, 7)

    try:
        svc_request = ServiceRequest.objects.create(
            customer=merchant_buyer,
            sector=target_sector,
            title=f"[B2B] {title}",
            description=description,
            urgency=urgency,
            wants_images=wants_images,
            customer_city='',
            max_budget=budget_value,
            expires_at=timezone.now() + timedelta(days=days),
        )
    except Exception as e:
        logger.error(f"[B2B REQUEST] Failed for {tenant.name}: {e}")
        return JsonResponse({"error": "فشل إنشاء الطلب"}, status=500)

    # 🔔 Send notifications to all eligible merchants in the sector
    _notify_merchants_of_new_request(svc_request, exclude_tenant=tenant)

    return JsonResponse({
        "status": "success",
        "message": "تم نشر طلبك! سيشاهده كل التجار المؤهلين.",
        "request_code": str(svc_request.request_code),
    })


def _notify_merchants_of_new_request(svc_request, exclude_tenant=None):
    """
    🔔 إشعار كل التجار في نفس قطاع الطلب.
    حالياً يسجل PlatformEvent (يظهر في activity feed).
    TODO: WebSocket/Push notifications للـ real-time.
    """
    try:
        eligible_merchants = Client.objects.filter(
            industry=svc_request.sector,
            is_active=True,
            status__in=('active', 'trial'),
        ).exclude(schema_name='public')
        if exclude_tenant:
            eligible_merchants = eligible_merchants.exclude(pk=exclude_tenant.pk)

        count = eligible_merchants.count()
        PlatformEvent.objects.create(
            event_type='other',
            tenant_schema='public',
            tenant_name='Marketplace',
            description=f"🛒 طلب جديد: {svc_request.title[:80]} — تم إشعار {count} تاجر",
            metadata={
                'request_code': str(svc_request.request_code),
                'sector': svc_request.sector,
                'urgency': svc_request.urgency,
                'merchants_notified': count,
            },
        )
        logger.info(f"[MARKETPLACE NOTIFY] Notified {count} merchants of request {svc_request.request_code}")
    except Exception as e:
        logger.error(f"[MARKETPLACE NOTIFY] Failed: {e}")

# =====================================================================
# 🎨 AI Designs Store — متجر التصاميم الفورية
# =====================================================================

def design_store_home(request):
    """🛍️ صفحة المتجر — يعرض الباقات (عملاء + مصممين)."""
    customer_packages = DesignPackage.objects.filter(
        is_active=True, target_audience='customer',
    ).order_by('sort_order', 'designs_count')
    designer_packages = DesignPackage.objects.filter(
        is_active=True, target_audience='designer',
    ).order_by('sort_order', 'designs_count')

    # Fallback: if no new packages yet, show all active
    if not customer_packages.exists() and not designer_packages.exists():
        customer_packages = DesignPackage.objects.filter(is_active=True).order_by('sort_order')

    customer = _marketplace_auth(request)
    user_balance = 0
    free_remaining = 0
    if customer:
        user_balance = sum(p.designs_remaining for p in
                          customer.design_purchases.filter(status='paid')
                          if p.is_usable)
        free_remaining = customer.free_designs_remaining

    return render(request, 'clients/marketplace/design_store.html', {
        'packages': customer_packages,  # backwards compat
        'customer_packages': customer_packages,
        'designer_packages': designer_packages,
        'customer': customer,
        'user_balance': user_balance,
        'free_remaining': free_remaining,
        'total_balance': user_balance + free_remaining,
    })


@csrf_exempt
def design_store_buy(request, package_slug):
    """شراء باقة."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول أولاً", "redirect": "/marketplace/"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    package = get_object_or_404(DesignPackage, slug=package_slug, is_active=True)
    payment_method = request.POST.get('payment_method', 'paymob')

    # Determine designs count — designers (sector=printing with job_title containing design keywords) get more
    is_designer = (customer.sector == 'printing' and
                   any(kw in (customer.job_title or '').lower()
                       for kw in ('مصمم', 'design', 'جرافيك', 'graphic', 'فنان')))
    designs_count = (package.designer_designs_count or package.designs_count) if is_designer else package.designs_count

    # Create the purchase as PENDING — will be marked paid after payment confirmation
    purchase = DesignPurchase.objects.create(
        customer=customer, package=package,
        designs_total=designs_count,
        price_paid=package.price_egp,
        payment_method=payment_method,
        status='pending',
    )
    logger.info(f"[DESIGN STORE] Purchase #{purchase.pk} created — PENDING payment ({payment_method})")

    # Build response based on payment method
    if payment_method == 'vodafone_cash':
        return JsonResponse({
            "status": "pending_payment",
            "purchase_id": purchase.pk,
            "purchase_code": str(purchase.purchase_code),
            "redirect": f"/marketplace/design-store/payment/{purchase.purchase_code}/",
            "message": "جاري توجيهك لصفحة الدفع...",
        })
    elif payment_method == 'instapay':
        return JsonResponse({
            "status": "pending_payment",
            "purchase_id": purchase.pk,
            "purchase_code": str(purchase.purchase_code),
            "redirect": f"/marketplace/design-store/payment/{purchase.purchase_code}/",
            "message": "جاري توجيهك لصفحة الدفع...",
        })
    else:
        # paymob / card — redirect to Paymob iframe
        paymob_api_key = getattr(settings, 'PAYMOB_API_KEY', '')
        paymob_integration_id = getattr(settings, 'PAYMOB_INTEGRATION_ID', '')
        paymob_iframe_id = getattr(settings, 'PAYMOB_IFRAME_ID', '')

        if not paymob_api_key:
            logger.error("[PAYMOB/DESIGN] API key not configured")
            return JsonResponse({"error": "الدفع بالبطاقة غير متاح حالياً"}, status=503)

        try:
            import requests as http_requests
            # Step 1: Auth
            auth_res = http_requests.post('https://accept.paymob.com/api/auth/tokens',
                json={'api_key': paymob_api_key}, timeout=15)
            auth_token = auth_res.json().get('token')

            # Step 2: Order
            amount_cents = int(float(package.price_egp) * 100)
            merchant_order_id = f'design_{purchase.pk}_{uuid.uuid4().hex[:8]}'
            order_res = http_requests.post('https://accept.paymob.com/api/ecommerce/orders', json={
                'auth_token': auth_token,
                'delivery_needed': 'false',
                'amount_cents': amount_cents,
                'currency': 'EGP',
                'items': [{'name': f'باقة {package.name}', 'amount_cents': amount_cents, 'quantity': '1'}],
                'merchant_order_id': merchant_order_id,
            }, timeout=15)
            order_id = order_res.json().get('id')

            # Step 3: Payment key
            billing = {
                'first_name': customer.full_name or 'Customer',
                'last_name': 'Design Store',
                'email': customer.email or 'customer@mousstec.com',
                'phone_number': customer.phone.lstrip('+') if customer.phone else '01000000000',
                'apartment': 'NA', 'floor': 'NA', 'street': 'NA', 'building': 'NA',
                'shipping_method': 'NA', 'postal_code': 'NA', 'city': 'Cairo',
                'country': 'EG', 'state': 'Cairo',
            }
            key_res = http_requests.post('https://accept.paymob.com/api/acceptance/payment_keys', json={
                'auth_token': auth_token,
                'amount_cents': amount_cents,
                'expiration': 3600,
                'order_id': order_id,
                'billing_data': billing,
                'currency': 'EGP',
                'integration_id': int(paymob_integration_id),
                'lock_order_when_paid': 'true',
            }, timeout=15)
            payment_token = key_res.json().get('token')

            # Store purchase info in cache for callback
            cache.set(f'paymob_design_{order_id}', {
                'purchase_id': purchase.pk,
                'customer_id': customer.pk,
            }, timeout=7200)

            iframe_url = f'https://accept.paymob.com/api/acceptance/iframes/{paymob_iframe_id}?payment_token={payment_token}'
            return JsonResponse({
                "status": "redirect_paymob",
                "redirect": iframe_url,
                "message": "جاري توجيهك لبوابة الدفع...",
            })
        except Exception as e:
            logger.error(f"[PAYMOB/DESIGN] Checkout error: {e}")
            return JsonResponse({"error": "حدث خطأ في الاتصال ببوابة الدفع. حاول مرة أخرى."}, status=500)


@csrf_exempt
def design_store_payment(request, purchase_code):
    """💳 صفحة الدفع — تعليمات التحويل + رفع إيصال."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/')

    purchase = get_object_or_404(DesignPurchase, purchase_code=purchase_code, customer=customer)

    if request.method == 'POST':
        # Customer submitting payment proof
        txn_ref = request.POST.get('txn_ref', '').strip()
        sender_phone = request.POST.get('sender_phone', '').strip()
        if not txn_ref:
            return JsonResponse({"error": "لازم تكتب رقم العملية"}, status=400)
        purchase.payment_reference = txn_ref
        purchase.sender_phone = sender_phone
        purchase.status = 'awaiting_confirm'
        purchase.save(update_fields=['payment_reference', 'sender_phone', 'status'])
        logger.info(f"[DESIGN STORE] Purchase #{purchase.pk} payment proof submitted: ref={txn_ref}")
        return JsonResponse({
            "status": "success",
            "message": "تم استلام بيانات الدفع — هيتم التفعيل خلال دقائق بعد التأكيد ✅",
        })

    return render(request, 'clients/marketplace/design_store_payment.html', {
        'customer': customer,
        'purchase': purchase,
    })


def design_store_confirm_payment(request, purchase_id):
    """✅ تأكيد الدفع بواسطة الأدمن."""
    # Only super admin (public schema superuser) can confirm
    if not request.user.is_authenticated or not request.user.is_superuser:
        return JsonResponse({"error": "غير مصرح"}, status=403)

    purchase = get_object_or_404(DesignPurchase, pk=purchase_id)

    if request.method == 'POST':
        action = request.POST.get('action', 'confirm')
        if action == 'confirm':
            purchase.status = 'paid'
            purchase.paid_at = timezone.now()
            purchase.save(update_fields=['status', 'paid_at'])
            logger.info(f"[DESIGN STORE] Purchase #{purchase.pk} CONFIRMED by admin {request.user}")
            return JsonResponse({"status": "success", "message": f"تم تأكيد الدفع — الباقة مفعلة للعميل"})
        elif action == 'reject':
            purchase.status = 'rejected'
            purchase.save(update_fields=['status'])
            logger.info(f"[DESIGN STORE] Purchase #{purchase.pk} REJECTED by admin {request.user}")
            return JsonResponse({"status": "success", "message": "تم رفض الطلب"})

    return JsonResponse({"error": "POST only"}, status=405)


def design_store_my_designs(request):
    """📚 صفحة تصاميمي + الرصيد المتبقي."""
    customer = _marketplace_auth(request)
    if not customer:
        return redirect('/marketplace/')

    purchases = customer.design_purchases.filter(status='paid').select_related('package').order_by('-created_at')
    designs = customer.designs.order_by('-created_at')[:50]
    active_purchase = next((p for p in purchases if p.is_usable), None)
    paid_remaining = sum(p.designs_remaining for p in purchases if p.is_usable)
    free_remaining = customer.free_designs_remaining

    return render(request, 'clients/marketplace/design_store_my.html', {
        'customer': customer,
        'purchases': purchases,
        'designs': designs,
        'active_purchase': active_purchase,
        'total_remaining': paid_remaining + free_remaining,
        'free_remaining': free_remaining,
        'paid_remaining': paid_remaining,
    })


@csrf_exempt
def design_store_generate(request):
    """🎨 توليد تصميم من الباقة."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    # 🛡️ Rate limiting — 5 generations per minute per customer (protects OpenAI API costs)
    gen_rate_key = f'design_gen_rate:{customer.pk}'
    gen_count = cache.get(gen_rate_key, 0)
    if gen_count >= 5:
        return JsonResponse({"error": "أنت ترسل طلبات كثيرة. انتظر دقيقة ثم حاول مرة أخرى."}, status=429)
    cache.set(gen_rate_key, gen_count + 1, 60)

    # Check free trial designs first, then paid packages
    using_free_trial = False
    purchase = None

    if customer.has_free_designs:
        using_free_trial = True
    else:
        # Find an active purchase with remaining designs
        purchase = next((p for p in customer.design_purchases.filter(status='paid').order_by('created_at')
                        if p.is_usable), None)
        if not purchase:
            return JsonResponse({
                "error": "لا يوجد رصيد متاح. اشتري باقة جديدة لتبدأ.",
                "redirect": "/marketplace/design-store/",
                "free_used": customer.free_designs_used,
                "free_total": customer.free_designs_total,
            }, status=403)

    title = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    category = request.POST.get('category', 'other')
    size_preset = request.POST.get('size_preset', '1024x1024')
    custom_w = request.POST.get('custom_width_px', '').strip()
    custom_h = request.POST.get('custom_height_px', '').strip()
    weight = request.POST.get('weight_kg', '').strip()
    output_format = request.POST.get('output_format', 'png')
    # Print dimensions from the user (for accurate design)
    print_width_cm = request.POST.get('print_width_cm', '').strip()
    print_height_cm = request.POST.get('print_height_cm', '').strip()
    use_standard_size = request.POST.get('use_standard_size', '')

    if not description or len(description) < 10:
        return JsonResponse({"error": "وصف التصميم قصير جداً (10 أحرف على الأقل)"}, status=400)

    # Smart size selection based on category + user dimensions
    # Category → best default AI size mapping
    CATEGORY_SIZE_MAP = {
        'logo': '1024x1024',
        'business_card': '1536x1024',
        'social_post': '1024x1024',
        'flyer': '1024x1536',
        'poster': '1024x1536',
        'banner': '1536x1024',
        'tshirt': '1024x1536',
        'mug': '1536x1024',
        'sticker': '1024x1024',
        'packaging': '1024x1024',
        'menu': '1024x1536',
        'invitation': '1024x1536',
        'mockup': '1024x1024',
    }

    # Map user presets → canonical size
    size_map = {
        '1024x1024': '1024x1024', '1024x1536': '1024x1536', '1536x1024': '1536x1024',
        '1024x1792': '1024x1792', '1792x1024': '1792x1024',
        '2048x2048': '1024x1024',
        'a4': '1024x1536', 'a3': '1024x1536',
        'business_card': '1536x1024',
        'tshirt_chest': '1024x1536', 'mug': '1536x1024',
        'custom': '1024x1024', 'auto': 'auto',
    }
    # If user chose 'auto' or no size, pick best size by category
    if size_preset in ('auto', '') or size_preset not in size_map:
        canonical_size = CATEGORY_SIZE_MAP.get(category, '1024x1024')
    else:
        canonical_size = size_map.get(size_preset, '1024x1024')

    # If user specified custom print dimensions, determine orientation
    if print_width_cm and print_height_cm:
        try:
            pw, ph = float(print_width_cm), float(print_height_cm)
            if pw > ph:
                canonical_size = '1536x1024'  # landscape
            elif ph > pw:
                canonical_size = '1024x1536'  # portrait
            else:
                canonical_size = '1024x1024'  # square
        except (ValueError, TypeError):
            pass

    # gpt-image-1 only supports: 1024x1024, 1024x1536, 1536x1024, auto
    GPT_IMAGE_SIZE_MAP = {
        '1024x1024': '1024x1024', '1024x1536': '1024x1536', '1536x1024': '1536x1024',
        '1024x1792': '1024x1536', '1792x1024': '1536x1024', 'auto': 'auto',
    }

    # Build dimension info string for the prompt
    dim_info = ''
    if print_width_cm and print_height_cm:
        dim_info = f" Actual print size: {print_width_cm}cm x {print_height_cm}cm."
    elif use_standard_size:
        std_sizes = {
            'tshirt_s': '28x38cm', 'tshirt_m': '30x40cm', 'tshirt_l': '32x42cm',
            'business_card': '9x5.5cm', 'a4': '21x29.7cm', 'a3': '29.7x42cm',
            'a5': '14.8x21cm', 'mug_standard': '23x9cm', 'banner_60': '60x160cm',
            'banner_80': '80x180cm', 'instagram_post': '1080x1080px',
            'instagram_story': '1080x1920px', 'facebook_cover': '820x312px',
        }
        if use_standard_size in std_sizes:
            dim_info = f" Standard size: {std_sizes[use_standard_size]}."

    # Learn from best past prompts (few-shot learning)
    learned_suffix = ''
    try:
        from clients.models import DesignPromptLog
        best_examples = DesignPromptLog.get_best_examples(category, limit=2)
        if best_examples:
            learned_suffix = ' Style reference from top-rated designs in this category.'
    except Exception:
        pass

    # ═══════════════════════════════════════════════════════════════════
    # 🎨 MASTER PROMPT ENGINE v3 — Production-grade AI Design Prompts
    # ─────────────────────────────────────────────────────────────────
    # Architecture:
    #   1. QUALITY_FOUNDATION — universal quality/anti-artifact layer
    #   2. CATEGORY_PROMPT    — category-specific master prompt
    #   3. MULTI_ANGLE        — optional multi-view mockup modifier
    #   4. ARABIC_AWARENESS   — RTL text rendering instructions
    #   5. LEARNED_INSIGHTS   — few-shot learning from best past prompts
    # ═══════════════════════════════════════════════════════════════════

    multi_angle = request.POST.get('multi_angle', '') == '1'

    # ── Layer 1: Quality Foundation ──────────────────────────────────
    QUALITY_FOUNDATION = (
        "You are a world-class graphic designer with 20 years of experience in branding, "
        "print design, and visual communication. You create designs that win international "
        "awards. Every design you produce is: (1) perfectly composed with golden-ratio "
        "proportions and visual balance, (2) uses professional typography with proper "
        "kerning, leading, and hierarchy, (3) has a cohesive, intentional color palette "
        "limited to 3-5 harmonious colors, (4) is print-ready at 300 DPI quality with "
        "CMYK-safe colors, (5) has clean edges, no artifacts, no blurriness, no distortion. "
        "CRITICAL RULES: Never produce amateur-looking designs. Never use more than 2 font "
        "families. Never create cluttered layouts — use generous whitespace. Never distort "
        "text or make it unreadable. All text must be crisp and perfectly aligned. "
    )

    # ── Layer 4: Arabic Awareness ────────────────────────────────────
    # Detect if user description contains Arabic
    import re
    has_arabic = bool(re.search(r'[؀-ۿݐ-ݿࢠ-ࣿ]', description))
    ARABIC_LAYER = ''
    if has_arabic:
        ARABIC_LAYER = (
            "ARABIC TEXT RULES: This design contains Arabic text. Arabic reads RIGHT-TO-LEFT. "
            "Use elegant Arabic typography (Naskh or modern Kufi style). Ensure Arabic letters "
            "are properly connected and shaped. Place Arabic text aligned to the RIGHT side. "
            "Use professional Arabic fonts — no broken or disconnected letters. "
        )

    # ── Layer 2: Category-Specific Master Prompts ────────────────────
    enhanced_desc = description
    weight_info = f" Product weight: {weight}kg." if weight else ''

    CATEGORY_PROMPTS = {
        'logo': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Create a world-class LOGO DESIGN. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Design a timeless, iconic logo that communicates the brand's essence "
            f"at a glance. Create a unique symbol/icon paired with custom lettering. The logo "
            f"must be: scalable (works at 16px favicon AND billboard size), memorable (recognizable "
            f"in 2 seconds), versatile (works on white, dark, and colored backgrounds). "
            f"COMPOSITION: Center the logo on a pure white background. Use negative space cleverly. "
            f"Maximum 2-3 brand colors. The icon and text should be perfectly balanced. "
            f"STYLE: Modern, clean, vector-style flat design. No photographic elements, no complex "
            f"gradients, no drop shadows, no 3D effects unless specifically requested. Think Apple, "
            f"Nike, Airbnb level quality. "
            f"OUTPUT: Render the final logo large and centered with ample padding around it.{dim_info}"
        ),

        'business_card': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PREMIUM BUSINESS CARD — render as a flat, top-down photograph of the "
            f"printed card lying on a dark marble or wooden surface. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create an elegant, minimal business card with perfect typography. "
            f"LAYOUT: Include these fields in clear visual hierarchy: Company name/logo (top), "
            f"Person name (prominent), Job title, Phone number, Email, Address (smaller). "
            f"TYPOGRAPHY: Use maximum 2 fonts — one bold display font for the name, one clean "
            f"sans-serif for details. Letter-spacing: slightly expanded for elegance. "
            f"DESIGN: Use one accent color against white/cream card stock. Add a subtle design "
            f"element (thin line, geometric pattern, or embossed texture). Consider a colored edge "
            f"or a minimal pattern on the back. "
            f"QUALITY: The card should look like a $500 Moo.com premium design — thick paper stock "
            f"feel, possibly with foil stamping or letterpress texture.{dim_info or ' Size: 85x55mm (standard).'}"
        ),

        'flyer': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a HIGH-IMPACT FLYER — flat print layout, ready for professional printing. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a flyer that grabs attention in under 1 second. "
            f"LAYOUT STRUCTURE (top to bottom): "
            f"(1) HERO ZONE (top 40%): Dominant visual element + bold headline in large, impactful font. "
            f"(2) BODY ZONE (middle 35%): Key information in organized blocks with icons or bullet points. "
            f"Use subheadings to break content. Keep body text readable (14pt+ equivalent). "
            f"(3) ACTION ZONE (bottom 25%): Strong call-to-action button/banner, contact details "
            f"(phone, address, social media), and company logo. "
            f"COLOR: Use a bold primary color for headlines and CTA, with a complementary secondary color. "
            f"Background should be clean (white or very light tint). "
            f"TYPOGRAPHY: Bold sans-serif for headlines (Impact, Montserrat style), clean body font. "
            f"VISUAL HIERARCHY: Someone should understand the message from 3 feet away just by reading "
            f"the headline and seeing the main visual.{dim_info or ' A4 (210x297mm).'}"
        ),

        'poster': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a DRAMATIC, ATTENTION-GRABBING POSTER — flat print layout. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a poster that commands attention on a wall from across the room. "
            f"COMPOSITION: Use the rule of thirds. Create one dominant focal point that takes up "
            f"at least 50% of the poster. Use dramatic contrast (light vs dark, big vs small). "
            f"TYPOGRAPHY: The headline should be MASSIVE — readable from 10+ feet away. Use extreme "
            f"font weight (ultra-bold/black). Limit to 5-7 words maximum for headline. "
            f"Supporting text should be much smaller, creating dramatic size contrast. "
            f"COLOR: Use high-contrast color scheme — dark background with bright accent, or vice versa. "
            f"IMAGERY: If the content requires imagery, use a single powerful, high-quality visual — "
            f"not multiple small images. "
            f"OVERALL: Think movie poster or museum exhibition quality — bold, artistic, unforgettable.{dim_info}"
        ),

        'social_post': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a VIRAL-WORTHY SOCIAL MEDIA POST that stops the scroll. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a post optimized for maximum engagement and shares. "
            f"COMPOSITION: Bold, simple, high-contrast. The message should be understood in under "
            f"2 seconds of viewing. Use the full frame — no wasted space. "
            f"TYPOGRAPHY: Large, bold text that is readable on a phone screen. Maximum 2 lines of "
            f"text for headline. Use modern trendy fonts (geometric sans-serif). "
            f"COLOR: Vibrant, saturated colors that pop on mobile screens. Use color blocking, "
            f"gradients, or duotone effects for modern appeal. "
            f"STYLE: Follow 2024-2026 design trends — glassmorphism, bold gradients, "
            f"oversized typography, minimalist compositions, neon accents. "
            f"BRANDING: Include a subtle brand logo/watermark in one corner (small, not distracting).{dim_info or ' Square 1080x1080px.'}"
        ),

        'menu': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design an ELEGANT RESTAURANT/CAFE MENU — flat print layout. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a menu that elevates the dining experience. "
            f"LAYOUT: Organized by food categories with clear section headers (appetizers, mains, "
            f"desserts, drinks). Each item has: name (bold), description (italic/light), price (right-aligned). "
            f"Use thin divider lines or subtle spacing between sections. "
            f"TYPOGRAPHY: Pair an elegant serif font (for headers/restaurant name) with a clean "
            f"sans-serif (for items/prices). Ensure prices are perfectly aligned in a right column. "
            f"DESIGN: Add subtle decorative elements — thin borders, small culinary icons, "
            f"ornamental dividers. The background should be cream/off-white or rich dark (leather feel). "
            f"QUALITY: Should look like it belongs in a Michelin-starred restaurant — refined, "
            f"luxurious, but easy to read.{dim_info or ' A4 (210x297mm).'}"
        ),

        'invitation': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a STUNNING INVITATION CARD — flat print layout. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create an invitation that makes recipients excited to attend. "
            f"LAYOUT: Center the event name prominently. Below it: date, time, venue, dress code, "
            f"RSVP details — in clear, elegant hierarchy. "
            f"DESIGN: Use luxurious elements — gold/silver foil effects, embossed textures, "
            f"decorative frames or borders, floral or geometric ornaments. "
            f"TYPOGRAPHY: Elegant script font for event name, clean serif or sans-serif for details. "
            f"Perfect letter spacing and line height. "
            f"COLOR PALETTE: Rich, celebratory colors — deep navy + gold, burgundy + cream, "
            f"blush pink + rose gold — depending on the event type. "
            f"PAPER: Simulate premium card stock — thick, textured, high-end stationery feel.{dim_info}"
        ),

        'banner': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL ROLL-UP BANNER — flat, vertical print layout. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a banner optimized for standing display at events/stores. "
            f"LAYOUT (vertical flow, top to bottom): "
            f"(1) TOP: Company logo + brand colors header bar. "
            f"(2) HERO: Main message in VERY LARGE bold text (readable from 3+ meters). "
            f"(3) MIDDLE: 3-4 key features/benefits with icons, clean grid layout. "
            f"(4) BOTTOM: Contact info strip — phone, email, website, QR code area. "
            f"TYPOGRAPHY: Extremely bold headlines (ultra-thick weight). Body text must be "
            f"minimum 24pt equivalent for readability at distance. "
            f"COLOR: Strong brand colors with high contrast. Full-bleed background color.{dim_info}"
        ),

        'sticker': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL STICKER/LABEL — die-cut ready. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a sticker that looks amazing at small size (3-10cm). "
            f"DESIGN: Bold, simple shapes with thick outlines. Maximum 3 colors. No fine "
            f"details that disappear when printed small. Vector-style flat illustration. "
            f"SHAPE: Design with a clear die-cut boundary — circle, rounded rectangle, or custom "
            f"contour shape. Show the sticker on white background with a subtle cut-line. "
            f"QUALITY: Should look like a premium vinyl sticker — vibrant, weatherproof, professional.{dim_info}"
        ),
    }

    # ── T-shirt (with multi-angle support) ───────────────────────────
    if category == 'tshirt':
        base_tshirt = (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"CLIENT BRIEF: {description}. "
        )
        if multi_angle:
            enhanced_desc = (
                f"{base_tshirt}"
                f"TASK: Create a PROFESSIONAL T-SHIRT PRODUCT PHOTOGRAPHY showing the shirt from "
                f"THREE ANGLES arranged side by side in ONE image: (1) FRONT VIEW — full front of shirt, "
                f"(2) BACK VIEW — full back of shirt, (3) 3/4 ANGLE VIEW — perspective view showing depth. "
                f"EXECUTION: Photo-realistic product mockup of a premium cotton t-shirt. The graphic design "
                f"should be professionally printed on the shirt (DTG/screen-print quality). "
                f"Show realistic fabric texture, natural folds and wrinkles, proper perspective distortion "
                f"of the artwork following the shirt's contours. "
                f"BACKGROUND: Clean, consistent studio background (light gray gradient). "
                f"LIGHTING: Professional studio lighting — soft key light, fill light, subtle rim light. "
                f"All three views should have identical lighting for consistency. "
                f"QUALITY: E-commerce product photography level — Shopify/Amazon listing quality.{dim_info or ' 30x40cm chest print.'}"
            )
        else:
            enhanced_desc = (
                f"{base_tshirt}"
                f"TASK: Create a STUNNING T-SHIRT PRODUCT MOCKUP — a single, hero product shot. "
                f"EXECUTION: Photo-realistic mockup showing the t-shirt worn by an invisible mannequin "
                f"(ghost mannequin style) or laid flat on a clean surface. The graphic artwork should be "
                f"screen-printed/DTG quality — vibrant, sharp, following the fabric contours naturally. "
                f"Show realistic cotton fabric texture, natural shadow beneath, studio lighting. "
                f"BACKGROUND: Clean studio white or light gray. "
                f"QUALITY: Premium e-commerce product photography — Shopify hero image quality.{dim_info or ' 30x40cm chest print.'}"
            )

    # ── Mug (with multi-angle support) ───────────────────────────────
    elif category == 'mug':
        base_mug = (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"CLIENT BRIEF: {description}. "
        )
        if multi_angle:
            enhanced_desc = (
                f"{base_mug}"
                f"TASK: Create a PHOTO-REALISTIC MUG MOCKUP showing THREE ANGLES in ONE image: "
                f"(1) FRONT — design facing camera, (2) BACK — opposite side, (3) HANDLE SIDE — 3/4 angle. "
                f"EXECUTION: Premium white ceramic 11oz coffee mug. The artwork wraps naturally around the "
                f"curved surface with proper perspective distortion. Show realistic ceramic texture, "
                f"glossy reflections, and a subtle shadow beneath each mug. "
                f"BACKGROUND: Clean white studio background, consistent lighting across all three views.{dim_info or ' Wrap area: 23x9cm.'}"
            )
        else:
            enhanced_desc = (
                f"{base_mug}"
                f"TASK: Create a PREMIUM MUG PRODUCT MOCKUP — single hero shot from a 3/4 angle. "
                f"EXECUTION: Photo-realistic white ceramic 11oz mug with the design wrapped around it. "
                f"Show the handle, realistic ceramic gloss, subtle reflections, and a soft shadow. "
                f"The artwork conforms to the mug's curvature naturally. "
                f"BACKGROUND: Lifestyle setting (wooden desk, coffee beans) OR clean studio white. "
                f"QUALITY: Premium product photography — Amazon/Etsy listing quality.{dim_info or ' Wrap area: 23x9cm.'}"
            )

    # ── Packaging (with multi-angle support) ─────────────────────────
    elif category == 'packaging':
        base_pkg = (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"CLIENT BRIEF: {description}. "
        )
        if multi_angle:
            enhanced_desc = (
                f"{base_pkg}"
                f"TASK: Create a PRODUCT PACKAGING MOCKUP showing THREE ANGLES in ONE image: "
                f"(1) FRONT — main branding panel, (2) BACK — info panel with ingredients/details, "
                f"(3) SIDE — secondary branding. Arrange side by side on a clean white background. "
                f"EXECUTION: Realistic 3D box/bag/bottle mockup with professional label design. "
                f"Include typography, barcode area, brand elements. Studio product photography quality.{dim_info}{weight_info}"
            )
        else:
            enhanced_desc = (
                f"{base_pkg}"
                f"TASK: Design PREMIUM PRODUCT PACKAGING — 3D realistic mockup from an attractive angle. "
                f"EXECUTION: Create packaging that has shelf-appeal — a customer would pick this product "
                f"over competitors. Design a complete package with: front branding panel (logo, product name, "
                f"key visual), info panel (ingredients/specs area), barcode area, regulatory symbols. "
                f"STYLE: Modern, clean packaging with professional typography and intentional color use. "
                f"RENDERING: Photo-realistic 3D mockup with studio lighting, subtle reflections, "
                f"and realistic material textures (matte, glossy, kraft paper, etc.).{dim_info}{weight_info}"
            )

    # ── All other categories from lookup ─────────────────────────────
    elif category in CATEGORY_PROMPTS:
        enhanced_desc = CATEGORY_PROMPTS[category]

    # ── Fallback for unknown categories ──────────────────────────────
    else:
        # Detect if this is a form/document request
        form_keywords = ('استقبال', 'فورم', 'نموذج', 'form', 'استمارة', 'ورقة', 'receipt',
                         'فاتورة', 'invoice', 'كشف', 'تقرير', 'سجل', 'بيان', 'شيت',
                         'checklist', 'صيانة', 'maintenance', 'inspection', 'فحص')
        is_form_request = any(kw in description.lower() for kw in form_keywords)

        if is_form_request:
            enhanced_desc = (
                f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
                f"TASK: Create a PROFESSIONAL BUSINESS FORM / DOCUMENT designed for PRINTING at a real business. "
                f"CLIENT BRIEF: {description}. "
                f"CRITICAL DESIGN RULES FOR THIS FORM: "
                f"1. This is a PRINTABLE PAPER FORM — it must look like a real form used in professional businesses. "
                f"2. Use a CLEAN WHITE BACKGROUND with a professional header area at the top for company logo and name. "
                f"3. Create CLEARLY LABELED FIELDS with horizontal lines for handwriting (like ________). "
                f"4. Use proper GRID LAYOUT with organized sections, each with a clear title/header. "
                f"5. All Arabic text must be RIGHT-ALIGNED, perfectly readable, with proper connected Arabic letters. "
                f"6. Use professional CORPORATE COLORS — navy blue (#1e3a5f) for headers, black for labels, "
                f"light gray (#f5f5f5) for alternating row backgrounds. "
                f"7. Include a footer area with date, signature line, and company info. "
                f"8. The form should look like it was designed by a professional print house — NOT a generic template. "
                f"9. If a logo is provided, place it prominently in the top-right corner (for RTL layout). "
                f"10. Use thin borders and lines, NOT thick boxes. Keep it elegant and clean. "
                f"STYLE REFERENCE: Think of premium auto dealership or professional service center paperwork — "
                f"clean, organized, branded, and easy to fill out by hand.{dim_info}"
            )
        else:
            enhanced_desc = (
                f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
                f"TASK: Create a PROFESSIONAL, PRINT-READY GRAPHIC DESIGN. "
                f"CLIENT BRIEF: {description}. "
                f"EXECUTION: Analyze the client's request and determine the best design approach. "
                f"Create visually stunning artwork with professional composition, color theory, "
                f"and visual hierarchy. Use a cohesive color palette, clean typography, and balanced layout. "
                f"QUALITY: The output should look like it was produced by a top design agency — "
                f"polished, pixel-perfect, ready for professional printing or digital use.{dim_info}"
            )

    # ── Layer 5: Append learned insights ─────────────────────────────
    if learned_suffix:
        enhanced_desc += learned_suffix

    # Generate via OpenAI
    openai_key = getattr(settings, 'OPENAI_API_KEY', None)
    if not openai_key:
        return JsonResponse({"error": "محرك التوليد غير متاح حالياً"}, status=503)

    # Handle logo file — read into memory before API calls
    logo_file = request.FILES.get('logo') if (
        not using_free_trial and purchase and purchase.package.allows_logo_upload
    ) else None
    logo_bytes = None
    if logo_file:
        logo_bytes = logo_file.read()
        logo_file.seek(0)  # Reset for later save
        # Enhance prompt to instruct AI to integrate the logo
        enhanced_desc += " IMPORTANT: Integrate the provided company logo naturally into the design. The logo should be clearly visible and well-placed within the composition."

    try:
        import openai
        client = openai.OpenAI(api_key=openai_key)

        # Try gpt-image-1 → dall-e-3 → dall-e-2
        models_chain = ['gpt-image-1', 'dall-e-3', 'dall-e-2']
        response = None
        used_model = None
        for m in models_chain:
            try:
                # Map size per model
                if m == 'gpt-image-1':
                    model_size = GPT_IMAGE_SIZE_MAP.get(canonical_size, '1024x1024')
                elif m == 'dall-e-2':
                    model_size = '1024x1024'
                else:
                    model_size = canonical_size  # dall-e-3 supports 1024x1792

                quality_level = purchase.package.quality_level if purchase else 'standard'

                # If logo uploaded and model supports image editing → use edit API
                if logo_bytes and m == 'gpt-image-1':
                    import io
                    logo_io = io.BytesIO(logo_bytes)
                    logo_io.name = 'logo.png'
                    edit_kwargs = {
                        'model': m,
                        'image': [logo_io],
                        'prompt': enhanced_desc[:4000],
                        'size': model_size,
                        'n': 1,
                        'quality': 'high' if quality_level in ('hd', 'ultra') else 'medium',
                    }
                    response = client.images.edit(**edit_kwargs)
                    used_model = m
                    break
                else:
                    # Standard generation (no logo or non-gpt-image model)
                    kwargs = {'model': m, 'prompt': enhanced_desc[:4000], 'size': model_size, 'n': 1}
                    if m == 'dall-e-3':
                        kwargs['quality'] = 'hd' if quality_level in ('hd', 'ultra') else 'standard'
                    elif m == 'gpt-image-1':
                        kwargs['quality'] = 'high' if quality_level in ('hd', 'ultra') else 'medium'
                    response = client.images.generate(**kwargs)
                    used_model = m
                    break
            except openai.BadRequestError as e:
                err_str = str(e)
                recoverable = ('does not exist', 'model_not_found', 'invalid_value',
                               'Invalid size', 'invalid_size', 'not supported',
                               'Could not process', 'invalid_image')
                if any(k in err_str for k in recoverable):
                    logger.warning(f"[DESIGN STORE] Model {m} failed: {err_str[:120]}, trying next...")
                    continue
                raise

        if not response:
            return JsonResponse({"error": "فشل التوليد. حاول لاحقاً."}, status=502)

        # Save image to disk
        first = response.data[0]
        image_url = getattr(first, 'url', None)
        if not image_url:
            b64 = getattr(first, 'b64_json', None)
            if b64:
                import base64 as _b64, uuid as _uuid
                from django.core.files.base import ContentFile
                from django.core.files.storage import default_storage
                img_bytes = _b64.b64decode(b64)
                filename = f"ai_store/{customer.uid}/{_uuid.uuid4().hex}.png"
                saved_path = default_storage.save(filename, ContentFile(img_bytes))
                image_url = default_storage.url(saved_path)
                if image_url.startswith('/'):
                    image_url = request.build_absolute_uri(image_url)
        else:
            # Download and persist DALL-E URLs (they expire)
            try:
                import requests as _req, uuid as _uuid
                from django.core.files.base import ContentFile
                from django.core.files.storage import default_storage
                r = _req.get(image_url, timeout=30)
                if r.status_code == 200:
                    filename = f"ai_store/{customer.uid}/{_uuid.uuid4().hex}.png"
                    saved_path = default_storage.save(filename, ContentFile(r.content))
                    local = default_storage.url(saved_path)
                    if local.startswith('/'):
                        local = request.build_absolute_uri(local)
                    image_url = local
            except Exception as e:
                logger.warning(f"[DESIGN STORE] Failed to persist: {e}")

        # Create design record
        # Free trial: 0 regenerations. Paid: from package settings.
        if using_free_trial:
            regen_limit = 0
        elif purchase:
            regen_limit = purchase.package.free_regenerations_per_design
        else:
            regen_limit = 0

        design = CustomerDesign.objects.create(
            customer=customer,
            purchase=purchase,
            is_free_trial=using_free_trial,
            title=title or description[:60], description=description,
            category=category, size_preset=size_preset,
            custom_width_px=int(custom_w) if custom_w.isdigit() else None,
            custom_height_px=int(custom_h) if custom_h.isdigit() else None,
            weight_kg=Decimal(weight) if weight else None,
            output_format=output_format,
            raw_input=description, engineered_prompt=enhanced_desc[:4000],
            image_url=image_url, model_used=used_model or 'unknown',
            regenerations_allowed=regen_limit,
        )

        # Save logo to design record
        if logo_file:
            logo_file.seek(0)
            design.logo_image = logo_file
            design.save(update_fields=['logo_image'])

        # Log prompt for AI learning
        try:
            from clients.models import DesignPromptLog
            DesignPromptLog.objects.create(
                category=category,
                user_prompt=description[:500],
                engineered_prompt=enhanced_desc[:4000],
                model_used=used_model or '',
                size_used=canonical_size,
                customer_rating=None,
                design=design,
            )
        except Exception:
            pass  # Non-critical

        # Consume 1 design from the right source
        if using_free_trial:
            customer.consume_free_design()
            remaining = customer.free_designs_remaining
        else:
            purchase.consume_design()
            purchase.refresh_from_db()
            remaining = purchase.designs_remaining

        # Build download URLs for different formats
        download_urls = {
            'png': image_url,
            'pdf': f"/marketplace/design-store/{design.design_code}/download/pdf/",
            'jpg': f"/marketplace/design-store/{design.design_code}/download/jpg/",
        }

        return JsonResponse({
            "status": "success",
            "design_id": design.pk,
            "design_code": str(design.design_code),
            "image_url": image_url,
            "model_used": used_model,
            "size": design.actual_size_label,
            "remaining_in_package": remaining,
            "regenerations_left": design.regenerations_allowed,
            "is_free_trial": using_free_trial,
            "can_download": not using_free_trial,
            "can_send_whatsapp": not using_free_trial,
            "download_urls": download_urls if not using_free_trial else {},
        })

    except Exception as e:
        logger.error(f"[DESIGN STORE] Generate failed: {e}")
        return JsonResponse({"error": "حدث خطأ أثناء إنشاء التصميم. حاول مرة أخرى."}, status=500)


@csrf_exempt
def design_store_send_whatsapp(request, design_code):
    """📱 إرسال التصميم للعميل على واتساب."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "غير مصرح"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)
    target_phone = request.POST.get('phone', customer.phone).strip()
    custom_message = request.POST.get('message', '').strip()

    if not target_phone:
        return JsonResponse({"error": "رقم الواتساب مطلوب"}, status=400)

    # Build wa.me deep link
    from urllib.parse import quote
    msg = custom_message or f"تصميمك من Mouss Tec AI Store جاهز!\n\nالعنوان: {design.title}\nالمقاس: {design.actual_size_label}\n\n{design.image_url}"
    phone_clean = target_phone.lstrip('+').lstrip('0')
    if not phone_clean.startswith('20'):
        phone_clean = '20' + phone_clean
    wa_url = f"https://wa.me/{phone_clean}?text={quote(msg)}"

    # Mark as sent
    design.sent_to_whatsapp = target_phone
    design.sent_at = timezone.now()
    design.save(update_fields=['sent_to_whatsapp', 'sent_at'])

    return JsonResponse({"status": "success", "whatsapp_url": wa_url})


def design_store_download(request, design_code, fmt):
    """📥 تحميل التصميم بصيغ مختلفة (png, jpg, pdf)."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "غير مصرح"}, status=401)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    if design.is_free_trial:
        return JsonResponse({"error": "التحميل غير متاح في التجربة المجانية"}, status=403)

    fmt = fmt.lower()
    if fmt not in ('png', 'jpg', 'jpeg', 'pdf'):
        return JsonResponse({"error": "صيغة غير مدعومة. الصيغ المتاحة: png, jpg, pdf"}, status=400)

    # Get the image file
    from django.core.files.storage import default_storage
    import io

    # Find the saved image path from the URL
    image_path = None
    if design.image_url:
        # Extract relative path from URL
        url = design.image_url
        for prefix in ['/media/', 'media/']:
            if prefix in url:
                image_path = url.split(prefix, 1)[-1]
                break

    if not image_path or not default_storage.exists(image_path):
        # Try downloading from the URL directly
        try:
            import requests as _req
            r = _req.get(design.image_url, timeout=30)
            if r.status_code == 200:
                img_data = r.content
            else:
                return JsonResponse({"error": "تعذر تحميل الصورة"}, status=404)
        except Exception:
            return JsonResponse({"error": "تعذر تحميل الصورة"}, status=404)
    else:
        with default_storage.open(image_path, 'rb') as f:
            img_data = f.read()

    from PIL import Image as PILImage

    if fmt == 'png':
        response = HttpResponse(img_data, content_type='image/png')
        response['Content-Disposition'] = f'attachment; filename="design_{design.design_code}.png"'
        return response

    img = PILImage.open(io.BytesIO(img_data))

    if fmt in ('jpg', 'jpeg'):
        # Convert to RGB (remove alpha) and save as JPEG
        if img.mode in ('RGBA', 'P', 'LA'):
            bg = PILImage.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            bg.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=95)
        response = HttpResponse(buf.getvalue(), content_type='image/jpeg')
        response['Content-Disposition'] = f'attachment; filename="design_{design.design_code}.jpg"'
        return response

    if fmt == 'pdf':
        # Convert image to PDF
        if img.mode == 'RGBA':
            bg = PILImage.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='PDF', resolution=300)
        response = HttpResponse(buf.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="design_{design.design_code}.pdf"'
        return response


@csrf_exempt
def design_store_regenerate(request, design_code):
    """🔄 إعادة توليد تصميم — مجاناً ضمن الحد المسموح."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "غير مصرح"}, status=401)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)
    if not design.can_regenerate:
        return JsonResponse({
            "error": f"استنفدت إعادة التوليد المسموحة ({design.regenerations_allowed} مرات)",
        }, status=403)

    # 🛡️ Rate limiting — shared with generate (5/min per customer)
    gen_rate_key = f'design_gen_rate:{customer.pk}'
    gen_count = cache.get(gen_rate_key, 0)
    if gen_count >= 5:
        return JsonResponse({"error": "أنت ترسل طلبات كثيرة. انتظر دقيقة ثم حاول مرة أخرى."}, status=429)
    cache.set(gen_rate_key, gen_count + 1, 60)

    # Re-generate with same specs by calling generate endpoint internally
    request.POST = request.POST.copy()
    request.POST['title'] = design.title
    request.POST['description'] = design.description
    request.POST['category'] = design.category
    request.POST['size_preset'] = design.size_preset
    request.POST['output_format'] = design.output_format

    # Don't consume from package — increment regen counter instead
    design.regenerations_used += 1
    design.save(update_fields=['regenerations_used'])

    # Inline regenerate without consuming purchase quota
    # (simplified — reuse same OpenAI call)
    openai_key = getattr(settings, 'OPENAI_API_KEY', None)
    try:
        import openai
        client = openai.OpenAI(api_key=openai_key)
        gpt_size_map = {
            '1024x1024': '1024x1024', '1024x1536': '1024x1536', '1536x1024': '1536x1024',
            '1024x1792': '1024x1536', '1792x1024': '1536x1024', 'auto': 'auto',
        }
        sz = gpt_size_map.get(design.size_preset, '1024x1024')
        resp = client.images.generate(
            model='gpt-image-1', prompt=design.engineered_prompt[:2000] or design.description,
            size=sz, n=1, quality='high',
        )
        first = resp.data[0]
        new_url = getattr(first, 'url', None)
        if not new_url and hasattr(first, 'b64_json'):
            import base64 as _b64, uuid as _uuid
            from django.core.files.base import ContentFile
            from django.core.files.storage import default_storage
            img_bytes = _b64.b64decode(first.b64_json)
            filename = f"ai_store/{customer.uid}/regen_{_uuid.uuid4().hex}.png"
            saved = default_storage.save(filename, ContentFile(img_bytes))
            new_url = request.build_absolute_uri(default_storage.url(saved))

        design.image_url = new_url
        design.save(update_fields=['image_url'])

        return JsonResponse({
            "status": "success",
            "image_url": new_url,
            "regenerations_left": design.regenerations_allowed - design.regenerations_used,
        })
    except Exception as e:
        logger.error(f"[REGEN] Failed: {e}")
        return JsonResponse({"error": "فشل إعادة التوليد"}, status=500)


@csrf_exempt
def design_store_print_request(request, design_code):
    """🖨️ طلب طباعة تصميم — العميل عجبه التصميم وعاوز يطبعه."""
    from clients.models import DesignPrintRequest

    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    product_type = request.POST.get('product_type', 'other')
    quantity = request.POST.get('quantity', '1')
    width_cm = request.POST.get('width_cm', '').strip()
    height_cm = request.POST.get('height_cm', '').strip()
    paper_type = request.POST.get('paper_type', '').strip()
    color_mode = request.POST.get('color_mode', 'full_color')
    finishing = request.POST.get('finishing', '').strip()
    notes = request.POST.get('notes', '').strip()
    delivery_address = request.POST.get('delivery_address', '').strip()
    delivery_phone = request.POST.get('delivery_phone', '').strip()

    try:
        qty = int(quantity)
        if qty < 1:
            qty = 1
    except (ValueError, TypeError):
        qty = 1

    print_req = DesignPrintRequest.objects.create(
        design=design,
        customer=customer,
        product_type=product_type,
        quantity=qty,
        width_cm=Decimal(width_cm) if width_cm else None,
        height_cm=Decimal(height_cm) if height_cm else None,
        paper_type=paper_type,
        color_mode=color_mode,
        finishing=finishing,
        notes=notes,
        delivery_address=delivery_address,
        delivery_phone=delivery_phone or customer.phone,
        status='pending',
    )

    logger.info(f"[PRINT REQUEST] #{print_req.pk} — {customer.full_name} wants to print design {design.design_code}")

    return JsonResponse({
        "status": "success",
        "request_id": print_req.pk,
        "request_code": str(print_req.request_code),
        "message": "تم إرسال طلب الطباعة بنجاح! سنتواصل معك قريباً بعرض السعر.",
    })


@csrf_exempt
def design_store_send_to_marketplace(request, design_code):
    """🛒 إرسال التصميم لسوق B2B — ينشئ ServiceRequest للتجار (المطابع) يقدموا عروض."""
    from clients.models import ServiceRequest

    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    if design.is_free_trial:
        return JsonResponse({"error": "هذه الميزة غير متاحة في التجربة المجانية"}, status=403)

    # Check if already sent to marketplace
    existing = ServiceRequest.objects.filter(
        customer=customer,
        title__contains=str(design.design_code)[:8],
        status='open',
    ).first()
    if existing:
        return JsonResponse({
            "status": "already_exists",
            "request_code": str(existing.request_code),
            "message": "التصميم موجود بالفعل في السوق وبيستقبل عروض.",
        })

    notes = request.POST.get('notes', '').strip()
    quantity = request.POST.get('quantity', '1').strip()
    urgency = request.POST.get('urgency', 'normal')

    try:
        qty = int(quantity)
        if qty < 1:
            qty = 1
    except (ValueError, TypeError):
        qty = 1

    # Build description for merchants
    desc = (
        f"طلب طباعة تصميم AI — {design.get_category_display()}\n"
        f"المقاس: {design.actual_size_label}\n"
        f"الكمية: {qty}\n"
    )
    if notes:
        desc += f"ملاحظات العميل: {notes}\n"
    desc += f"\nرابط التصميم: {design.image_url}"

    # Create ServiceRequest in B2B marketplace
    from datetime import timedelta
    sr = ServiceRequest.objects.create(
        customer=customer,
        sector='printing',
        title=f"طباعة {design.get_category_display()} — {design.title[:60]} [{str(design.design_code)[:8]}]",
        description=desc,
        urgency=urgency if urgency in ('normal', 'soon', 'urgent') else 'normal',
        customer_city=customer.city or '',
        expires_at=timezone.now() + timedelta(days=7),
    )

    # Attach design image as reference
    if design.image_url:
        try:
            import requests as _req
            from django.core.files.base import ContentFile
            r = _req.get(design.image_url, timeout=15)
            if r.status_code == 200:
                from django.core.files.uploadedfile import InMemoryUploadedFile
                import io
                sr.attachment_1.save(
                    f"design_{design.design_code}.png",
                    ContentFile(r.content),
                    save=True,
                )
        except Exception as e:
            logger.warning(f"[MARKETPLACE] Failed to attach design image: {e}")

    logger.info(f"[MARKETPLACE] Design {design.design_code} sent to B2B by {customer.full_name}")

    return JsonResponse({
        "status": "success",
        "request_code": str(sr.request_code),
        "message": f"تم نشر تصميمك في سوق الطباعة. المطابع هتبدأ تبعتلك عروض أسعار قريباً.",
    })


@csrf_exempt
def design_store_watermark(request, design_code):
    """💧 إضافة / إزالة علامة مائية على التصميم."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    if design.is_free_trial:
        return JsonResponse({"error": "العلامة المائية غير متاحة في التجربة المجانية"}, status=403)

    watermark_text = request.POST.get('text', customer.company_name or customer.full_name).strip()
    if not watermark_text:
        watermark_text = 'Mouss Tec AI Design'

    # Get the original image
    from django.core.files.storage import default_storage
    import io
    from PIL import Image as PILImage, ImageDraw, ImageFont

    img_data = None
    # Try local storage first
    if design.image_url:
        url = design.image_url
        for prefix in ['/media/', 'media/']:
            if prefix in url:
                rel_path = url.split(prefix, 1)[-1]
                if default_storage.exists(rel_path):
                    with default_storage.open(rel_path, 'rb') as f:
                        img_data = f.read()
                break

    if not img_data:
        try:
            import requests as _req
            r = _req.get(design.image_url, timeout=30)
            if r.status_code == 200:
                img_data = r.content
        except Exception:
            pass

    if not img_data:
        return JsonResponse({"error": "تعذر تحميل الصورة"}, status=404)

    # Apply watermark
    img = PILImage.open(io.BytesIO(img_data)).convert('RGBA')
    w, h = img.size

    # Create transparent overlay
    overlay = PILImage.new('RGBA', (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Use a large font size relative to image
    font_size = max(w, h) // 15
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    # Draw diagonal watermark text multiple times across image
    import math
    diagonal = int(math.sqrt(w**2 + h**2))
    step_y = font_size * 3

    for y_offset in range(-diagonal, diagonal, step_y):
        for x_offset in range(-w, w * 2, len(watermark_text) * font_size):
            draw.text(
                (x_offset, y_offset),
                watermark_text,
                font=font,
                fill=(255, 255, 255, 45),  # Semi-transparent white
            )

    # Rotate overlay
    overlay = overlay.rotate(30, expand=False, center=(w // 2, h // 2))

    # Composite
    watermarked = PILImage.alpha_composite(img, overlay)
    watermarked_rgb = watermarked.convert('RGB')

    # Save watermarked version
    import uuid as _uuid
    from django.core.files.base import ContentFile
    buf = io.BytesIO()
    watermarked_rgb.save(buf, format='PNG', quality=95)
    buf.seek(0)

    filename = f"ai_store/{customer.uid}/wm_{_uuid.uuid4().hex}.png"
    saved_path = default_storage.save(filename, ContentFile(buf.getvalue()))
    wm_url = default_storage.url(saved_path)
    if wm_url.startswith('/'):
        wm_url = request.build_absolute_uri(wm_url)

    return JsonResponse({
        "status": "success",
        "watermarked_url": wm_url,
        "message": "تم إضافة العلامة المائية بنجاح",
    })
