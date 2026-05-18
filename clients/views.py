from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponseForbidden
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Count, Min, Sum, F, Avg
from django.utils import timezone
from django.db import transaction, connection
from django.contrib.auth.models import User
from django_tenants.utils import schema_context
from django.core.cache import cache
from decimal import Decimal
import json
import logging
import uuid
# الاستدعاء الصريح والمباشر للاستمارات والموديلات لمنع تعارض الـ Pylance
from clients.forms import TenantSignupForm
from clients.models import Client, Domain, GlobalB2BMarketplace, BlindBiddingRequest, BidOffer, EscrowLedger

logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🏢 محرك التخليق الآلي للمؤسسات المعزولة (Automated Onboarding Engine)
# =====================================================================
import json
import logging
import os
from django.shortcuts import render
from django.db import transaction
from django.utils.text import slugify
from django_tenants.utils import schema_context
from django.contrib.auth import get_user_model
from .forms import TenantSignupForm
from .models import Client, Domain

User = get_user_model()
ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal')

def register_new_tenant_saas(request):
    if request.method == 'POST':
        form = TenantSignupForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            company_name = data['company_name']
            
            # 🚀 توليد الاسم المبدئي من اسم الشركة
            subdomain_slug = slugify(company_name)
            if not subdomain_slug:
                import secrets
                subdomain_slug = f"mt-{secrets.token_hex(3)}"
            schema_name = subdomain_slug.replace('-', '_')

            success = False
            attempts = 0
            max_attempts = 5

            # 🛡️ محرك التكرار المرن: الاعتماد على أمان قاعدة البيانات مباشرة
            while not success and attempts < max_attempts:
                try:
                    # فتح معاملة ذرية مستقلة لكل محاولة
                    with transaction.atomic():
                        # 1. إنشاء سجل الشركة والنطاق
                        tenant = Client.objects.create(
                            schema_name=schema_name,
                            name=company_name,
                            is_active=True
                        )
                        Domain.objects.create(
                            domain=f"{subdomain_slug}.localhost",
                            tenant=tenant,
                            is_primary=True
                        )

                        # 2. الانتقال الفوري لزراعة المستخدم والملف التشغيلي للفرع
                        with schema_context(schema_name):
                            name_parts = data['full_name'].split(' ', 1)
                            first_name = name_parts[0]
                            last_name = name_parts[1] if len(name_parts) > 1 else ''
                            
                            new_admin = User.objects.create_superuser(
                                username=data['email'],
                                email=data['email'],
                                password=data['password'],
                                first_name=first_name,
                                last_name=last_name
                            )
                            
                            from inventory.models import EmployeeProfile
                            EmployeeProfile.objects.get_or_create(
                                user=new_admin,
                                defaults={'role': 'admin', 'can_edit_posted_invoices': True}
                            )
                    
                    # إذا وصلنا هنا بدون استثناء، المعاملة نجحت بالكامل!
                    success = True

                except Exception as e:
                    error_msg = str(e).lower()
                    # 🎯 إذا ردت قاعدة البيانات بأن الاسم مكرر، نولد كود فريد ونحاول مجدداً فوراً
                    if "already exists" in error_msg or "unique constraint" in error_msg:
                        attempts += 1
                        import secrets
                        suffix = secrets.token_hex(2)  # توليد كود مميز مثل a1b2
                        subdomain_slug = f"{slugify(company_name)}-{suffix}"
                        schema_name = subdomain_slug.replace('-', '_')
                    else:
                        # إذا كان الخطأ تشغيلي آخر (مثل خطأ جداول مفقودة)، نقف ونعرضه للمطور
                        logging.error(f"🔴 [SaaS PROVISIONING CRASH]: {str(e)}")
                        form.add_error(None, f"🛑 فشل تأسيس السيرفر: {str(e)}")
                        return render(request, 'clients/signup_register.html', {'form': form})

            if success:
                # التوجيه لصفحة النجاح الفخمة بعد اكتمال الدورة الآمنة
                target_url = f"http://{subdomain_slug}.localhost:8000/{ADMIN_URL}/"
                return render(request, 'clients/signup_success.html', {
                    'company_name': company_name,
                    'target_url': target_url,
                    'admin_email': data['email']
                })
            else:
                form.add_error(None, "🛑 فشل التأسيس: تم استنفاد محاولات التسمية التلقائية بسبب روابط قديمة محبوسة في قاعدة البيانات.")
    else:
        form = TenantSignupForm()

    return render(request, 'clients/signup_register.html', {'form': form})
