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

    # ✅ Success — save to AIStudioSession للـ tenant عشان يبقى في الـ history
    try:
        _persist_session(request, result)
    except Exception as e:
        logger.warning(f'[COPILOT VIEW] persist failed (non-fatal): {e}')

    return JsonResponse(result)


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
    """يجيب MarketplaceCustomer المرتبط بالـ session (لو فيه)."""
    try:
        from clients.models import MarketplaceCustomer
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
