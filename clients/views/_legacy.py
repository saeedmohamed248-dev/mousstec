from django.http import JsonResponse, Http404, HttpResponse
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

from ._shared import (
    _is_platform_owner,
    _marketplace_auth,
    _send_otp_via_channel,
    _notify_merchants_of_new_request,
    _landing_bot_local_reply,
    _build_customer_topup_cards,
)
from .webhook_views import universal_webhook_multiplexer
from .b2b_views import (
    b2b_market_search_api,
    active_blind_bids_api,
    submit_bid_offer_api,
    my_escrow_wallet_api,
    market_demand_predictor_api,
)
from .auth_views import (
    register_new_tenant_saas,
    smart_post_login_redirect,
    client_login_finder,
    mousstec_landing_page,
    automotive_landing_page,
    printing_landing_page,
    account_recovery,
)
from .subscription_views import (
    saas_pricing_page,
    paymob_checkout,
    paymob_callback,
    manage_subscription,
    purchase_addon_api,
    features_page,
)
from .admin_views import (
    super_admin_dashboard,
    super_admin_customer_detail,
    super_admin_tenant_grants,
    enter_tenant,
    impersonate_login,
)



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



# =====================================================================
# 🛍️ سوق العملاء والمناقصات المجهولة (Customer Marketplace)
# =====================================================================

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
    password = data.get('password', '')
    job_title = data.get('job_title', '').strip()
    sector = data.get('sector', 'automotive')
    city = data.get('city', '').strip()
    email = data.get('email', '').strip()

    if not full_name or not phone:
        return JsonResponse({"error": "الاسم ورقم الموبايل مطلوبان"}, status=400)

    if not password or len(password) < 6:
        return JsonResponse({"error": "كلمة المرور مطلوبة (٦ حروف على الأقل)"}, status=400)

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
        customer = MarketplaceCustomer(
            customer_type=customer_type,
            full_name=full_name,
            company_name=company_name,
            phone=cleaned_phone,
            email=email or None,
            job_title=job_title,
            sector=sector,
            city=city,
            is_verified=True,
            last_login_at=timezone.now(),
        )
        customer.set_password(password)
        customer.save()
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



@csrf_exempt
def marketplace_verify_otp(request):
    """التحقق من كود OTP — معطل (تم إلغاء OTP)."""
    return JsonResponse({"error": "OTP verification is disabled. Use direct login."}, status=410)