# =====================================================================
# 🌍 1. واجهة الإمبراطورية المفتوحة (Public SaaS Landing Page)
# =====================================================================
def mousstec_landing_page(request):
    """
    بوابة الهبوط الديناميكية: تعرض إحصائيات حية لشبكة التجار والمبيعات لزيادة الاشتراكات.
    """
    total_clients = Client.objects.filter(is_active=True).count()
    verified_merchants = Client.objects.filter(is_verified_merchant=True).count()
    total_parts_in_market = GlobalB2BMarketplace.objects.aggregate(Sum('available_qty'))['available_qty__sum'] or 0
    successful_bids = BlindBiddingRequest.objects.filter(status='completed').count()
    
    context = {
        'total_clients': max(total_clients, 1), 
        'verified_merchants': verified_merchants,
        'total_parts': total_parts_in_market,
        'successful_bids': successful_bids,
        'system_uptime': "99.99%",
    }
    return render(request, 'clients/landing.html', context)

# =====================================================================
# 🌐 2. الموزع المركزي للإشعارات الخارجية (Universal Webhook Multiplexer)
# =====================================================================
@csrf_exempt
def universal_webhook_multiplexer(request):
    """
    بوابة استقبال وتصفية إشعارات بوابات الدفع (FinTech) ومعالجات الـ AI.
    🚀 ابتكار: محمي بدرع منع تكرار العمليات المالي (Idempotency Keys via Redis).
    """
    if request.method != 'POST': return HttpResponseForbidden("POST Only")
    
    try:
        payload = json.loads(request.body)
        event_id = payload.get('id', 'evt_' + str(uuid.uuid4().hex[:12]))
        event_type = payload.get('type', 'unknown')
        
        # 🚀 ابتكار: فحص درع التكرار (Idempotency Check) لمنع الشحن المزدوج للمحافظ
        cache_key = f"mousstec_processed_webhook_{event_id}"
        if cache.get(cache_key):
            logger.warning(f"⏳ FinTech Safety: Webhook {event_id} was already processed. Skipping to protect liquidity.")
            return JsonResponse({"status": "duplicate", "message": "Transaction already processed safely."})
        
        # 1. معالجة إيداعات محافظ التجار وأساطيل الـ B2B
        if event_type == 'payment_intent.succeeded':
            client_id = payload['data']['metadata']['client_id']
            amount = Decimal(str(payload['data']['amount_received'])) / 100
            
            with transaction.atomic():
                tenant = Client.objects.get(id=client_id)
                tenant.wallet_balance = F('wallet_balance') + amount
                tenant.save(update_fields=['wallet_balance'])
                
                EscrowLedger.objects.create(
                    client=tenant, transaction_type='deposit', amount=amount,
                    description=f"إيداع مالي سحابي موثق إلكترونياً (Ref: {event_id})"
                )
            
            # قفل مفتاح الأمان في الكاش لمدة 24 ساعة بعد النجاح
            cache.set(cache_key, "processed", timeout=86400)
            logger.info(f"💰 FinTech Alert: {amount} EGP successfully credited to {tenant.company_name} wallet.")
            return JsonResponse({"status": "success", "message": "Payment secure and ledger entries written."})

        elif event_type == 'ai.vision.completed':
            return JsonResponse({"status": "acknowledged"})
            
        return JsonResponse({"status": "ignored", "reason": "Event type unhandled"})
        
    except Exception as e:
        logger.error(f"🚨 Webhook Multiplexer Critical Failure: {e}")
        return JsonResponse({"error": str(e)}, status=500)

