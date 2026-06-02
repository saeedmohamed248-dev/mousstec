"""
🎨 Premium AI Printing Copilot — Views & API
=====================================================================
Endpoints:
  POST /printing-copilot/api/generate/    → refine + generate image
  POST /printing-copilot/api/send-to-print/ → convert generated image to DesignPrintRequest

كل API ملفوف بـ try/except. الـ errors بترجع 200 + رسالة عربية أنيقة عشان
الـ UI يعرضها بدل ما يكسر بـ 500.
"""
from __future__ import annotations

import json
import logging

from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .printing_copilot import run_copilot_pipeline
from .credits import (
    get_tenant_balance, get_customer_balance,
    consume_tenant_credit, consume_customer_credit,
    create_tenant_topup,
)
from .credit_packages import CUSTOMER_TOPUPS, TENANT_TOPUPS, get_topup_by_slug

logger = logging.getLogger('mouss_tec_core')

# Whitelist للـ categories اللي السيستم بيتعامل معها (مرتبطة بـ CustomerDesign.DESIGN_CATEGORIES)
_ALLOWED_CATEGORIES = {
    'logo', 'business_card', 'letterhead', 'stamp', 'social_post', 'story',
    'cover', 'flyer', 'poster', 'banner', 'sign', 'menu', 'invitation',
    'certificate', 'brochure', 'tshirt', 'mug_design', 'sticker', 'packaging',
    'label', 'thumbnail', 'other',
}

_ALLOWED_AUDIENCES = {'merchant', 'customer'}


# ---------------------------------------------------------------------------
# 1. Generate (Two-Stage: refine → Flux)
# ---------------------------------------------------------------------------
@login_required
@require_POST
def copilot_generate(request):
    """يستقبل brief عربي + category + size، يرد بـ image URL + الـ engineered prompt."""
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'invalid_json'}, status=400)

    brief = (body.get('brief') or '').strip()
    category = (body.get('category') or 'other').strip()
    size = (body.get('size') or '1024x1024').strip()
    audience = (body.get('audience') or 'merchant').strip()

    if not brief:
        return JsonResponse({
            'success': False,
            'error': 'empty_brief',
            'message': 'اكتب وصف للتصميم اللي عاوزه.',
        }, status=400)

    if len(brief) > 1500:
        return JsonResponse({
            'success': False,
            'error': 'brief_too_long',
            'message': 'الوصف طويل جداً — اختصره (1500 حرف كحد أقصى).',
        }, status=400)

    if category not in _ALLOWED_CATEGORIES:
        category = 'other'
    if audience not in _ALLOWED_AUDIENCES:
        audience = 'merchant'

    # ── 💳 Credit pre-check (قبل ما نصرف API call) ──
    tenant = _resolve_tenant_for_request(request, audience)
    customer = _get_marketplace_customer(request) if audience == 'customer' else None

    pre_balance, gate_reason = _check_credit_gate(audience, tenant, customer)
    if gate_reason:
        return JsonResponse({
            'success': False,
            'need_topup': True,
            'audience': audience,
            'message': gate_reason,
            'balance': pre_balance,
        }, status=200)

    try:
        result = run_copilot_pipeline(
            arabic_brief=brief,
            category=category,
            size_hint=size,
            audience=audience,
        )
    except Exception as e:
        logger.exception('[COPILOT VIEW] pipeline crashed')
        return JsonResponse({
            'success': False,
            'message': '⚠️ مولد الصور مش متاح دلوقتي — جرب تاني بعد لحظات.',
            'error': str(e),
        }, status=200)

    if not result.get('success'):
        # رسالة أنيقة بناءً على نوع الخطأ
        err = result.get('error', '')
        if 'key_missing' in err or 'token_missing' in err:
            msg = '🔑 خدمة توليد الصور لسه مش مفعّلة — كلّم الإدارة.'
        elif 'timeout' in err:
            msg = '⏱️ التوليد ياخد وقت أطول من المتوقع — جرب تاني.'
        elif 'http' in err:
            msg = '🌐 مزود توليد الصور رد بخطأ — جرب تاني خلال ثواني.'
        else:
            msg = '⚠️ مقدرناش نولّد الصورة دلوقتي — جرب تصيغ الوصف بشكل تاني.'

        result['message'] = msg
        return JsonResponse(result, status=200)

    # ✅ Success — اخصم credit + احفظ في الـ history
    try:
        consume_result = _consume_credit_after_success(audience, tenant, customer, {
            'category': category,
            'size': result.get('size'),
            'model': result.get('model'),
        })
        result['credit'] = consume_result
        if consume_result and consume_result.get('balance'):
            result['balance'] = consume_result['balance']
    except Exception as e:
        logger.warning(f'[COPILOT VIEW] credit consume failed (non-fatal): {e}')

    try:
        _persist_session(request, result)
    except Exception as e:
        logger.warning(f'[COPILOT VIEW] persist failed (non-fatal): {e}')

    return JsonResponse(result)


