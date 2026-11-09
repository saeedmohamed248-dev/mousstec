"""
🤖 AI Assistant — landing-page sales/education bot.

Powered by Gemini (free tier) via the unified LLM gateway. Falls back to
a deterministic local reply when AI is disabled or the API errors.

Extracted from ``_legacy.py`` (Step 5 of the incremental split).
"""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

from ._shared import _landing_bot_local_reply

logger = logging.getLogger('mouss_tec_core')


# 🤖 AI Assistant — مساعد ذكي للموقع الرئيسي (Landing Page)
# يعمل بـ Gemini (مجاني) — يعرف كل شيء عن القطاعين
# =====================================================================

# 🐛 [FIX]: البرومبت كان بيقول «السوق وتحليل الأعطال من جولد والمستشار في إمباير بس» — اتغيّر في باقات نوفمبر ٢٠٢٦
LANDING_BOT_KNOWLEDGE = """أنت "مساعد Mouss Tec الذكي" — مساعد مبيعات ودعم متخصص في منصة Mouss Tec.

🏢 تعريف الشركة:
Mouss Tec شركة مصرية متخصصة في الأتمتة الذكية وأنظمة ERP السحابية.
رسالتنا: نوفر منصة أعمال موحّدة (Business Operating System) لأي مجال — على نفس فلسفة الشركات العالمية زي SAP و Oracle و Odoo و Zoho — لكن بسعر مصري وواجهة عربية وبدعم محلي.
- منصة سحابية متكاملة (SaaS) شغّالة 24/7 بدون تنصيب
- أتمتة كل دورة الشغل: مبيعات، مشتريات، مخزون، محاسبة، CRM، HR، تقارير
- ذكاء اصطناعي مدمج: مستشار ذكي يرد من بيانات شركتك الفعلية + توقعات + تنبيهات
- قابلة للتخصيص لأي قطاع: حالياً جاهزة بالكامل لـ"السيارات" و"الطباعة"، ومرنة للتوسع لأي مجال (مطاعم، صيدليات، مصانع، خدمات...)
- سوق B2B مدمج لربط الشركات ببعض

أنت موجود على المنصة كلها (Landing + الصفحات الداخلية). مهمتك:
1. تعليم الزوار والعملاء كل حاجة عن النظام والشركة بالتفصيل
2. الإجابة عن أي سؤال يخص النظام (مهما كان) — أسعار، فيتشرز، إعداد، استخدام، فواتير، مخزون، تقارير، الذكاء الاصطناعي، الباقات، الدفع
3. الرد على أسئلة العملاء الحاليين عن استخدام النظام (مش بس الزوار الجدد)
4. إقناع الزوار بتجربة النظام مجاناً
5. لو سؤال خارج معرفتك — قول بصراحة "أنصحك تتواصل مع فريق الدعم" + اعرض عليه فورم المساعدة

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
- كشف أعطال لكل العربيات (صيني/كوري/ياباني/أوروبي/أمريكي) بجهاز OBD — في كل الباقات بعدد عربيات في الشهر
🎁 جوه كل الباقات من غير فلوس زيادة (بأعداد بتكبر مع الباقة — الأرقام في الأسعار تحت):
  رد آلي ذكي على واتساب، استوديو التسويق (بوستات بالذكاء الاصطناعي)، السوق (B2B وقطع الغيار) من غير عمولة،
  المستشار الذكي، وكشف الأعطال. ماسنجر/إنستجرام/الكومنتات والإعلانات الممولة من جولد/برو.
⚠️ مزايا زي تقارير الأرباح المتقدمة والمرتبات والفروع من جولد، وعقود الأساطيل في إمباير — ماتوعدش بميزة مش في الباقة.

🎨 القطاع الثاني: المطابع والتصميم
نظام متخصص لشركات الطباعة ومكاتب التصميم:
- طلبات طباعة: إنشاء طلب → إضافة مهام (تيشرت، كروت، بوسترات...) → تسعير تلقائي
- إدارة المصممين: سجل أعمال كل مصمم + تقييمات + ساعات العمل
- ماكينات الطباعة: حاسبة تكلفة CMYK لكل ماكينة → تعرف ربحك الحقيقي
- مخزون الخامات: ورق، أحبار، خامات تيشرت — مع تنبيه نقص تلقائي
- ملفات المشاريع: رفع ملفات التصميم وحفظها مع كل طلب
- صلاحيات الموظفين: تحكم كامل — مين يشوف الخزينة، مين يعدل الطلبات
- AI Studio: توليد تصاميم بالذكاء الاصطناعي + علامة مائية ذكية — في كل الباقات بعدد تصاميم في الشهر (10 / 50 / 100 / 300)
- مستشار ذكي (AI Copilot): بوت داخلي متصل بالداتابيز — اسأله "بيعنا كام؟" ويرد من بياناتك الحقيقية

{LIVE_PRICING}

📞 قنوات التواصل والدعم (لا تذكرها إلا لما العميل يطلب التواصل/المساعدة/الشكوى أو لما تكون مش قادر تجاوب على سؤاله):
- 📱 رقم الاتصال/واتساب: 01094850763
- 📨 فورم المساعدة موجود جوه الشات نفسه — في تبويب "محتاج مساعدة؟" أعلى الشات
- 📘 صفحة الفيسبوك الرسمية: https://www.facebook.com/profile.php?id=61589886542375

🚫 قواعد صارمة:
- لا تخترع معلومات — إذا مش عارف، قل "أنصحك تتواصل مع فريق الدعم على 01094850763 أو من فورم المساعدة جوه الشات"
- ⚠️ مهم جداً: ما تذكرش رقم التليفون 01094850763 في أي رد إلا لو:
   (أ) العميل صراحةً طلب رقم/تواصل/مساعدة/شكوى
   (ب) أو ما قدرتش تجاوب على سؤاله
   (ج) أو طلب التحدث مع إنسان/موظف دعم
   غير كده — جاوب على السؤال مباشرة من غير ما تحط الرقم
- كن ودود ومفيد واستخدم عربي مصري بسيط
- ردودك قصيرة ومفيدة (3-5 جمل كحد أقصى)
- لما حد يسأل عن الأسعار، اسأله عن نشاطه (سيارات ولا طباعة ولا مجال تاني) عشان تديله الباقة المناسبة
- شجّع الزوار على التجربة المجانية {TRIAL_DAYS} يوم بكل المزايا
- لو سأل "إنتو بتعملوا إيه؟" أو "مين Mouss Tec؟" — استخدم تعريف الشركة فوق
- لو سأل عن مجال غير السيارات والطباعة (مطاعم، صيدليات، إلخ) — قول إن المنصة قابلة للتخصيص لأي قطاع وادعوه يتواصل مع الدعم لمناقشة احتياجاته
- استخدم إيموجي باعتدال
"""