@csrf_exempt
def marketplace_login(request):
    """دخول عميل حالي — برقم الموبايل + كلمة المرور."""
    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    # 🛡️ Rate limiting — 8 محاولات/دقيقة/IP لمنع brute force
    client_ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
    rate_key = f'mp_login_rate:{client_ip}'
    cnt = cache.get(rate_key, 0)
    if cnt >= 8:
        return JsonResponse({"error": "محاولات كثيرة. انتظر دقيقة ثم حاول مرة أخرى."}, status=429)
    cache.set(rate_key, cnt + 1, 60)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "بيانات غير صالحة"}, status=400)

    phone = data.get('phone', '').strip()
    password = data.get('password', '')
    if not phone or not password:
        return JsonResponse({"error": "رقم الموبايل وكلمة المرور مطلوبان"}, status=400)

    # Normalize Egyptian phone
    cleaned = phone
    if not phone.startswith('+'):
        digits = phone.lstrip('0')
        if len(digits) == 10 and digits.startswith('1'):
            cleaned = f'+20{digits}'
        elif len(digits) == 11 and digits.startswith('01'):
            cleaned = f'+2{digits}'
        elif len(digits) == 12 and digits.startswith('201'):
            cleaned = f'+{digits}'

    customer = MarketplaceCustomer.objects.filter(phone=cleaned).first()
    if not customer:
        return JsonResponse({"error": "رقم غير مسجل. سجل حساب جديد."}, status=404)

    if customer.is_blocked:
        return JsonResponse({"error": "تم تعليق حسابك. تواصل مع الدعم."}, status=403)

    # 🛡️ يدعم الحسابات القديمة (بدون باسورد) عبر مطابقة الاسم
    if customer.has_usable_password():
        if not customer.check_password(password):
            logger.warning(f"[MARKETPLACE] Login failed — wrong password for {cleaned[:6]}***")
            return JsonResponse({"error": "رقم الموبايل أو كلمة المرور غير صحيحة"}, status=403)
    else:
        # حساب قديم بدون باسورد — أول دخول يضبط الباسورد المُرسَل
        customer.set_password(password)

    customer.is_verified = True
    customer.session_token = uuid.uuid4()
    customer.last_login_at = timezone.now()
    customer.save(update_fields=['is_verified', 'session_token', 'last_login_at', 'password_hash'])
    logger.info(f"[MARKETPLACE] Login OK: {cleaned[:6]}***")
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
            'pending_approval': requests_qs.filter(status='pending_approval').count(),
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
            status='pending_approval',  # ينتظر موافقة الإدارة أولاً
            is_approved=False,
            expires_at=timezone.now() + timedelta(days=days),
        )
    except Exception as e:
        logger.error(f"[MARKETPLACE] Failed to create request for {customer.phone}: {e}")
        return JsonResponse({"error": "فشل إنشاء الطلب. حاول مرة أخرى."}, status=500)

    # Handle attachments
    if request.FILES.get('attachment_1'):
        svc_request.attachment_1 = request.FILES['attachment_1']
    if request.FILES.get('attachment_2'):
        svc_request.attachment_2 = request.FILES['attachment_2']
    if request.FILES.get('attachment_1') or request.FILES.get('attachment_2'):
        svc_request.save()

    # Update stats
    MarketplaceCustomer.objects.filter(pk=customer.pk).update(total_requests=F('total_requests') + 1)

    # 🔔 لا نرسل إشعارات للتجار — الطلب ينتظر موافقة الإدارة أولاً

    return JsonResponse({
        "status": "success",
        "message": "تم إرسال طلبك بنجاح! سيتم مراجعته من الإدارة قبل عرضه للتجار.",
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
        qs = qs.filter(status='open', is_approved=True, expires_at__gt=timezone.now())

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

    svc_request = get_object_or_404(ServiceRequest, request_code=request_code, status='open', is_approved=True)

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
            status='pending_approval',
            is_approved=False,
            expires_at=timezone.now() + timedelta(days=days),
        )
    except Exception as e:
        logger.error(f"[B2B REQUEST] Failed for {tenant.name}: {e}")
        return JsonResponse({"error": "فشل إنشاء الطلب"}, status=500)

    # 🔔 لا نرسل إشعارات — ينتظر موافقة الإدارة أولاً

    return JsonResponse({
        "status": "success",
        "message": "تم إرسال طلبك! سيتم مراجعته من الإدارة قبل عرضه للتجار.",
        "request_code": str(svc_request.request_code),
    })


# ------------------------------------------------------------------
# ✅ Super Admin: Approve / Reject marketplace requests
# ------------------------------------------------------------------

@csrf_exempt
@login_required
@user_passes_test(lambda u: u.is_superuser)
def marketplace_admin_approve(request, request_id):
    """موافقة سوبر أدمن على طلب سوق."""
    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    svc_request = get_object_or_404(ServiceRequest, pk=request_id)
    if svc_request.status != 'pending_approval':
        return JsonResponse({"error": "الطلب ليس في انتظار الموافقة"}, status=400)

    # Approve and make it live
    svc_request.status = 'open'
    svc_request.is_approved = True
    # Reset expiry from now (since it was waiting for approval)
    expiry_map = {'urgent': 1, 'soon': 3, 'normal': 7}
    days = expiry_map.get(svc_request.urgency, 7)
    svc_request.expires_at = timezone.now() + timedelta(days=days)
    svc_request.save(update_fields=['status', 'is_approved', 'expires_at'])

    # 🔔 Now notify merchants
    _notify_merchants_of_new_request(svc_request)

    return JsonResponse({"status": "success", "message": "تم الموافقة على الطلب ونشره للتجار."})


@csrf_exempt
@login_required
@user_passes_test(lambda u: u.is_superuser)
def marketplace_admin_reject(request, request_id):
    """رفض سوبر أدمن لطلب سوق."""
    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    svc_request = get_object_or_404(ServiceRequest, pk=request_id)
    if svc_request.status != 'pending_approval':
        return JsonResponse({"error": "الطلب ليس في انتظار الموافقة"}, status=400)

    data = json.loads(request.body) if request.body else {}
    svc_request.status = 'rejected_by_admin'
    svc_request.admin_notes = data.get('reason', '')
    svc_request.save(update_fields=['status', 'admin_notes'])

    return JsonResponse({"status": "success", "message": "تم رفض الطلب."})


# ------------------------------------------------------------------
# ✏️ Customer: Edit their own request (only if still pending or open)
# ------------------------------------------------------------------

@csrf_exempt
def marketplace_edit_request(request, request_code):
    """تعديل طلب من صاحبه (فقط لو لسه pending_approval أو open)."""
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "يجب تسجيل الدخول"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    svc_request = get_object_or_404(ServiceRequest, request_code=request_code, customer=customer)

    if svc_request.status not in ('pending_approval', 'open'):
        return JsonResponse({"error": "لا يمكن تعديل الطلب بعد قبول عرض أو انتهاء الطلب"}, status=400)

    title = request.POST.get('title', '').strip()
    description = request.POST.get('description', '').strip()
    urgency = request.POST.get('urgency', '').strip()

    if title:
        if len(title) < 5:
            return JsonResponse({"error": "العنوان قصير جداً"}, status=400)
        svc_request.title = title[:300]
    if description:
        if len(description) < 10:
            return JsonResponse({"error": "التفاصيل قصيرة جداً"}, status=400)
        svc_request.description = description[:5000]
    if urgency and urgency in ('normal', 'soon', 'urgent'):
        svc_request.urgency = urgency

    # Handle attachment updates
    if request.FILES.get('attachment_1'):
        svc_request.attachment_1 = request.FILES['attachment_1']
    if request.FILES.get('attachment_2'):
        svc_request.attachment_2 = request.FILES['attachment_2']

    # If it was already open (approved), editing sends it back for re-approval
    if svc_request.status == 'open':
        svc_request.status = 'pending_approval'
        svc_request.is_approved = False

    svc_request.save()

    msg = "تم تعديل الطلب بنجاح."
    if svc_request.status == 'pending_approval':
        msg += " سيتم مراجعته مرة أخرى من الإدارة."

    return JsonResponse({"status": "success", "message": msg})


# =====================================================================
# 🎨 AI Designs Store — متجر التصاميم الفورية
# =====================================================================

def design_store_home(request):
    """🛍️ صفحة المتجر — يعرض الباقات (عملاء + مصممين).

    🆕 باقات العملاء الآن مصدرها CUSTOMER_TOPUPS catalog مباشرةً (50/100/500)،
    مش الـ DB القديمة (cust_2/4/8 العتيقة). الفلسفة:
    الـ catalog هو single source of truth، والـ DB بتسجّل المشتريات فقط.
    """
    customer_packages = _build_customer_topup_cards()
    designer_packages = DesignPackage.objects.filter(
        is_active=True, target_audience='designer',
    ).order_by('sort_order', 'designs_count')

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

            # 🛡️ تحقق من قيم الإعدادات (أرقام صحيحة)
            try:
                integration_id_int = int(paymob_integration_id)
            except (TypeError, ValueError):
                logger.error(f"[PAYMOB/DESIGN] PAYMOB_INTEGRATION_ID غير رقمي: {paymob_integration_id!r}")
                return JsonResponse({"error": "إعدادات بوابة الدفع غير صحيحة. تواصل مع الدعم."}, status=503)
            if not paymob_iframe_id:
                logger.error("[PAYMOB/DESIGN] PAYMOB_IFRAME_ID غير مضبوط")
                return JsonResponse({"error": "إعدادات بوابة الدفع غير مكتملة. تواصل مع الدعم."}, status=503)

            # Step 1: Auth
            auth_res = http_requests.post('https://accept.paymob.com/api/auth/tokens',
                json={'api_key': paymob_api_key}, timeout=15)
            if auth_res.status_code != 201 and auth_res.status_code != 200:
                logger.error(f"[PAYMOB/DESIGN] Auth failed: HTTP {auth_res.status_code} — {auth_res.text[:300]}")
                return JsonResponse({"error": "فشل المصادقة مع بوابة الدفع. حاول لاحقاً."}, status=502)
            auth_token = auth_res.json().get('token')
            if not auth_token:
                logger.error(f"[PAYMOB/DESIGN] Auth returned no token: {auth_res.text[:300]}")
                return JsonResponse({"error": "بوابة الدفع لم ترسل رمز المصادقة. حاول لاحقاً."}, status=502)

            # Step 2: Order
            amount_cents = int(float(package.price_egp) * 100)
            merchant_order_id = f'design_{purchase.pk}_{uuid.uuid4().hex[:8]}'
            order_res = http_requests.post('https://accept.paymob.com/api/ecommerce/orders', json={
                'auth_token': auth_token,
                'delivery_needed': 'false',
                'amount_cents': amount_cents,
                'currency': 'EGP',
                'items': [{'name': f'باقة {package.name_ar}', 'amount_cents': amount_cents, 'quantity': '1'}],
                'merchant_order_id': merchant_order_id,
            }, timeout=15)
            if order_res.status_code not in (200, 201):
                logger.error(f"[PAYMOB/DESIGN] Order failed: HTTP {order_res.status_code} — {order_res.text[:300]}")
                return JsonResponse({"error": "فشل إنشاء طلب الدفع. حاول لاحقاً."}, status=502)
            order_id = order_res.json().get('id')
            if not order_id:
                logger.error(f"[PAYMOB/DESIGN] Order returned no id: {order_res.text[:300]}")
                return JsonResponse({"error": "بوابة الدفع لم ترسل رقم الطلب. حاول لاحقاً."}, status=502)

            # Step 3: Payment key
            # 🌐 callback مرتبط بدومين الموقع (يفضل التحكم منا بدل dashboard Paymob)
            base_url = f"{'https' if request.is_secure() else 'http'}://{request.get_host()}"
            billing = {
                'first_name': (customer.full_name or 'Customer').split()[0][:50] or 'Customer',
                'last_name': 'Design',
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
                'integration_id': integration_id_int,
                'lock_order_when_paid': 'true',
            }, timeout=15)
            if key_res.status_code not in (200, 201):
                logger.error(f"[PAYMOB/DESIGN] Payment key failed: HTTP {key_res.status_code} — {key_res.text[:300]}")
                return JsonResponse({"error": "فشل إصدار رمز الدفع. حاول لاحقاً."}, status=502)
            payment_token = key_res.json().get('token')
            if not payment_token:
                logger.error(f"[PAYMOB/DESIGN] Payment key returned no token: {key_res.text[:300]}")
                return JsonResponse({"error": "بوابة الدفع لم ترسل رمز الدفع. حاول لاحقاً."}, status=502)

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
        except http_requests.Timeout:
            logger.error("[PAYMOB/DESIGN] Paymob timeout")
            return JsonResponse({"error": "بوابة الدفع لا تستجيب. حاول لاحقاً."}, status=504)
        except http_requests.RequestException as e:
            logger.error(f"[PAYMOB/DESIGN] Network error: {e}")
            return JsonResponse({"error": "خطأ في الاتصال ببوابة الدفع. تحقق من الإنترنت."}, status=502)
        except Exception as e:
            logger.exception(f"[PAYMOB/DESIGN] Unexpected error: {e}")
            return JsonResponse({"error": f"خطأ غير متوقع: {type(e).__name__}. تواصل مع الدعم."}, status=500)


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

    purchases = customer.design_purchases.filter(
        status__in=['paid', 'exhausted']
    ).select_related('package').order_by('-created_at')
    designs = list(customer.designs.order_by('-created_at')[:50])
    active_purchase = next((p for p in purchases if p.is_usable), None)
    paid_remaining = sum(p.designs_remaining for p in purchases if p.is_usable)
    free_remaining = customer.free_designs_remaining

    # إضافة معلومات إعادة التوليد لكل تصميم
    for d in designs:
        d.regen_left = max(d.regenerations_allowed - d.regenerations_used, 0)

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
                "error": "رصيدك خلص! اشتري باقة جديدة علشان تكمل تصميمات.",
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
        'logo': '1024x1024', 'stamp': '1024x1024',
        'business_card': '1536x1024', 'letterhead': '1024x1536',
        'social_post': '1024x1024', 'story': '1024x1536', 'cover': '1536x1024',
        'flyer': '1024x1536', 'poster': '1024x1536', 'brochure': '1024x1536',
        'banner': '1536x1024', 'sign': '1536x1024',
        'certificate': '1536x1024', 'receipt_form': '1024x1536',
        'tshirt': '1024x1536', 'pants': '1024x1536', 'abaya': '1024x1536',
        'uniform': '1024x1536', 'cap': '1024x1024', 'bag': '1024x1024',
        'shoe': '1024x1024', 'full_body': '1024x1536',
        'mug': '1536x1024', 'mug_design': '1536x1024',
        'sticker': '1024x1024', 'label': '1024x1024',
        'packaging': '1024x1024', 'mockup': '1024x1024',
        'film_poster': '1024x1536', 'book_cover': '1024x1536',
        'album_cover': '1024x1024', 'thumbnail': '1536x1024',
        'pattern': '1024x1024', 'illustration': '1024x1024',
        'infographic': '1024x1536', 'car_wrap': '1536x1024',
        'menu': '1024x1536', 'invitation': '1024x1536',
    }

    # Map user presets → canonical size
    size_map = {
        '1024x1024': '1024x1024', '1024x1536': '1024x1536', '1536x1024': '1536x1024',
        '1024x1792': '1024x1792', '1792x1024': '1792x1024',
        '2048x2048': '1024x1024',
        # مطبوعات
        'a4': '1024x1536', 'a3': '1024x1536', 'a5': '1024x1536',
        'business_card': '1536x1024',
        # يافطات
        'banner_wide': '1536x1024', 'rollup': '1024x1536',
        'sign_square': '1024x1024', 'sign_landscape': '1536x1024',
        # ملابس
        'tshirt_chest': '1024x1536', 'tshirt_full': '1024x1536',
        'pants_pattern': '1024x1536', 'abaya_pattern': '1024x1536',
        'full_body': '1024x1536',
        'mug': '1536x1024', 'bag': '1024x1024',
        # أغلفة
        'book_cover': '1024x1536', 'youtube_thumb': '1536x1024',
        'film_poster': '1024x1536',
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
        "ULTRA-IMPORTANT: The design must look like a premium product from a top-tier design "
        "agency — NOT like a template or clip-art. Use professional lighting, shadows, and depth. "
        "Every element must have purpose and visual weight. "
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

        # ── هوية تجارية ──
        'letterhead': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL LETTERHEAD / corporate stationery. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Clean, corporate letterhead with header, footer, and side accents. "
            f"Include space for company logo, address, phone, email. Professional typography. "
            f"STYLE: Modern, minimal, print-ready A4.{dim_info}"
        ),
        'stamp': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a CORPORATE RUBBER STAMP. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a professional stamp design — circular or rectangular. "
            f"Include company name, registration number space, and decorative border. "
            f"STYLE: Traditional stamp aesthetic with clean text, monochrome.{dim_info}"
        ),
        'story': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a vertical SOCIAL MEDIA STORY (9:16 ratio). "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Full-screen mobile-first design. Bold visuals, engaging typography, "
            f"swipe-up ready. Vibrant colors, modern layout.{dim_info}"
        ),
        'cover': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a SOCIAL MEDIA COVER / banner image. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Wide-format cover for Facebook/YouTube/LinkedIn. Professional, branded, "
            f"with clear visual hierarchy. Hero image with text overlay.{dim_info}"
        ),
        'sign': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL SIGNAGE / cladding / outdoor sign. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Large-format sign design readable from distance. Bold brand colors, "
            f"clear company name, high contrast. Include mock lighting effects. "
            f"STYLE: Modern storefront/building signage — LED backlit or cladding finish.{dim_info}"
        ),
        'certificate': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a FORMAL CERTIFICATE / award document. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Elegant certificate with decorative borders, gold/silver accents, "
            f"formal typography. Include fields for name, date, signature line. "
            f"STYLE: Premium, official, printable.{dim_info}"
        ),
        'brochure': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL BROCHURE panel. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Informative layout with sections, images, and call-to-action. "
            f"STYLE: Corporate, clean, print-ready with bleed marks.{dim_info}"
        ),
        'receipt_form': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL BUSINESS FORM / receipt / invoice template. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Clean form with organized fields, lines for handwriting, "
            f"company header, numbered rows, total section. Print-ready. "
            f"STYLE: Professional document design with clear hierarchy.{dim_info}"
        ),

        # ── ملابس ──
        'pants': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design FASHION PANTS / trousers. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a detailed fashion illustration showing pants design from front and back. "
            f"Show fabric pattern, stitching details, pockets, waistband. "
            f"STYLE: Professional fashion sketch / product mockup. Photorealistic rendering.{dim_info}"
        ),
        'abaya': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design an ELEGANT ABAYA / JALABIYA / modest fashion garment. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a beautiful modest fashion design showing the full garment. "
            f"Include embroidery patterns, fabric draping, sleeve details. "
            f"STYLE: High-end fashion illustration. Show flowing fabric, intricate detailing.{dim_info}"
        ),
        'uniform': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL UNIFORM / workwear. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Corporate uniform mockup with branding, name tags, "
            f"functional pockets. Show front view. "
            f"STYLE: Clean product mockup, realistic fabric rendering.{dim_info}"
        ),
        'cap': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a BRANDED CAP / HAT. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Cap product mockup with embroidered/printed logo. "
            f"Show front and side angle. Professional product photography style.{dim_info}"
        ),
        'bag': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a BRANDED BAG / TOTE. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Product mockup of a branded bag with print/embroidery. "
            f"Professional product photography, studio lighting.{dim_info}"
        ),
        'shoe': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design BRANDED FOOTWEAR / SHOES. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Detailed shoe/sneaker design mockup. Show branding, "
            f"materials, sole design. Professional product rendering.{dim_info}"
        ),

        # ── تغليف ──
        'label': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PRODUCT LABEL / sticker label. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Print-ready product label with brand, ingredients area, "
            f"barcode space. Professional typography and layout.{dim_info}"
        ),
        'mug_design': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a MUG print — wraparound graphic. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Create a mug mockup with the design wrapped around it. "
            f"Vibrant colors, high contrast, photorealistic ceramic rendering.{dim_info}"
        ),

        # ── وسائط ──
        'film_poster': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a CINEMATIC MOVIE/SERIES POSTER. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Dramatic composition with key characters/imagery. "
            f"Professional typography, cinematic lighting and color grading. "
            f"STYLE: Hollywood-quality movie poster. Dramatic, compelling.{dim_info}"
        ),
        'book_cover': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL BOOK COVER. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Genre-appropriate cover with compelling imagery and typography. "
            f"STYLE: Bestseller-quality cover design. Clear title placement.{dim_info}"
        ),
        'album_cover': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design an ALBUM COVER art. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Creative, artistic album artwork. Square format. "
            f"STYLE: Visually striking, musical, genre-appropriate.{dim_info}"
        ),
        'thumbnail': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a YOUTUBE THUMBNAIL (16:9). "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Ultra bold text, expressive face/image, high contrast colors. "
            f"Designed to stand out in a feed. Click-worthy, attention-grabbing.{dim_info}"
        ),

        # ── أخرى ──
        'pattern': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Create a SEAMLESS REPEATING PATTERN. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Tileable pattern for fabric/textile/wallpaper. "
            f"Ensure edges match perfectly for seamless tiling.{dim_info}"
        ),
        'illustration': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Create a PROFESSIONAL ILLUSTRATION. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Clean lines, vivid colors, detailed artwork. "
            f"STYLE: Modern digital illustration, professional quality.{dim_info}"
        ),
        'infographic': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a PROFESSIONAL INFOGRAPHIC. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Data visualization with charts, icons, clean layout. "
            f"STYLE: Modern, informative, visually organized.{dim_info}"
        ),
        'car_wrap': (
            f"{QUALITY_FOUNDATION}{ARABIC_LAYER}"
            f"TASK: Design a VEHICLE WRAP / fleet graphics. "
            f"CLIENT BRIEF: {description}. "
            f"EXECUTION: Full vehicle body wrap design with brand identity. "
            f"Show the design on a realistic car/van/truck mockup. "
            f"STYLE: Professional fleet branding, eye-catching on the road.{dim_info}"
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
                        'quality': 'high',  # Always high for best results
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
                        kwargs['quality'] = 'high'  # Always high quality for best results
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

        # Save chat history — initial generation
        try:
            from clients.models import DesignChatMessage
            DesignChatMessage.objects.create(
                design=design, role='user', content=description, image_url=''
            )
            DesignChatMessage.objects.create(
                design=design, role='assistant',
                content=f"تم توليد تصميم: {title or description[:60]}",
                image_url=image_url or ''
            )
        except Exception:
            pass

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

    import io
    from django.core.files.storage import default_storage

    # ── Step 1: Load image bytes ──────────────────────────────────
    img_data = None
    if design.image_url:
        url = design.image_url

        # Try local file first (extract path from URL)
        for prefix in ['/media/', 'media/']:
            if prefix in url:
                rel_path = url.split(prefix, 1)[-1]
                try:
                    if default_storage.exists(rel_path):
                        with default_storage.open(rel_path, 'rb') as f:
                            img_data = f.read()
                        break
                except Exception:
                    pass

        # Fallback: download from URL
        if not img_data:
            try:
                import requests as _req
                r = _req.get(url, timeout=30)
                if r.status_code == 200:
                    img_data = r.content
            except Exception as e:
                logger.error(f"[DOWNLOAD] Failed to fetch image: {e}")

    if not img_data:
        return JsonResponse({"error": "تعذر تحميل الصورة — الملف غير موجود"}, status=404)

    # ── Step 2: For PNG, serve raw (no conversion needed) ─────────
    if fmt == 'png':
        # Even if source is WebP, convert to actual PNG
        try:
            from PIL import Image as PILImage
            img = PILImage.open(io.BytesIO(img_data))
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            img_data = buf.getvalue()
        except Exception:
            pass  # Serve raw bytes if PIL fails
        response = HttpResponse(img_data, content_type='image/png')
        response['Content-Disposition'] = f'attachment; filename="design_{design.design_code}.png"'
        design.download_count += 1
        design.save(update_fields=['download_count'])
        return response

    # ── Step 3: Convert to JPG or PDF ────────────────────────────
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(img_data))
    except Exception as e:
        logger.error(f"[DOWNLOAD] PIL cannot open image: {e}")
        return JsonResponse({"error": "تعذر فتح الصورة للتحويل"}, status=500)

    # Convert to RGB for JPG/PDF (remove alpha channel)
    if img.mode in ('RGBA', 'P', 'LA', 'PA'):
        bg = PILImage.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        try:
            bg.paste(img, mask=img.split()[-1] if 'A' in img.mode else None)
        except Exception:
            bg.paste(img)
        img = bg
    elif img.mode != 'RGB':
        img = img.convert('RGB')

    buf = io.BytesIO()

    if fmt in ('jpg', 'jpeg'):
        img.save(buf, format='JPEG', quality=95)
        response = HttpResponse(buf.getvalue(), content_type='image/jpeg')
        response['Content-Disposition'] = f'attachment; filename="design_{design.design_code}.jpg"'
    elif fmt == 'pdf':
        img.save(buf, format='PDF', resolution=300)
        response = HttpResponse(buf.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="design_{design.design_code}.pdf"'

    design.download_count += 1
    design.save(update_fields=['download_count'])
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
    openai_key = getattr(settings, 'OPENAI_API_KEY', None)
    if not openai_key:
        return JsonResponse({"error": "محرك التوليد غير متاح"}, status=503)

    # Use the engineered prompt (best quality) or fall back to description
    regen_prompt = design.engineered_prompt or design.description
    if not regen_prompt or len(regen_prompt) < 10:
        regen_prompt = f"Create a professional design: {design.title}. {design.description}"

    try:
        import openai
        client = openai.OpenAI(api_key=openai_key)
        gpt_size_map = {
            '1024x1024': '1024x1024', '1024x1536': '1024x1536', '1536x1024': '1536x1024',
            '1024x1792': '1024x1536', '1792x1024': '1536x1024', 'auto': 'auto',
        }
        sz = gpt_size_map.get(design.size_preset, '1024x1024')

        # Try gpt-image-1 → dall-e-3 fallback
        models_to_try = ['gpt-image-1', 'dall-e-3']
        resp = None
        for m in models_to_try:
            try:
                kwargs = {
                    'model': m,
                    'prompt': regen_prompt[:4000],
                    'size': sz if m == 'gpt-image-1' else design.size_preset or '1024x1024',
                    'n': 1,
                }
                if m == 'gpt-image-1':
                    kwargs['quality'] = 'high'
                elif m == 'dall-e-3':
                    kwargs['quality'] = 'hd'
                resp = client.images.generate(**kwargs)
                break
            except Exception as model_err:
                logger.warning(f"[REGEN] Model {m} failed: {model_err}")
                continue

        if not resp:
            return JsonResponse({"error": "فشل إعادة التوليد مع كل المحركات"}, status=502)

        first = resp.data[0]
        new_url = getattr(first, 'url', None)

        # gpt-image-1 returns b64_json, not url
        if not new_url:
            b64 = getattr(first, 'b64_json', None)
            if b64:
                import base64 as _b64
                from django.core.files.base import ContentFile
                from django.core.files.storage import default_storage
                img_bytes = _b64.b64decode(b64)
                filename = f"ai_store/{customer.uid}/regen_{uuid.uuid4().hex}.png"
                saved = default_storage.save(filename, ContentFile(img_bytes))
                local_url = default_storage.url(saved)
                if local_url.startswith('/'):
                    local_url = request.build_absolute_uri(local_url)
                new_url = local_url

        if not new_url:
            return JsonResponse({"error": "لم يتم استلام صورة من المحرك"}, status=502)

        # Persist DALL-E URLs (they expire after ~1 hour)
        if 'oaidalleapiprodscus' in (new_url or ''):
            try:
                import requests as _req
                from django.core.files.base import ContentFile
                from django.core.files.storage import default_storage
                r = _req.get(new_url, timeout=30)
                if r.status_code == 200:
                    filename = f"ai_store/{customer.uid}/regen_{uuid.uuid4().hex}.png"
                    saved = default_storage.save(filename, ContentFile(r.content))
                    local_url = default_storage.url(saved)
                    if local_url.startswith('/'):
                        local_url = request.build_absolute_uri(local_url)
                    new_url = local_url
            except Exception:
                pass

        design.image_url = new_url
        design.save(update_fields=['image_url'])

        # Save chat history for regeneration
        try:
            from clients.models import DesignChatMessage
            DesignChatMessage.objects.create(
                design=design, role='user',
                content='إعادة توليد التصميم بنفس المواصفات'
            )
            DesignChatMessage.objects.create(
                design=design, role='assistant',
                content='تم إعادة توليد التصميم',
                image_url=new_url
            )
        except Exception:
            pass

        return JsonResponse({
            "status": "success",
            "image_url": new_url,
            "regenerations_left": design.regenerations_allowed - design.regenerations_used,
        })
    except Exception as e:
        logger.error(f"[REGEN] Failed: {e}")
        return JsonResponse({"error": f"فشل إعادة التوليد: {str(e)[:100]}"}, status=500)


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


@csrf_exempt
def design_store_chat_history(request, design_code):
    """💬 جلب تاريخ المحادثة لتصميم معين."""
    from clients.models import DesignChatMessage
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "غير مصرح"}, status=401)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)
    messages = design.chat_messages.order_by('created_at').values(
        'role', 'content', 'image_url', 'is_refinement', 'created_at'
    )
    return JsonResponse({
        "status": "success",
        "design_code": str(design.design_code),
        "title": design.title,
        "messages": [
            {
                "role": m['role'],
                "content": m['content'],
                "image_url": m['image_url'],
                "is_refinement": m['is_refinement'],
                "time": m['created_at'].strftime("%d/%m %H:%M"),
            }
            for m in messages
        ],
    })