# ---------------------------------------------------------------------------
# Credit helpers
# ---------------------------------------------------------------------------
def _resolve_tenant_for_request(request, audience: str):
    """يجيب الـ tenant object للطلب الحالي (لو audience=merchant)."""
    if audience != 'merchant':
        return None
    try:
        from django.db import connection
        from clients.models import Client
        schema = getattr(connection, 'schema_name', 'public')
        if schema == 'public':
            # Merchant بيستخدم الـ tool من الـ admin على public — نحتاج tenant من user
            # أو نخلي الـ merchant ميقدرش يولّد من public schema
            return None
        return Client.objects.filter(schema_name=schema).first()
    except Exception:
        return None


def _check_credit_gate(audience: str, tenant, customer) -> tuple[dict, str | None]:
    """يفحص لو الـ user عنده رصيد قبل التوليد. يرجع (balance, reason_message_or_None)."""
    if audience == 'merchant':
        if not tenant:
            return ({}, '⚠️ مش قادر أحدد شركتك. سجل دخول من حساب الشركة الأول.')
        bal = get_tenant_balance(tenant)
        if bal.get('total', 0) <= 0:
            return (bal, '💳 رصيد التصاميم خلص. اشحن باقتك من قسم "شحن التصاميم".')
        return (bal, None)
    else:
        if not customer:
            return ({}, '⚠️ لازم تكون مسجل دخول كعميل عشان تولّد تصميم.')
        bal = get_customer_balance(customer)
        if bal.get('total', 0) <= 0:
            return (bal, '💳 رصيد التصاميم خلص. اشحن باقتك من المتجر.')
        return (bal, None)


def _consume_credit_after_success(audience: str, tenant, customer, metadata: dict) -> dict:
    """يخصم credit بعد توليد ناجح."""
    if audience == 'merchant' and tenant:
        return consume_tenant_credit(tenant, metadata)
    if audience == 'customer' and customer:
        return consume_customer_credit(customer, metadata)
    return {'success': False, 'reason': 'no_actor'}


