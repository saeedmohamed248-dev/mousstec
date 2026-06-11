"""
🧠 Customer-side Advisor — sector-aware (printing / automotive)
=====================================================================
الـ Advisor للعملاء بيخدم القطاع بتاع العميل بس:

  • printing customer  → Brand & Design Advisor
       يقترح تصاميم بناءً على تاريخك، يحلل اتساق الـ Brand،
       ويعرفك إيه الباقة المناسبة من design store.

  • automotive customer → Vehicle Care Advisor
       يحلل تاريخ التشخيصات بتاعتك، أكواد DTC اللي اتكررت،
       والاشتراك الحالي، ويفكرك بمواعيد الصيانة.

الفروق عن advisor الـ tenant:
  - مفيش function-calling (الـ context صغير، بنحقن كل البيانات في الـ prompt)
  - مفيش tenant data leakage — كل query محصور بـ customer.pk
  - rate-limited per customer (15/min, 250/hr)
"""
from __future__ import annotations

import json
import logging

from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from clients.views._shared import _marketplace_auth

logger = logging.getLogger('mouss_tec_core')

_SESSION_KEY = 'customer_advisor_history_v1'
_MAX_HISTORY = 10  # turns kept in session


# =====================================================================
# Sector-specific context builders
# =====================================================================
def _design_context(customer) -> str:
    """Builds an Arabic snapshot of the customer's design activity."""
    try:
        from clients.models import CustomerDesign, CustomerBrandProfile
    except Exception:
        return 'لا يوجد بيانات تصاميم متاحة.'

    designs = CustomerDesign.objects.filter(customer=customer)
    total = designs.count()
    recent = list(
        designs.order_by('-created_at')[:5]
        .values('title', 'category', 'created_at')
    )
    top_cats = list(
        designs.values('category')
        .annotate(n=Count('id')).order_by('-n')[:3]
    )
    # Brand profile completeness
    bp = getattr(customer, 'brand_profile', None)
    brand_done = bool(bp and getattr(bp, 'logo', None) and getattr(bp, 'primary_color', None))

    parts = [
        f'📊 إجمالي التصاميم المولّدة: {total}',
        f'🎨 Brand Profile: {"مكتمل ✅" if brand_done else "ناقص ⚠️ (مفيش لوجو أو ألوان)"}',
    ]
    if top_cats:
        cat_lbl = '، '.join(f"{c['category']} ({c['n']})" for c in top_cats)
        parts.append(f'🏆 أكتر تصنيفات استخدمتها: {cat_lbl}')
    if recent:
        recent_lbl = '، '.join(r.get('title') or r.get('category') or '-' for r in recent[:3])
        parts.append(f'🕐 آخر تصاميم: {recent_lbl}')

    free_left = max((customer.free_designs_total or 0) - (customer.free_designs_used or 0), 0)
    parts.append(f'🎁 تصاميم مجانية متبقية: {free_left}')

    return '\n'.join(parts)


def _automotive_context(customer) -> str:
    """Builds a snapshot of the customer's diagnostics activity."""
    try:
        from clients.models import CustomerDiagnosticsSubscription
    except Exception:
        return 'لا توجد بيانات تشخيصية متاحة.'

    sub = CustomerDiagnosticsSubscription.objects.filter(customer=customer).first()
    if not sub:
        return (
            '🚗 لسه معندكش اشتراك تشخيص.\n'
            'ابدأ بـ 7 أيام مجاناً من /marketplace/diagnostics/pricing/'
        )

    parts = [
        f'💎 الباقة الحالية: {sub.get_tier_display()}',
        f'📈 سكانات اتعملت لحد دلوقتي: {sub.lifetime_scans}',
        f'🔋 المتبقي في الشهر الحالي: {sub.quota_remaining()}',
        f'⏰ أيام متبقية في الاشتراك: {sub.days_remaining()}',
    ]
    # Most-frequent DTC families across scans, if we track them
    return '\n'.join(parts)


# =====================================================================
# Sector-specific system prompts
# =====================================================================
def _design_system_prompt() -> str:
    return (
        "أنت 'مستشار البراند' لعميل سوق Mouss Tec للتصاميم. "
        "وظيفتك: تساعد العميل (مش وكالة دعاية، شخص عادي عنده بيزنس) يعمل "
        "تصاميم متّسقة مع برانده، ويختار أنسب فئة/باقة لكل احتياج.\n\n"
        "🎯 منهجك:\n"
        " 1. خد سياق التصاميم اللي مولّدها قبل كده، ولو فيه pattern (مثلاً "
        "    معظمها لوجوهات، أو معظمها بوست سوشيال) — اقترح حاجة تكمل البراند.\n"
        " 2. لو الـ Brand Profile ناقص (مفيش لوجو/ألوان) — قول للعميل صراحة "
        "    يكمله من /marketplace/dashboard/brand-profile/ عشان كل تصميم "
        "    قادم يبقى متسق.\n"
        " 3. لو العميل سأل 'إيه أحسن باقة ليّا؟' — حلل استخدامه واقترح "
        "    الباقة اللي تكفي شهر بدون نقص.\n"
        " 4. لو العميل سأل عن فكرة تصميم — اعمله brief مرتب: العناصر، "
        "    الألوان، النص، الجو العام. وقول له ينقله للـ design store.\n\n"
        "🚫 ممنوع: تكتب كود، تخمن أسعار غير اللي في الباقات، تتكلم عن "
        "عملاء تانيين أو بياناتهم.\n"
        "✅ كل ردك بالعربي المصري البسيط، 3-6 جُمل كحد أقصى ما لم يطلب تفصيل."
    )


