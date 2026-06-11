"""
🛍️ Marketplace Core — customer-facing marketplace + blind-bidding endpoints.

Covers: marketplace home/sector landings (automotive/printing), customer
register/verify/login/logout, dashboard, service-request CRUD, offer
accept/rate, merchant feed + submit_offer, admin approve/reject. All
endpoints are public-API surface for ``/marketplace/...`` URLs.

Extracted from ``_legacy.py`` (Step 5 of the incremental split). The
package facade (``clients/views/__init__.py``) preserves the URL surface.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Avg, F
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django_tenants.utils import schema_context

from clients.models import (
    Client,
    MarketplaceCustomer,
    ServiceRequest,
    TenderOffer,
)

from ._shared import (
    _marketplace_auth,
    _notify_merchants_of_new_request,
)

logger = logging.getLogger('mouss_tec_core')


# ───────────────────────────────────────────────────────────────────────────
# Endpoint implementations (preserved verbatim from _legacy.py)
# ───────────────────────────────────────────────────────────────────────────

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
    """صفحة دخول/تسجيل سوق السيارات.

    لو العميل مسجّل بالفعل في القطاع التاني (طباعة)، نعرض الصفحة مع
    notice بدل ما نوديه dashboard صامت — عشان يعرف إنه في سوق تاني وعنده
    خيار يكمل عمل أكاونت ثاني أو يرجع لوحته.
    """
    customer = _marketplace_auth(request)
    if customer and customer.sector == 'automotive':
        return redirect('/marketplace/dashboard/')
    context = {'sector': 'automotive'}
    if customer and customer.sector != 'automotive':
        context['cross_sector_notice'] = (
            "أنت مسجّل حالياً في سوق الطباعة والتصميم. "
            "للدخول لسوق السيارات سجّل حساب جديد، أو ارجع للوحتك."
        )
        context['existing_customer_sector'] = customer.sector
    return render(request, 'clients/marketplace/automotive.html', context)


def marketplace_printing(request):
    """صفحة دخول/تسجيل سوق الطباعة والتصميم.

    لو العميل مسجّل بالفعل في قطاع السيارات، نعرض الصفحة مع notice بدل
    redirect صامت — Issue #4: العميل اللي في سوق سيارات كان بيتحوّل
    للوحة من غير ما يفهم إنه دخل سوق تاني.
    """
    customer = _marketplace_auth(request)
    if customer and customer.sector == 'printing':
        return redirect('/marketplace/dashboard/')
    context = {'sector': 'printing'}
    if customer and customer.sector != 'printing':
        context['cross_sector_notice'] = (
            "أنت مسجّل حالياً في سوق السيارات. "
            "للدخول لسوق الطباعة والتصميم سجّل حساب جديد، أو ارجع للوحتك."
        )
        context['existing_customer_sector'] = customer.sector
    return render(request, 'clients/marketplace/printing.html', context)


@csrf_exempt
def marketplace_register(request):
    """تسجيل عميل جديد في السوق — خطوة 1.

    🐛 [Issue #4 FIX — car owner signup error]:
    1. MarketplaceCustomer لازم يُكتب في schema='public' فقط (الجدول في
       SHARED_APPS، مش موجود في الـ tenant schemas). الـ view ده ممكن
       يتنادى من tenant subdomain فلازم نلف الـ ORM داخل schema_context.
    2. كنا بنخفي الـ Exception الفعلي ونرجع رسالة عامة، فالعميل بيشوف
       "فشل التسجيل" بدون تفاصيل. دلوقتي بنـ log الـ traceback الكامل
       ونرجع كود مميز للعميل (duplicate_phone vs validation_error vs
       internal_error) عشان الـ UI يقدر يوجّه الرسالة بدقة.
    3. التحقق من الـ phone الموجود ينفّذ بعد الـ normalize عشان نفس
       الرقم بأشكال مختلفة (+20 أو 01) ميـ duplicateـش.
    """
    if request.method != 'POST':
        return JsonResponse({"error": "POST only"}, status=405)

    # 🛡️ [Anti-abuse 2026-06-11]: حماية مزدوجة ضد signup farming.
    # كان 3/دقيقة فقط — سهل التحايل بـ proxies. دلوقتي:
    #   - 3/دقيقة (سلوك الـ burst الطبيعي)
    #   - 15/ساعة (يمنع scripting طويل المدى من نفس IP)
    # ملاحظة: 15 مش 5 — في مصر CGNAT و WiFi مقاهي بتخلي عيلة أو
    # مكتب صغير يبانوا من نفس IP. 15 يكفي للسلوك العائلي و يقفل الـ bots.
    # الـ hour check قبل الـ minute عشان لو وصل الساعة، الـ minute counter
    # ميتزدش ظلم على اللي محصلش له تسجيل.
    client_ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()

    reg_hour_key = f'otp_reg_hour:{client_ip}'
    reg_hour_count = cache.get(reg_hour_key, 0)
    if reg_hour_count >= 15:
        return JsonResponse({"error": "تجاوزت الحد المسموح من التسجيلات. جرّب لاحقاً."}, status=429)

    reg_rate_key = f'otp_reg_rate:{client_ip}'
    reg_count = cache.get(reg_rate_key, 0)
    if reg_count >= 3:
        return JsonResponse({"error": "طلبات كثيرة. انتظر دقيقة ثم حاول مرة أخرى."}, status=429)

    cache.set(reg_rate_key, reg_count + 1, 60)
    cache.set(reg_hour_key, reg_hour_count + 1, 3600)

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

    if customer_type not in ('individual', 'company'):
        return JsonResponse({"error": "نوع العميل غير صالح"}, status=400)

    if customer_type == 'company' and not company_name:
        return JsonResponse({"error": "اسم الشركة مطلوب"}, status=400)

    if sector not in ('automotive', 'printing'):
        return JsonResponse({"error": "قطاع غير صالح"}, status=400)

    # Normalize phone — Egyptian format: +20 + 10-digit mobile (starts with 1)
    cleaned_phone = phone
    if not phone.startswith('+'):
        digits = ''.join(c for c in phone if c.isdigit())  # strip dashes/spaces
        digits_no_lead = digits.lstrip('0')
        if len(digits_no_lead) == 10 and digits_no_lead.startswith('1'):
            cleaned_phone = f'+20{digits_no_lead}'
        elif len(digits) == 12 and digits.startswith('201'):
            cleaned_phone = f'+{digits}'
        else:
            cleaned_phone = phone  # keep as-is, fall through to validation

    # ─────────────────────────────────────────────────────────────────────
    # 🐛 [Issue #4 FIX]: المنطق كله لازم يشتغل على schema='public' لأن
    # MarketplaceCustomer جدول مشترك (SHARED_APPS) — لو الـ request جاي من
    # tenant subdomain (والـ search_path بيشمل tenant_schema قبل public)،
    # ممكن يحصل لخبطة على القيود الـ unique أو على signals بتلمس tables
    # في schema غلط. الـ schema_context بيضمن atomicity على public.
    # ─────────────────────────────────────────────────────────────────────
    try:
        with schema_context('public'):
            # تحقق من التكرار بعد الـ normalize
            existing = MarketplaceCustomer.objects.filter(phone=cleaned_phone).first()
            if existing:
                return JsonResponse({
                    "status": "existing",
                    "error": "الرقم مسجل بالفعل. ادخل من تسجيل الدخول.",
                    "message": "الرقم مسجل بالفعل. ادخل من تسجيل الدخول.",
                    "code": "duplicate_phone",
                    "is_existing": True,
                }, status=409)

            customer = MarketplaceCustomer(
                customer_type=customer_type,
                full_name=full_name,
                company_name=company_name,
                phone=cleaned_phone,
                email=(email or None),
                job_title=job_title,
                sector=sector,
                city=city,
                is_verified=True,
                last_login_at=timezone.now(),
            )
            customer.set_password(password)
            customer.save()
            # 🎁 Auto-grant 7-day diagnostics trial for car owners only.
            # Printing customers don't need OBD scans.
            if sector == 'automotive':
                try:
                    from clients.models import CustomerDiagnosticsSubscription
                    CustomerDiagnosticsSubscription.grant_trial(customer)
                except Exception as trial_err:
                    logger.warning(
                        "[MARKETPLACE] trial grant failed for %s: %s",
                        cleaned_phone[:6] + '***', trial_err,
                    )
    except Exception as e:
        # ⚠️ نـ log الـ traceback الكامل عشان نقدر نشخّص بدل ما نخمّن.
        import traceback
        logger.error(
            "[MARKETPLACE REGISTER] failed for phone=%s sector=%s err=%s\n%s",
            cleaned_phone[:6] + '***' if cleaned_phone else '', sector, e,
            traceback.format_exc(),
        )
        err_msg = str(e).lower()
        if 'unique' in err_msg or 'duplicate' in err_msg:
            return JsonResponse({
                "error": "الرقم مسجل بالفعل. ادخل من تسجيل الدخول.",
                "code": "duplicate_phone",
            }, status=409)
        return JsonResponse({
            "error": "فشل التسجيل. حاول مرة أخرى أو تواصل مع الدعم.",
            "code": "internal_error",
            "debug": str(e) if settings.DEBUG else None,
        }, status=500)

    logger.info(f"[MARKETPLACE] New customer {cleaned_phone[:6]}*** registered (sector={sector})")
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

    # Normalize Egyptian phone — same logic as marketplace_register
    cleaned = phone
    if not phone.startswith('+'):
        digits = ''.join(c for c in phone if c.isdigit())
        digits_no_lead = digits.lstrip('0')
        if len(digits_no_lead) == 10 and digits_no_lead.startswith('1'):
            cleaned = f'+20{digits_no_lead}'
        elif len(digits) == 12 and digits.startswith('201'):
            cleaned = f'+{digits}'

    # 🐛 [Issue #4 FIX]: نفس سبب marketplace_register — لازم public schema.
    with schema_context('public'):
        customer = MarketplaceCustomer.objects.filter(phone=cleaned).first()
        if not customer:
            return JsonResponse({"error": "رقم غير مسجل. سجل حساب جديد."}, status=404)

        if customer.is_blocked:
            return JsonResponse({"error": "تم تعليق حسابك. تواصل مع الدعم."}, status=403)
        # 🛡️ Soft-deleted accounts cannot log in — they don't exist from the
        # user's point of view, but their FKs are preserved for audit.
        if getattr(customer, 'is_deleted', False):
            logger.warning(f"[MARKETPLACE] Blocked login on deleted account: {cleaned[:6]}***")
            return JsonResponse({"error": "هذا الحساب غير موجود."}, status=404)

        # 🛡️ يدعم الحسابات القديمة (بدون باسورد)
        if customer.has_usable_password():
            if not customer.check_password(password):
                logger.warning(f"[MARKETPLACE] Login failed — wrong password for {cleaned[:6]}***")
                return JsonResponse({"error": "رقم الموبايل أو كلمة المرور غير صحيحة"}, status=403)
        else:
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

    sector_ui = {
        'automotive': {
            'label': 'سوق السيارات',
            'icon': 'fa-car',
            'color': '#f59e0b',
            'color_2': '#ef4444',
            'switch_url': '/marketplace/automotive/',
            'title_placeholder': 'مثال: محتاج تغيير زيت BMW 320i',
            'desc_placeholder': 'اشرح بالتفصيل: نوع العربية، السنة، الموديل، المشكلة...',
            'images_label': 'أريد صور القطع / الخدمات مع العروض',
            'greeting': 'محتاج إيه لعربيتك النهاردة؟',
            'quick_categories': [
                {'icon': 'fa-oil-can',       'label': 'تغيير زيت',  'q': 'محتاج تغيير زيت'},
                {'icon': 'fa-stethoscope',   'label': 'فحص شامل',   'q': 'محتاج فحص كهرباء وميكانيكا'},
                {'icon': 'fa-cogs',          'label': 'قطع غيار',   'q': 'محتاج قطعة غيار:'},
                {'icon': 'fa-bolt',          'label': 'كهرباء',     'q': 'مشكلة كهرباء في'},
                {'icon': 'fa-car-burst',     'label': 'بدي وفرش',    'q': 'إصلاح بدي / فرش'},
                {'icon': 'fa-circle-notch',  'label': 'إطارات',     'q': 'تغيير إطارات / بنشر'},
            ],
            'tips': [
                'حدد سنة الموديل والـ VIN لو معاك — التجار بيدوا سعر أدق.',
                'فعّل "أريد صور" عشان تشوف القطعة قبل ما تشتري.',
                'الميزانية القصوى مخفية عن التجار — بتفلتر العروض بس.',
            ],
        },
        'printing': {
            'label': 'سوق الطباعة والتصميم',
            'icon': 'fa-palette',
            'color': '#ec4899',
            'color_2': '#8b5cf6',
            'switch_url': '/marketplace/printing/',
            'title_placeholder': 'مثال: محتاج لوجو لمطعم بيتزا',
            'desc_placeholder': 'اشرح بالتفصيل: الألوان، الستايل، المقاسات، الكمية...',
            'images_label': 'أريد تصميمات / صور مع العروض',
            'greeting': 'إيه اللي هنبدع فيه النهاردة؟',
            'quick_categories': [
                {'icon': 'fa-pen-nib',     'label': 'لوجو',         'q': 'محتاج تصميم لوجو لـ'},
                {'icon': 'fa-image',       'label': 'فلاير/بوستر',   'q': 'محتاج فلاير أو بوستر لـ'},
                {'icon': 'fa-id-card',     'label': 'كروت شخصية',    'q': 'محتاج كروت شخصية'},
                {'icon': 'fa-shirt',       'label': 'تيشرت/مج',     'q': 'محتاج طباعة تيشرت / مج'},
                {'icon': 'fa-flag',        'label': 'بنرات/يفط',     'q': 'محتاج بنر / يفطة'},
                {'icon': 'fa-share-nodes', 'label': 'سوشيال ميديا', 'q': 'محتاج تصميمات سوشيال ميديا لـ'},
            ],
            'tips': [
                'لو عندك ألوان أو لوجو موجود — ارفعهم في الـ Brand Profile مرة وعمرها يستخدمهم AI.',
                'متجر تصاميم AI بيوفر تصميم في ثواني — جربه قبل ما تطلب من مصمم.',
                'حدد المقاسات والكمية في التفاصيل عشان السعر يبقى دقيق.',
            ],
        },
    }.get(customer.sector, {
        'label': 'السوق', 'icon': 'fa-store', 'color': '#10b981', 'color_2': '#059669',
        'switch_url': '/marketplace/',
        'title_placeholder': 'عنوان الطلب', 'desc_placeholder': 'تفاصيل الطلب...',
        'images_label': 'أريد صور مع العروض', 'greeting': 'محتاج إيه؟',
        'quick_categories': [], 'tips': [],
    })

    # Sidebar enrichment per sector
    side_widgets = {}
    if customer.sector == 'printing':
        free_remaining = customer.free_designs_remaining
        paid_remaining = sum(p.designs_remaining for p in
                             customer.design_purchases.filter(status='paid')
                             if p.is_usable)
        side_widgets['credits'] = {
            'free': free_remaining,
            'paid': paid_remaining,
            'total': free_remaining + paid_remaining,
        }
        side_widgets['recent_designs'] = list(
            customer.designs.order_by('-created_at')[:6]
        )

    # Smart next action — sector-aware nudge based on data
    next_action = None
    if requests_qs.filter(status='open', offers_count=0).exists():
        cnt = requests_qs.filter(status='open', offers_count=0).count()
        next_action = {
            'icon': 'fa-lightbulb',
            'title': f'عندك {cnt} طلب مفتوح بدون عروض',
            'desc': 'جرب تعدّل العنوان أو تضيف صور — الطلبات المفصّلة بتجيب عروض أكتر.',
            'action_url': '#requests',
            'action_label': 'مراجعة طلباتي',
        }
    elif requests_qs.filter(status='accepted', accepted_offer__isnull=False).exists():
        accepted = requests_qs.filter(status='accepted').first()
        next_action = {
            'icon': 'fa-handshake',
            'title': 'فيه طلب وافقت على عرضه — تواصل مع التاجر',
            'desc': accepted.title,
            'action_url': f'/marketplace/request/{accepted.request_code}/',
            'action_label': 'عرض التفاصيل',
        }
    elif not requests_qs.exists():
        next_action = {
            'icon': 'fa-rocket',
            'title': 'ابدأ أول طلب لك',
            'desc': 'دور على أفضل عرض من ٤٢٠+ تاجر في القطاع — مجاناً.',
            'action_url': '#new-request',
            'action_label': 'اعمل طلب الآن',
        }

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
        'sector_ui': sector_ui,
        'side_widgets': side_widgets,
        'next_action': next_action,
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