# ---------------------------------------------------------------------------
# 2. Send to Print (تحويل التصميم لـ DesignPrintRequest)
# ---------------------------------------------------------------------------
@login_required
@require_POST
def copilot_send_to_print(request):
    """
    يحوّل image URL + التفاصيل لـ CustomerDesign + DesignPrintRequest.

    Body:
        image_url (str): الصورة المولّدة
        engineered_prompt (str)
        category (str)
        product_type (str): نوع المنتج للطباعة
        quantity (int)
        notes (str): تعليمات إضافية للمطبعة
        delivery_address (str), delivery_phone (str)
        customer_id (int, اختياري): للـ Merchant فقط — يحدد لأي عميل marketplace
                                    يتسجل الطلب. الـ Customer العادي بياخدها من الـ session.
    """
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'invalid_json'}, status=400)

    image_url = (body.get('image_url') or '').strip()
    if not image_url or not (image_url.startswith('http://') or image_url.startswith('https://')):
        return JsonResponse({
            'success': False,
            'message': 'الصورة مش جاهزة — ولّد التصميم الأول.',
        }, status=400)

    # ── Customer resolution ──
    # 1. لو Merchant/Admin: يقدر يبعت customer_id صريح (اختياري)
    # 2. لو Customer عادي: ناخد من الـ session زي ما هو
    is_merchant = request.user.is_authenticated and (
        request.user.is_staff or request.user.is_superuser
    )
    explicit_customer_id = body.get('customer_id')

    customer = None
    if is_merchant and explicit_customer_id:
        customer = _resolve_customer_by_id(explicit_customer_id)
        if not customer:
            return JsonResponse({
                'success': False,
                'message': 'العميل المختار مش موجود — اختار من القائمة.',
                'error': 'customer_not_found',
            }, status=404)
    else:
        customer = _get_marketplace_customer(request)

    if not customer:
        # Merchant من غير ما اختار عميل
        if is_merchant:
            return JsonResponse({
                'success': False,
                'message': '⚠️ اختار العميل من القائمة الأول عشان نسجل الطلب باسمه.',
                'error': 'merchant_must_select_customer',
            }, status=400)
        return JsonResponse({
            'success': False,
            'message': 'لازم تكون مسجل دخول كعميل عشان تطلب طباعة.',
            'error': 'no_marketplace_customer',
        }, status=403)

    try:
        from clients.models import CustomerDesign, DesignPrintRequest

        design = CustomerDesign.objects.create(
            customer=customer,
            is_free_trial=True,
            title=(body.get('title') or 'تصميم AI')[:200],
            description=(body.get('brief') or '')[:1000],
            category=(body.get('category') or 'other'),
            raw_input=(body.get('brief') or '')[:2000],
            engineered_prompt=(body.get('engineered_prompt') or '')[:2000],
            negative_prompt=(body.get('negative_prompt') or '')[:1000],
            image_url=image_url[:600],
            model_used=(body.get('model') or 'flux-schnell')[:50],
            size_preset=(body.get('size') or 'custom'),
        )

        product_type = (body.get('product_type') or 'other').strip()
        valid_product_types = dict(DesignPrintRequest.PRODUCT_TYPE_CHOICES).keys()
        if product_type not in valid_product_types:
            product_type = 'other'

        try:
            quantity = max(1, min(100000, int(body.get('quantity', 1))))
        except (TypeError, ValueError):
            quantity = 1

        print_req = DesignPrintRequest.objects.create(
            design=design,
            customer=customer,
            product_type=product_type,
            quantity=quantity,
            notes=(body.get('notes') or '')[:2000],
            delivery_address=(body.get('delivery_address') or '')[:500],
            delivery_phone=(body.get('delivery_phone') or '')[:20],
            status='pending',
        )

        return JsonResponse({
            'success': True,
            'message': '✅ طلب الطباعة اتسجل! هنرد عليك بعرض السعر قريباً.',
            'print_request_code': str(print_req.request_code),
            'design_id': design.id,
        })
    except Exception as e:
        logger.exception('[COPILOT VIEW] send-to-print failed')
        return JsonResponse({
            'success': False,
            'message': '⚠️ تعذر تسجيل طلب الطباعة — جرب تاني.',
            'error': str(e),
        }, status=200)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _persist_session(request, result: dict):
    """يحفظ التوليد في AIStudioSession (لو موجود)."""
    try:
        from clients.models import AIStudioSession
        AIStudioSession.objects.create(
            user_id=getattr(request.user, 'id', None),
            schema_name=getattr(connection, 'schema_name', 'public'),
            prompt=result.get('engineered_prompt', '')[:2000],
            image_url=result.get('image_url', '')[:600],
            model_used=result.get('model', '')[:50],
        )
    except Exception:
        # Model schema might differ — non-fatal
        pass


def _get_marketplace_customer(request):
    """يجيب MarketplaceCustomer المرتبط بالـ marketplace cookie.

    الـ marketplace customers بيستخدموا cookie اسمها mp_session فيها الـ
    session_token (UUID). مش Django session — فـ request.user / login_required
    مش بيشتغلوا معاهم. هنا بنحاكي نفس logic _marketplace_auth في
    clients/views/_shared.py.

    Fallback اختياري: لو الكوكي مش موجودة بنشوف Django session كـ legacy.
    """
    try:
        from clients.models import MarketplaceCustomer
        token = request.COOKIES.get('mp_session')
        if token:
            return MarketplaceCustomer.objects.filter(
                session_token=token, is_verified=True, is_blocked=False,
            ).first()
        # Legacy fallback (Django session) — للحالات القديمة لو فيه
        cust_id = request.session.get('marketplace_customer_id')
        if cust_id:
            return MarketplaceCustomer.objects.filter(id=cust_id).first()
    except Exception:
        return None
    return None