def landing_bot_knowledge() -> str:
    """🐛 [FIX]: الأسعار والخصومات والتجربة كانت مكتوبة في البرومبت بأرقام قديمة (سيلفر 780، Empire
    1,800، تجربة 7 أيام…) — البوت كان بيوعد العملاء بأسعار غير اللي بيتحاسبوا عليها. دلوقتي من الداتابيز."""
    from clients.models.tenancy import TRIAL_DAYS
    from clients.services.pricing_facts import pricing_text_for_bot
    try:
        live = pricing_text_for_bot()
    except Exception:
        logger.exception("[AI ASSISTANT] live pricing failed")
        live = "💰 الأسعار: اطلع على صفحة الأسعار https://mousstec.com/pricing/"
    return (LANDING_BOT_KNOWLEDGE.replace('{LIVE_PRICING}', live)
            .replace('{TRIAL_DAYS}', str(TRIAL_DAYS)))


# (رسائل، ثواني) — زائر: ٨ في الدقيقة و٦٠ في اليوم؛ مستخدم داخل: ٢٠ في الدقيقة و٣٠٠ في اليوم
VISITOR_LIMITS = ((8, 60), (60, 24 * 3600))
MEMBER_LIMITS = ((20, 60), (300, 24 * 3600))


def _hit(key, ttl):
    """عدّاد ذرّي في الكاش (add ثم incr) — بيرجّع العدد بعد الزيادة."""
    if cache.add(key, 1, ttl):
        return 1
    try:
        return cache.incr(key)
    except ValueError:   # المفتاح انتهى بين add و incr
        cache.add(key, 1, ttl)
        return 1


def _rate_limited(request):
    from erp_core.client_ip import client_ip
    user = getattr(request, 'user', None)
    member = bool(user is not None and user.is_authenticated)
    who = f'u{user.pk}' if member else f'ip{client_ip(request) or "unknown"}'
    for limit, window in (MEMBER_LIMITS if member else VISITOR_LIMITS):
        if _hit(f'ai_chat_rate:{window}:{who}', window) > limit:
            return True
    return False


def ai_assistant_api(request):
    """🤖 API endpoint للمساعد الذكي — يعمل بـ Gemini (مجاني)"""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    # 🛡️ حد للرسائل — كل رسالة بتكلّف نداء LLM. الزائر (مش داخل) أقل من المستخدم.
    # 🐛 [FIX]: كان get ثم set (طلبين في نفس اللحظة بيعدّوا الاتنين) ومن أول قيمة في
    #    X-Forwarded-For من غير تحقق — بقى عدّاد ذرّي على IP سليم (erp_core.client_ip).
    if _rate_limited(request):
        return JsonResponse({
            'reply': '⚠️ عدد الرسائل كثير. حاول مرة أخرى بعد شوية.',
            'status': 'rate_limited'
        }, status=429)

    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            return JsonResponse({'error': 'بيانات غير صالحة'}, status=400)
        user_message = str(data.get('message') or '').strip()
        conversation_history = data.get('history') or []
        if not isinstance(conversation_history, list):
            conversation_history = []

        if not user_message:
            return JsonResponse({'error': 'الرسالة فارغة'}, status=400)

        if len(user_message) > 1000:
            return JsonResponse({'error': 'الرسالة طويلة جداً'}, status=400)

        # ── Gemini API call (مجاني) ──
        gemini_reply = None
        try:
            from inventory.ai_services import call_llm_layer

            ai_enabled = getattr(settings, 'ENABLE_AI_PREDICTIONS', False)
            api_key = getattr(settings, 'AI_VISION_API_KEY', None)

            if ai_enabled and api_key:
                # بناء سجل المحادثة (آخر 6 رسائل فقط)
                messages = [
                    {"role": "system", "content": landing_bot_knowledge()},
                ]
                for msg in conversation_history[-6:]:
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get('role', 'user')
                    content = str(msg.get('content') or '')[:1500]
                    if role == 'user' and content:
                        messages.append({"role": "user", "content": content})
                    elif role == 'assistant' and content:
                        messages.append({"role": "assistant", "content": content})

                messages.append({"role": "user", "content": user_message})
                gemini_reply = call_llm_layer(messages, json_mode=False, max_retries=1)
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