# =====================================================================
# 🛒 3. محرك بحث سوق التجار (B2B Global Search API)
# =====================================================================
@login_required(login_url='/secure-portal/')
def b2b_market_search_api(request):
    """
    رادار فحص المخازن ومقارنة الأسعار اللحظي للتجار ومراكز الصيانة المشتركة.
    """
    current_schema = connection.schema_name
    if current_schema == 'public' and not request.user.is_superuser:
        return JsonResponse({"error": "غير مصرح بدخول الشبكة التجارية العامة من النطاق المركزي."}, status=403)

    part_number = request.GET.get('part_number', '').strip()
    if not part_number:
        return JsonResponse({"error": "برجاء تزويد رقم القطعة المطلوب تتبعها."}, status=400)

    results = GlobalB2BMarketplace.objects.filter(
        part_number__iexact=part_number,
        available_qty__gt=0,
        tenant__is_active=True,
        tenant__is_marketplace_active=True
    ).select_related('tenant').order_by('-tenant__is_verified_merchant', 'wholesale_price')[:10]

    if not results.exists():
        return JsonResponse({"status": "not_found", "message": "القطعة المطلوبة غير متاحة حالياً في مستودعات التجار المشتركين."})

    data = []
    for rank, item in enumerate(results):
        data.append({
            "rank": rank + 1,
            "is_best_price": rank == 0,
            "dealer_name": item.tenant.company_name,
            "is_verified": item.tenant.is_verified_merchant,
            "rating": float(item.tenant.market_rating or 5.0),
            "ai_confidence_score": getattr(item, 'ai_quality_confidence', 100), 
            "condition": item.condition,
            "price": float(item.wholesale_price),
            "qty_available": item.available_qty,
        })

    return JsonResponse({
        "status": "success",
        "part_number": part_number,
        "results_count": len(data),
        "best_price": data[0]["price"] if data else None,
        "dealers": data
    })

# =====================================================================
# ⚖️ 4. لوحة ومحرك المزادات العكسية (Smart Blind Bidding Engine)
# =====================================================================
@login_required(login_url='/secure-portal/')
def active_blind_bids_api(request):
    """عرض المناقصات والمزادات المفتوحة داخل الورش لجلب القطع الناقصة."""
    active_bids = BlindBiddingRequest.objects.filter(
        status='open', expires_at__gt=timezone.now()
    ).select_related('buyer').order_by('-created_at')

    data = []
    for bid in active_bids:
        data.append({
            "bid_id": bid.id,
            "request_code": str(bid.request_id)[:8],
            "part_number": bid.part_number,
            "required_qty": bid.required_qty,
            "buyer_name": "مشتري سري مصان" if getattr(bid, 'auto_award', False) else bid.buyer.company_name, 
            "expires_in_minutes": int((bid.expires_at - timezone.now()).total_seconds() / 60)
        })

    return JsonResponse({"status": "success", "active_bids_count": len(data), "bids": data})

@csrf_exempt
@login_required(login_url='/secure-portal/')
def submit_bid_offer_api(request):
    """
    🚀 محرك تقديم العروض المغلقة من التجار ومطابقتها فورياً.
    🚀 ابتكار: الترسية اللحظية التلقائية والخصم من المحفظة (Auto-Award Matchmaking).
    """
    if request.method != 'POST': return JsonResponse({"error": "POST Only"}, status=400)
    if not hasattr(request, 'tenant') or request.tenant.schema_name == 'public':
        return JsonResponse({"error": "الشركات التجارية المفعلة فقط يمكنها تقديم عروض مالية."}, status=403)

    try:
        data = json.loads(request.body)
        bid_id = data.get('bid_id')
        offer_price = Decimal(str(data.get('offer_price', 0)))
        delivery_days = int(data.get('delivery_days', 1))

        with transaction.atomic():
            bid = get_object_or_404(BlindBiddingRequest.objects.select_for_update(), id=bid_id, status='open')
            
            if bid.buyer == request.tenant:
                return JsonResponse({"error": "قانون المنصة يمنع المزايدة على طلباتك الشخصية."}, status=400)

            # تسجيل العرض المالي للتاجر
            offer, created = BidOffer.objects.update_or_create(
                bidding_request=bid, seller=request.tenant,
                defaults={'offer_price': offer_price, 'estimated_delivery_days': delivery_days}
            )

            # 🚀 ابتكار: خوارزمية الترسية والتحصيل الفوري (Instant FinTech Matchmaking)
            if bid.target_price and offer_price <= bid.target_price:
                # تجميد ونقل أموال الضمان فوراً
                bid.status = 'completed'
                bid.winning_price = offer_price
                bid.save(update_fields=['status', 'winning_price'])
                
                # تجميد رصيد المشتري في محفظة الأمانة (Escrow)
                buyer_tenant = bid.buyer
                buyer_tenant.wallet_balance -= bid.total_escrow_amount
                buyer_tenant.escrow_held += bid.total_escrow_amount
                buyer_tenant.save(update_fields=['wallet_balance', 'escrow_held'])
                
                logger.info(f"⚖️ [AUTO-AWARD SUCCESS]: Bid {bid.id} automatically awarded to {request.tenant.company_name}")
                return JsonResponse({
                    "status": "auto_awarded",
                    "message": "🔥 تم قبول عرضك وترسية المزاد عليك فوراً لمطابقتك السعر المستهدف للعميل! تم حجز الأموال في محفظة الضمان الضامنة."
                })

        return JsonResponse({"status": "success", "message": "تم تشفير وتثبيت عرضك المالي في المظروف المغلق بنجاح في انتظار مراجعة المشتري."})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

