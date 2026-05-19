from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.http import JsonResponse, HttpResponseForbidden
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Count, Min, Sum, F, Avg, Max
from django.utils import timezone
from django.db import transaction, connection
from django.contrib.auth import get_user_model
from django.contrib import messages
from django.core.cache import cache
from django.utils.text import slugify
from django_tenants.utils import schema_context
from decimal import Decimal
from datetime import timedelta
import json
import logging
import uuid
import os
import secrets

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
                            admin_user.set_password(data['password'])
                            if not created:
                                admin_user.first_name = name_parts[0]
                                admin_user.last_name = name_parts[1] if len(name_parts) > 1 else ''
                                admin_user.is_staff = True
                                admin_user.is_superuser = True
                            admin_user.save()

                            try:
                                from inventory.models import EmployeeProfile, ProductCategory
                                EmployeeProfile.objects.get_or_create(user=admin_user, defaults={'role': 'admin', 'can_edit_posted_invoices': True})
                                if business_type in ['service_center', 'both']:
                                    ProductCategory.objects.get_or_create(name='أجور مصنعيات وخدمات', is_service=True)
                                elif business_type == 'parts_dealer':
                                    ProductCategory.objects.get_or_create(name='قطع غيار ميكانيكا', is_service=False)
                                    ProductCategory.objects.get_or_create(name='زيوت وفلاتر', is_service=False)
                            except ImportError:
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
        form = TenantSignupForm()

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
    if request.user.is_superuser:
        return redirect('/superadmin/')
    if tenant and tenant.schema_name != 'public':
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
            tenant = Client.objects.filter(email__iexact=email).exclude(schema_name='public').first()
            if not tenant:
                # بحث موسع: البريد قد يكون للمالك وليس للشركة
                from django_tenants.utils import get_tenant_model
                all_tenants = Client.objects.exclude(schema_name='public')
                for t in all_tenants:
                    try:
                        with schema_context(t.schema_name):
                            if User.objects.filter(email__iexact=email).exists():
                                tenant = t
                                break
                    except Exception:
                        continue

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

            error = "لا يوجد حساب مرتبط بهذا البريد الإلكتروني. تأكد من البريد أو أنشئ حساباً جديداً."
    return render(request, 'clients/login_finder.html', {'error': error})


def mousstec_landing_page(request):
    return render(request, 'clients/landing.html')

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

@csrf_exempt
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
                    buyer_tenant.wallet_balance -= total_req
                    buyer_tenant.escrow_held += total_req
                    buyer_tenant.save(update_fields=['wallet_balance', 'escrow_held'])
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

    return render(request, 'clients/pricing.html', {
        'tenant': tenant, 'shop': shop_schema,
        'pricing': {
            'silver': {'price': 685, 'users': 1, 'branches': 1, 'treasuries': 1},
            'gold': {'price': 1185, 'users': 4, 'branches': 2, 'treasuries': 2},
            'empire': {'price': 3000},
            'addon_price': 125,
        }
    })


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


@csrf_exempt
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
@user_passes_test(lambda u: u.is_active and u.is_superuser, login_url='/secure-portal/login/')
def super_admin_dashboard(request):

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

    return render(request, 'clients/super_admin.html', {
        'tenants': tenants,
        'summary': summary,
        'today': today,
    })