def _automotive_system_prompt() -> str:
    return (
        "أنت 'مستشار صيانة العربية' لعميل سوق Mouss Tec للسيارات. "
        "بتكلم صاحب العربية نفسه، مش فني. هدفك: تطمنه أو تنذره بناءً على "
        "تاريخ التشخيصات اللي عمله، وتفكره بالصيانة الدورية.\n\n"
        "🎯 منهجك:\n"
        " 1. لو عنده اشتراك نشط — استفد من السياق (عدد السكانات، الكوتة "
        "    المتبقية، الباقة). قول له: تستحق ترقي ولا الباقة دي مكفياك؟\n"
        " 2. لو الاشتراك منتهي أو مش موجود — ادله على /marketplace/diagnostics/pricing/\n"
        " 3. لو سأل 'العربية بتعمل كذا، إيه السبب؟' — اطلب منه يدخل صفحة "
        "    التشخيص (/marketplace/diagnostics/) ويعمل scan أو chat هناك "
        "    عشان الـ context هيكون متاح بـ DTCs.\n"
        " 4. كل نصيحة بصياغة بسيطة (لا مصطلحات تقنية بدون شرح).\n\n"
        "🚫 ممنوع: تشخّص أعطال هنا — هنا الشات استشاري، التشخيص الفعلي في "
        "صفحة التشخيص. ممنوع أسعار قطع، أو ذكر فنيين بأسماء.\n"
        "✅ كل ردك بالعربي المصري، 3-6 جُمل."
    )


# =====================================================================
# Endpoints
# =====================================================================
@csrf_exempt
def advisor_chat(request):
    """POST {message} → reply (sector auto-detected from customer.sector)."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)

    customer = _marketplace_auth(request)
    if not customer:
        return JsonResponse({'error': 'auth_required'}, status=401)

    from erp_core.ai._safety import check_ai_rate_limit
    ok, msg = check_ai_rate_limit(
        f'cust_advisor:{customer.pk}', per_minute=15, per_hour=250,
    )
    if not ok:
        return JsonResponse({'error': 'rate_limited', 'message': msg}, status=429)

    try:
        body = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'invalid_json'}, status=400)

    user_message = (body.get('message') or '').strip()
    if not user_message:
        return JsonResponse({'reply': 'اسألني عن أي حاجة تخص حسابك — وأنا هساعدك.'})
    if len(user_message) > 1500:
        return JsonResponse({'error': 'message_too_long', 'message': 'اختصر السؤال.'}, status=400)

    sector = customer.sector or 'automotive'
    if sector == 'printing':
        system_prompt = _design_system_prompt()
        context_block = _design_context(customer)
    else:
        system_prompt = _automotive_system_prompt()
        context_block = _automotive_context(customer)

    history = list(request.session.get(_SESSION_KEY, []))
    history.append({'role': 'user', 'text': user_message})
    history = history[-(_MAX_HISTORY * 2):]

    messages = [{'role': 'system', 'content': system_prompt}]
    for turn in history[:-1][-_MAX_HISTORY:]:
        role = 'user' if turn.get('role') == 'user' else 'assistant'
        text = str(turn.get('text', '')).strip()
        if text:
            messages.append({'role': role, 'content': text})
    messages.append({
        'role': 'user',
        'content': f'═══ سياق حسابك ═══\n{context_block}\n═══════════════════\n\nسؤال: {user_message}',
    })

    try:
        from inventory.ai_services import call_llm_layer
        answer = call_llm_layer(messages, json_mode=False, max_retries=2)
    except Exception:
        logger.exception('[CUSTOMER ADVISOR] pipeline failed')
        return JsonResponse({
            'error': 'ai_unavailable',
            'message': 'المستشار مش متاح دلوقتي، جرب تاني خلال لحظات.',
        }, status=503)

    if not answer:
        return JsonResponse({
            'error': 'ai_empty',
            'message': 'مفيش رد دلوقتي، جرب تاني.',
        }, status=503)

    answer = answer.strip()
    history.append({'role': 'assistant', 'text': answer})
    request.session[_SESSION_KEY] = history[-(_MAX_HISTORY * 2):]
    request.session.modified = True

    return JsonResponse({
        'reply': answer,
        'sector': sector,
    })


@csrf_exempt
def advisor_reset(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    request.session.pop(_SESSION_KEY, None)
    request.session.modified = True
    return JsonResponse({'status': 'ok'})


def advisor_page(request):
    """Standalone page for the customer advisor — optional dedicated UI."""
    customer = _marketplace_auth(request)
    if not customer:
        from django.shortcuts import redirect
        return redirect('/marketplace/login/')
    sector = customer.sector or 'automotive'
    return render(request, 'clients/marketplace/customer_advisor.html', {
        'customer': customer,
        'sector': sector,
        'sector_label': 'البراند والتصاميم' if sector == 'printing' else 'صيانة العربية',
        'sector_emoji': '🎨' if sector == 'printing' else '🚗',
    })