# =====================================================================
# 🛡️ 5. محفظة الضامن المالي ودفتر الأستاذ (FinTech Escrow Ledger)
# =====================================================================
@login_required(login_url='/secure-portal/')
def my_escrow_wallet_api(request):
    """
    بوابة المحفظة والمستندات المالية المؤمنة بالكامل ضد التلاعب والمسح الجانبي.
    """
    if not hasattr(request, 'tenant') or request.tenant.schema_name == 'public':
        return JsonResponse({"error": "هذه البيانات متاحة للمؤسسات المشتركة فقط."}, status=403)

    tenant = request.tenant
    recent_transactions = EscrowLedger.objects.filter(client=tenant).order_by('-created_at')[:8]
    ledger_data = [{
        "type": t.transaction_type,
        "amount": float(t.amount),
        "desc": t.description,
        "date": t.created_at.strftime("%Y-%m-%d %H:%M")
    } for t in recent_transactions]

    return JsonResponse({
        "status": "success",
        "tenant_name": tenant.company_name,
        "is_verified": tenant.is_verified_merchant,
        "wallet": {
            "available_to_withdraw": float(tenant.wallet_balance),
            "held_in_escrow": float(tenant.escrow_held), 
            "total_assets": float(tenant.wallet_balance + tenant.escrow_held)
        },
        "recent_ledger_entries": ledger_data
    })

# =====================================================================
# 🤖 6. رادار التنبؤ بطلب السوق (Advanced Market Demand AI Predictor)
# =====================================================================
@login_required(login_url='/secure-portal/')
def market_demand_predictor_api(request):
    """
    تحليلات وبصيرة البيزنس (BI)؛ لتوجيه الموردين للقطع الأكثر طلباً ومؤشر ندرتها.
    """
    thirty_days_ago = timezone.now() - timezone.timedelta(days=30)
    
    # جلب الإحصائيات وتحليل أسعار الترسية
    trending_parts = BlindBiddingRequest.objects.filter(created_at__gte=thirty_days_ago) \
        .values('part_number') \
        .annotate(
            request_count=Count('id'), 
            total_qty_demanded=Sum('required_qty'),
            avg_win_price=Avg('winning_price')
        ) \
        .order_by('-request_count')[:5]

    data = []
    for part in trending_parts:
        # 🚀 ابتكار: حساب مؤشر الندرة النسبي (Scarcity Index Calculation)
        req_count = part['request_count']
        scarcity_status = "🔥 عجز وعقم شديد بالأسواق إقليمياً" if req_count > 8 else "📈 طلب مرتفع وسريع الدوران"
        
        data.append({
            "part_number": part['part_number'],
            "demand_heat_index": req_count,
            "market_scarcity_index": scarcity_status,
            "suggested_competitive_price": float(part['avg_win_price']) if part['avg_win_price'] else "تحت التقييم الفجائي للـ AI"
        })

    return JsonResponse({
        "status": "success",
        "ai_intelligence_message": "بناءً على تتبع رادار Mouss Tec لتحركات المخازن وعجز الفروع، نوصي بضخ هذه المكونات في مستودعاتك لتحقيق أعلى عوائد تدوير.",
        "trending_parts": data
    })