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
from clients.models import Client, Domain, GlobalB2BMarketplace, BlindBiddingRequest, BidOffer, EscrowLedger

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
                        tenant = Client.objects.create(
                            schema_name=schema_name,
                            name=company_name,
                            owner_name=data.get('full_name', company_name),
                            email=data['email'],
                            phone=data.get('phone', ''),
                            industry=industry,
                            business_type=business_type,
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
                'otp_hint': otp_code if not email_sent else '',  # إظهار الكود إذا لم يتم إرساله بالإيميل
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

            if not new_password or len(new_password) < 6:
                tenant = Client.objects.filter(schema_name=schema_name).first()
                context = {
                    'step': 'reset',
                    'tenant_name': tenant.name if tenant else '',
                    'tenant_schema': schema_name,
                    'reset_token': reset_token,
                    'error': 'كلمة السر يجب أن تكون 6 أحرف على الأقل.',
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
    
    # 🛡️ الحماية السيبرانية: توثيق مصدر الـ Webhook (يجب تفعيله في الـ Production)
    # expected_sig = request.headers.get('STRIPE_SIGNATURE', '')
    # if not verify_stripe_signature(request.body, expected_sig): return HttpResponseForbidden()
    
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
    data = [{"bid_id": b.id, "part_number": b.part_number, "required_qty": b.required_qty, "buyer_name": "مشتري سري" if b.auto_award else b.buyer.name, "urgency": "High" if (b.expires_at - timezone.now()).total_seconds() < 7200 else "Normal"} for b in active_bids]
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
    except Exception as e: return JsonResponse({"error": str(e)}, status=500)

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
            'vodafone_cash': '01094850763',
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

    return render(request, 'clients/manage_subscription.html', {
        'tenant': tenant,
        'prorated_cost': prorated_cost,
        'full_addon_price': float(Client.ADDON_PRICE_PER_MONTH),
        'remaining_days': remaining_days,
        'result_msg': result_msg,
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
        return JsonResponse({"error": str(e)}, status=500)


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

    # 🛡️ حماية مزدوجة: حتى لو عدى الـ decorator، نتأكد إنه على public schema
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
        elif action == 'activate':
            target.status = 'active'
            target.is_active = True
            target.save(update_fields=['status', 'is_active'])
        elif action == 'flag_fraud':
            target.is_fraud_flagged = True
            target.save(update_fields=['is_fraud_flagged'])
        elif action == 'unflag_fraud':
            target.is_fraud_flagged = False
            target.save(update_fields=['is_fraud_flagged'])
        elif action == 'extend_trial':
            target.trial_ends_at = target.trial_ends_at + timedelta(days=3)
            target.save(update_fields=['trial_ends_at'])
        elif action == 'activate_subscription':
            plan = request.POST.get('plan', 'silver')
            billing_period = request.POST.get('billing_period', 'monthly')
            # ── خريطة الأسعار والخصومات ──
            plan_prices = {'silver': 475, 'gold': 700, 'empire': 1400}
            period_days = {'monthly': 30, 'quarterly': 90, 'semi_annual': 180, 'annual': 365}
            period_discounts = {'monthly': Decimal('0'), 'quarterly': Decimal('0.09'),
                                'semi_annual': Decimal('0.125'), 'annual': Decimal('0.25')}
            period_labels = {'monthly': 'شهري', 'quarterly': 'ربع سنوي',
                             'semi_annual': 'نصف سنوي', 'annual': 'سنوي'}
            months_map = {'monthly': 1, 'quarterly': 3, 'semi_annual': 6, 'annual': 12}

            base_price = Decimal(str(plan_prices.get(plan, 475)))
            discount = period_discounts.get(billing_period, Decimal('0'))
            months = months_map.get(billing_period, 1)
            total = (base_price * months * (1 - discount)).quantize(Decimal('1'))
            days = period_days.get(billing_period, 30)

            target.plan = plan
            target.status = 'active'
            target.is_active = True
            target.subscription_end_date = timezone.localdate() + timedelta(days=days)
            target.save(update_fields=['plan', 'status', 'is_active', 'subscription_end_date'])

            messages.success(request,
                f'✅ تم تفعيل اشتراك «{target.name}» — باقة {target.get_plan_display()} '
                f'({period_labels.get(billing_period, billing_period)}) — {total} ج.م — '
                f'ينتهي {target.subscription_end_date}')
        return redirect('super_admin_dashboard')

    tenants = Client.objects.exclude(schema_name='public').order_by('-created_on')
    today = timezone.localdate()

    summary = {
        'total': tenants.count(),
        'trial': tenants.filter(status='trial').count(),
        'active': tenants.filter(status='active').count(),
        'suspended': tenants.filter(status='suspended').count(),
        'fraud': tenants.filter(is_fraud_flagged=True).count(),
    }

    # ── بيانات الباقات للمودال ──
    plan_prices_json = json.dumps({'silver': 475, 'gold': 700, 'empire': 1400})
    period_discounts_json = json.dumps({'monthly': 0, 'quarterly': 0.09, 'semi_annual': 0.125, 'annual': 0.25})
    period_months_json = json.dumps({'monthly': 1, 'quarterly': 3, 'semi_annual': 6, 'annual': 12})

    return render(request, 'clients/super_admin.html', {
        'tenants': tenants,
        'summary': summary,
        'today': today,
        'plan_prices_json': plan_prices_json,
        'period_discounts_json': period_discounts_json,
        'period_months_json': period_months_json,
    })


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
- سيلفر (475 ج/شهر): فرع واحد + موظف واحد + خزينة واحدة — مناسب للورش الصغيرة
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
        from inventory.ai_services import call_gemini_layer

        ai_enabled = getattr(settings, 'ENABLE_AI_PREDICTIONS', False)
        api_key = getattr(settings, 'AI_VISION_API_KEY', None)

        if not ai_enabled or not api_key:
            return JsonResponse({
                'reply': '⚠️ المساعد الذكي غير مُفعّل حالياً. تواصل مع فريق الدعم للمساعدة.',
                'status': 'no_api_key'
            })

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

        reply = call_gemini_layer(messages, json_mode=False, max_retries=1)

        if not reply:
            return JsonResponse({
                'reply': '⚠️ حدث خطأ مؤقت في المحرك. حاول مرة أخرى بعد قليل.',
                'status': 'error'
            })

        return JsonResponse({
            'reply': reply,
            'status': 'ok'
        })

    except ImportError:
        return JsonResponse({
            'reply': '⚠️ محرك الذكاء الاصطناعي غير متاح حالياً.',
            'status': 'no_library'
        })
    except json.JSONDecodeError:
        return JsonResponse({'error': 'بيانات غير صالحة'}, status=400)
    except Exception as e:
        logger.error(f'AI Assistant error: {e}')
        return JsonResponse({
            'reply': '⚠️ حدث خطأ مؤقت. حاول مرة أخرى بعد قليل.',
            'status': 'error'
        })