@csrf_exempt
def design_store_refine(request, design_code):
    """✏️ تعديل تحسيني — العميل يكتب تعليمات إضافية بدون إعادة توليد كامل."""
    from clients.models import DesignChatMessage
    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({"error": "غير مصرح"}, status=401)

    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    design = get_object_or_404(CustomerDesign, design_code=design_code, customer=customer)

    if not design.can_regenerate:
        return JsonResponse({
            "error": f"استنفدت محاولات التعديل ({design.regenerations_allowed} مرات)",
        }, status=403)

    # Rate limiting
    gen_rate_key = f'design_gen_rate:{customer.pk}'
    gen_count = cache.get(gen_rate_key, 0)
    if gen_count >= 5:
        return JsonResponse({"error": "انتظر دقيقة ثم حاول مرة أخرى."}, status=429)
    cache.set(gen_rate_key, gen_count + 1, 60)

    refinement_text = request.POST.get('refinement', '').strip()
    if not refinement_text or len(refinement_text) < 3:
        return JsonResponse({"error": "اكتب التعديل المطلوب (3 أحرف على الأقل)"}, status=400)

    # Save user refinement message
    DesignChatMessage.objects.create(
        design=design, role='user', content=refinement_text, is_refinement=True
    )

    # Build refinement prompt
    base_prompt = design.engineered_prompt or design.description
    refinement_prompt = (
        f"{base_prompt[:2500]} "
        f"REFINEMENT INSTRUCTION: The client wants to modify the existing design. "
        f"Keep everything from the original design but apply these changes: {refinement_text}. "
        f"Maintain the same style, color palette, and composition. "
        f"Only change what the client specifically asked for."
    )

    openai_key = getattr(settings, 'OPENAI_API_KEY', None)
    if not openai_key:
        return JsonResponse({"error": "محرك التوليد غير متاح"}, status=503)

    try:
        import openai
        client_ai = openai.OpenAI(api_key=openai_key)
        gpt_size_map = {
            '1024x1024': '1024x1024', '1024x1536': '1024x1536', '1536x1024': '1536x1024',
            '1024x1792': '1024x1536', '1792x1024': '1536x1024', 'auto': 'auto',
        }
        sz = gpt_size_map.get(design.size_preset, '1024x1024')

        # Try using image edit API (gpt-image-1) with the existing image for true refinement
        resp = None
        used_edit = False

        # Attempt edit with existing image (best refinement quality)
        if design.image_url:
            try:
                import requests as _req, io
                img_resp = _req.get(design.image_url, timeout=15)
                if img_resp.status_code == 200:
                    img_io = io.BytesIO(img_resp.content)
                    img_io.name = 'current.png'
                    resp = client_ai.images.edit(
                        model='gpt-image-1',
                        image=[img_io],
                        prompt=refinement_prompt[:4000],
                        size=sz,
                        n=1,
                        quality='high',
                    )
                    used_edit = True
            except Exception as edit_err:
                logger.warning(f"[REFINE] Edit API failed, falling back to generate: {edit_err}")

        # Fallback: regenerate with refinement prompt
        if not resp:
            models_to_try = ['gpt-image-1', 'dall-e-3']
            for m in models_to_try:
                try:
                    kwargs = {
                        'model': m,
                        'prompt': refinement_prompt[:4000],
                        'size': sz if m == 'gpt-image-1' else design.size_preset or '1024x1024',
                        'n': 1,
                    }
                    if m == 'gpt-image-1':
                        kwargs['quality'] = 'high'
                    elif m == 'dall-e-3':
                        kwargs['quality'] = 'hd'
                    resp = client_ai.images.generate(**kwargs)
                    break
                except Exception as model_err:
                    logger.warning(f"[REFINE] Model {m} failed: {model_err}")
                    continue

        if not resp:
            return JsonResponse({"error": "فشل التعديل مع كل المحركات"}, status=502)

        first = resp.data[0]
        new_url = getattr(first, 'url', None)

        # Handle b64_json response
        if not new_url:
            b64 = getattr(first, 'b64_json', None)
            if b64:
                import base64 as _b64
                from django.core.files.base import ContentFile
                from django.core.files.storage import default_storage
                img_bytes = _b64.b64decode(b64)
                filename = f"ai_store/{customer.uid}/refine_{uuid.uuid4().hex}.png"
                saved = default_storage.save(filename, ContentFile(img_bytes))
                local_url = default_storage.url(saved)
                if local_url.startswith('/'):
                    local_url = request.build_absolute_uri(local_url)
                new_url = local_url

        # Persist DALL-E URLs
        if new_url and 'oaidalleapiprodscus' in new_url:
            try:
                import requests as _req
                from django.core.files.base import ContentFile
                from django.core.files.storage import default_storage
                r = _req.get(new_url, timeout=30)
                if r.status_code == 200:
                    filename = f"ai_store/{customer.uid}/refine_{uuid.uuid4().hex}.png"
                    saved = default_storage.save(filename, ContentFile(r.content))
                    local_url = default_storage.url(saved)
                    if local_url.startswith('/'):
                        local_url = request.build_absolute_uri(local_url)
                    new_url = local_url
            except Exception:
                pass

        if not new_url:
            return JsonResponse({"error": "لم يتم استلام صورة"}, status=502)

        # Update design
        design.image_url = new_url
        design.regenerations_used += 1
        design.engineered_prompt = refinement_prompt[:4000]
        design.save(update_fields=['image_url', 'regenerations_used', 'engineered_prompt'])

        # Save AI response message
        DesignChatMessage.objects.create(
            design=design, role='assistant',
            content=f"تم تعديل التصميم: {refinement_text[:100]}",
            image_url=new_url, is_refinement=True
        )

        return JsonResponse({
            "status": "success",
            "image_url": new_url,
            "regenerations_left": design.regenerations_allowed - design.regenerations_used,
            "used_edit_api": used_edit,
        })
    except Exception as e:
        logger.error(f"[REFINE] Failed: {e}")
        return JsonResponse({"error": f"فشل التعديل: {str(e)[:100]}"}, status=500)