def _resolve_customer_by_id(raw_id):
    """يجيب MarketplaceCustomer بالـ ID مع validation. يرجع None لو غير صالح."""
    try:
        cid = int(raw_id)
    except (TypeError, ValueError):
        return None
    try:
        from clients.models import MarketplaceCustomer
        return MarketplaceCustomer.objects.filter(id=cid).first()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 3. Merchant Customer Search (autocomplete للـ dropdown في الـ admin)
# ---------------------------------------------------------------------------
@login_required
def copilot_customer_search(request):
    """
    Autocomplete للـ MarketplaceCustomer — للـ merchants بس.
    GET /printing-copilot/api/customer-search/?q=<query>

    يرجع أول 15 نتيجة matching اسم/شركة/موبايل.
    """
    # Merchants only — مش معنى الـ endpoint ده يكون مفتوح للعملاء العاديين
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({'success': False, 'error': 'forbidden'}, status=403)

    q = (request.GET.get('q') or '').strip()
    if len(q) < 2:
        return JsonResponse({'success': True, 'results': []})

    try:
        from django.db.models import Q
        from clients.models import MarketplaceCustomer
        qs = MarketplaceCustomer.objects.filter(
            Q(full_name__icontains=q)
            | Q(company_name__icontains=q)
            | Q(phone__icontains=q)
        ).order_by('-last_login_at', '-id')[:15]

        results = [{
            'id': c.id,
            'name': c.full_name or c.company_name or 'بدون اسم',
            'company': c.company_name or '',
            'phone': c.phone or '',
            'sector': c.sector,
            'label': _format_customer_label(c),
        } for c in qs]

        return JsonResponse({'success': True, 'results': results})
    except Exception as e:
        logger.exception('[COPILOT VIEW] customer search failed')
        return JsonResponse({'success': False, 'error': str(e)}, status=200)


def _format_customer_label(c) -> str:
    """نص العرض في الـ dropdown: 'الاسم — الشركة — الموبايل'."""
    bits = [c.full_name or 'عميل']
    if c.company_name:
        bits.append(c.company_name)
    if c.phone:
        bits.append(c.phone)
    return ' — '.join(bits)


# ---------------------------------------------------------------------------
# 4. Balance API (للـ UI يعرض الرصيد المتاح live)
# ---------------------------------------------------------------------------
# ⚠️ مفيش @login_required — الـ marketplace customers بيستخدموا mp_session
# cookie مش Django auth. الـ branching جوه بيتحقق من الـ audience.
def copilot_balance(request):
    """يرجع رصيد التصاميم للمستخدم الحالي.

    Query: ?audience=merchant|customer (default: merchant)
    """
    audience = (request.GET.get('audience') or 'merchant').strip()
    if audience not in _ALLOWED_AUDIENCES:
        return JsonResponse({'success': False, 'error': 'invalid_audience'}, status=400)

    try:
        if audience == 'merchant':
            tenant = _resolve_tenant_for_request(request, 'merchant')
            if not tenant:
                return JsonResponse({
                    'success': False,
                    'message': 'مش قادر أحدد الشركة.',
                    'balance': {'total': 0},
                })
            bal = get_tenant_balance(tenant)
        else:
            customer = _get_marketplace_customer(request)
            if not customer:
                return JsonResponse({
                    'success': False,
                    'message': 'لازم تكون مسجل دخول كعميل.',
                    'balance': {'total': 0},
                })
            bal = get_customer_balance(customer)

        return JsonResponse({'success': True, 'audience': audience, 'balance': bal})
    except Exception as e:
        logger.exception('[COPILOT BALANCE] failed')
        return JsonResponse({'success': False, 'error': str(e), 'balance': {'total': 0}}, status=200)


# ---------------------------------------------------------------------------
# 5. Top-up Packages Catalog (للـ storefront — public)
# ---------------------------------------------------------------------------
def copilot_topup_catalog(request):
    """يرجع قائمة باقات الشحن المتاحة. ?audience=merchant|customer"""
    audience = (request.GET.get('audience') or 'merchant').strip()
    catalog = TENANT_TOPUPS if audience == 'merchant' else CUSTOMER_TOPUPS
    return JsonResponse({
        'success': True,
        'audience': audience,
        'packages': [{
            'slug': p['slug'],
            'name': p['name'],
            'designs': p['designs'],
            'price_egp': float(p['price']),
            'price_per_design': float(p['price']) / p['designs'],
            'badge': p.get('badge', ''),
        } for p in catalog],
    })


# ---------------------------------------------------------------------------
# 6. Purchase a Top-up (creates pending purchase + return checkout link)
# ---------------------------------------------------------------------------
# ⚠️ مفيش @login_required — marketplace customer flow بيستخدم mp_session.
# الـ branching جوه بيتأكد من tenant context (merchant) أو cookie (customer).
@require_POST
def copilot_topup_purchase(request):
    """
    يبدأ شراء top-up. بيخلق record بـ status=pending وبيرجع checkout URL.

    Body: {package_slug, audience, payment_method}
    """
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'invalid_json'}, status=400)

    slug = (body.get('package_slug') or '').strip()
    audience = (body.get('audience') or 'merchant').strip()
    payment_method = (body.get('payment_method') or 'paymob').strip()

    if audience not in _ALLOWED_AUDIENCES:
        return JsonResponse({'success': False, 'error': 'invalid_audience'}, status=400)

    pkg = get_topup_by_slug(slug, audience)
    if not pkg:
        return JsonResponse({
            'success': False,
            'message': 'الباقة المختارة غير متاحة.',
            'error': 'package_not_found',
        }, status=404)

    try:
        if audience == 'merchant':
            tenant = _resolve_tenant_for_request(request, 'merchant')
            if not tenant:
                return JsonResponse({
                    'success': False,
                    'message': 'مش قادر أحدد الشركة.',
                }, status=400)
            topup = create_tenant_topup(
                tenant=tenant,
                designs=pkg['designs'],
                price=pkg['price'],
                payment_method=payment_method,
                mark_paid=False,
            )
            return JsonResponse({
                'success': True,
                'purchase_code': str(topup.purchase_code),
                'designs': pkg['designs'],
                'price_egp': float(pkg['price']),
                'message': 'تم تجهيز طلب الشراء. كمل الدفع لتفعيل الرصيد.',
                # checkout_url هيتولّد من Paymob layer لو متفعّل
                'checkout_url': f'/payment/paymob/checkout/?topup_id={topup.id}',
            })
        else:
            # Customer purchase — نستخدم DesignPurchase موديل الموجود
            from clients.models import DesignPurchase
            customer = _get_marketplace_customer(request)
            if not customer:
                return JsonResponse({
                    'success': False,
                    'message': 'سجل دخول كعميل الأول.',
                }, status=403)

            # نحتاج DesignPackage matching الـ slug. لو مش موجود نخلق inline.
            from clients.models import DesignPackage
            pkg_obj = DesignPackage.objects.filter(slug=slug).first()
            if not pkg_obj:
                pkg_obj = DesignPackage.objects.create(
                    slug=slug,
                    target_audience='customer',
                    name_ar=pkg['name'],
                    designs_count=pkg['designs'],
                    price_egp=pkg['price'],
                    is_active=True,
                )
            purchase = DesignPurchase.objects.create(
                customer=customer,
                package=pkg_obj,
                designs_total=pkg['designs'],
                designs_used=0,
                price_paid=pkg['price'],
                payment_method=payment_method,
                status='pending',
            )
            return JsonResponse({
                'success': True,
                'purchase_code': str(purchase.purchase_code),
                'designs': pkg['designs'],
                'price_egp': float(pkg['price']),
                'message': 'تم تجهيز طلب الشراء. كمل الدفع لتفعيل الرصيد.',
                'checkout_url': f'/payment/paymob/checkout/?purchase_id={purchase.id}',
            })
    except Exception as e:
        logger.exception('[COPILOT TOPUP] purchase failed')
        return JsonResponse({
            'success': False,
            'message': '⚠️ تعذر بدء عملية الشراء — جرب تاني.',
            'error': str(e),
        }, status=200)
