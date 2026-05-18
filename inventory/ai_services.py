import requests
import json
import logging
import time
import re
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger('mousstec_ai')

# =====================================================================
# 🛡️ المحرك المركزي المعزز للتخاطب مع الذكاء الاصطناعي (Resilient AI Gateway)
# =====================================================================
def call_gemini_layer(messages, json_mode=False, max_retries=3):
    """
    بوابة الاتصال المزودة بمحرك التعافي الذاتي (Auto-Retry) ومنظف الهلوسة.
    """
    if not getattr(settings, 'ENABLE_AI_PREDICTIONS', False) or not getattr(settings, 'AI_VISION_API_KEY', None):
        logger.warning("⚠️ Mouss Tec AI: محرك الذكاء الاصطناعي معطل في الإعدادات أو المفتاح مفقود.")
        return None

    url = f"{settings.AI_MODEL_ENDPOINT}v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.AI_VISION_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "gemini-1.5-flash",
        "messages": messages,
        "temperature": 0.1, # حرارة منخفضة جداً لضمان الدقة والابتعاد عن الهلوسة
    }

    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    # 🚀 ابتكار: محرك التعافي من أخطاء الشبكات (Exponential Backoff)
    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            if response.status_code == 200:
                res_data = response.json()
                raw_content = res_data['choices'][0]['message']['content']
                
                # 🚀 ابتكار: مقصلة تنظيف الهلوسة (JSON Sanitizer)
                if json_mode:
                    # إزالة علامات الماركداون التي قد تكسر الـ JSON Parser
                    raw_content = re.sub(r'^```json\s*', '', raw_content)
                    raw_content = re.sub(r'^```\s*', '', raw_content)
                    raw_content = re.sub(r'\s*```$', '', raw_content)
                    
                return raw_content
                
            elif response.status_code == 429:
                logger.warning(f"⏳ Mouss Tec AI: ضغط على خوادم جوجل (Rate Limit). المحاولة {attempt + 1}/{max_retries}...")
                time.sleep(2 ** attempt) # انتظار (1، 2، 4) ثوانٍ تصاعدياً
                continue
                
            else:
                logger.error(f"🔴 Mouss Tec AI Error [{response.status_code}]: {response.text}")
                return None
                
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.warning(f"⏳ Mouss Tec AI: انقطاع في الاتصال. المحاولة {attempt + 1}/{max_retries}...")
            time.sleep(2 ** attempt)
            
        except Exception as e:
            logger.error(f"🔴 Mouss Tec AI Fatal Error: {e}")
            return None
            
    logger.error("🛑 Mouss Tec AI: فشلت جميع محاولات الاتصال بمحرك الذكاء الاصطناعي.")
    return None

# =====================================================================
# 🚗 1. مستشار الأعطال وتوقع قطع الغيار (معزز بالذاكرة الشاملة)
# =====================================================================
def predict_parts_from_dtc(dtc_code):
    """
    يحلل كود العطل ويتوقع القطع.
    🚀 مدمج مع الـ Global Semantic Cache لتوفير 99% من تكلفة الـ API وتسريع الرد.
    """
    dtc_clean = str(dtc_code).strip().upper()
    cache_key = f"mousstec_ai_dtc_global_{dtc_clean}"
    
    # 1. فحص الذاكرة الشاملة للمنصة أولاً (Zero-Cost, 5ms Latency)
    cached_result = cache.get(cache_key)
    if cached_result:
        logger.info(f"⚡ Mouss Tec AI: تم جلب تحليل الكود {dtc_clean} من الذاكرة الصاروخية.")
        return cached_result

    # 2. إذا لم يكن بالذاكرة، نسأل العقل المركزي
    system_instruction = (
        "You are a master automotive diagnostic engineer specializing in BMW & MINI Cooper. "
        "Given a diagnostic trouble code (DTC), predict the exact replacement parts required to fix it. "
        "Return strictly a JSON object with a root key 'recommendations' containing an array of objects. "
        "Each object must have: 'part_name' (in clear Arabic), 'probability' (e.g. 90%), and 'p_n' (OEM part number)."
    )
    
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"Analyze this fault code and estimate repair parts: {dtc_clean}"}
    ]
    
    raw_response = call_gemini_layer(messages, json_mode=True)
    if raw_response:
        try:
            parsed_data = json.loads(raw_response)
            # 3. حفظ النتيجة في الذاكرة لمدة 30 يوماً ليستفيد منها كل عملاء المنصة!
            cache.set(cache_key, parsed_data, timeout=30 * 24 * 60 * 60)
            return parsed_data
        except json.JSONDecodeError as e:
            logger.error(f"🔴 Mouss Tec AI JSON Parser Error (DTC): {e}\nRaw: {raw_response}")
            return None
    return None

# =====================================================================
# 👁️ 2. قناص صور الفواتير الورقية والمشتريات (AI Vision OCR)
# =====================================================================
def scan_invoice_image_ai(image_base64):
    """
    يقرأ صور الفواتير الورقية ويستخرج بيانات المورد، الإجمالي، والأصناف للحقن المباشر في المخزن.
    """
    system_instruction = (
        "You are an expert automotive B2B invoice data extractor. Analyze the uploaded invoice image. "
        "Extract the vendor name, invoice total, and line items exactly as they appear. "
        "Return strictly a JSON object with keys: 'vendor_name' (string), 'invoice_total' (float), "
        "and 'items' (array of objects containing 'part_number' or '' if none, 'name' in Arabic, 'qty' as integer, and 'cost' as float)."
    )
    
    messages = [
        {"role": "system", "content": system_instruction},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Extract all structured B2B purchase data from this receipt/invoice image."},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_base64}"
                    }
                }
            ]
        }
    ]
    
    raw_response = call_gemini_layer(messages, json_mode=True, max_retries=2) # محاولتين فقط للصور لثقلها
    if raw_response:
        try: return json.loads(raw_response)
        except json.JSONDecodeError: return None
    return None

# =====================================================================
# 📈 3. رادار الصيانة الاستباقية (Prognostic Maintenance AI)
# =====================================================================
def predict_future_failures(brand, model_name, mileage):
    """
    🚀 ابتكار (Upselling Engine): يتنبأ بما سيتعطل قريباً بناءً على موديل السيارة وقراءة العداد.
    يُستخدم لاقتراح صيانات وقائية للعميل قبل حدوث المشكلة.
    """
    cache_key = f"mousstec_ai_prog_{brand}_{model_name}_{int(mileage/10000)}k"
    cached_result = cache.get(cache_key)
    if cached_result: return cached_result

    system_instruction = (
        "You are a predictive maintenance AI for luxury cars. Based on the brand, model, and current mileage (in KM), "
        "predict the top 3 mechanical/electrical components that are statistically likely to fail within the next 10,000 KM. "
        "Return strictly a JSON object with a root key 'preventive_maintenance' containing an array of objects. "
        "Each object must have: 'system' (e.g. Cooling, Suspension), 'warning_message' (in engaging Arabic), and 'urgency' (High, Medium)."
    )
    
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"Predict failures for: Brand: {brand}, Model: {model_name}, Mileage: {mileage} KM."}
    ]
    
    raw_response = call_gemini_layer(messages, json_mode=True)
    if raw_response:
        try:
            parsed_data = json.loads(raw_response)
            cache.set(cache_key, parsed_data, timeout=7 * 24 * 60 * 60) # كاش لمدة أسبوع
            return parsed_data
        except: return None
    return None

# =====================================================================
# 🎭 4. محلل مشاعر العملاء ومخاطر التسرب (Sentiment & Churn Analyzer)
# =====================================================================
def analyze_customer_sentiment(customer_notes_or_complaints):
    """
    🚀 ابتكار (CRM Intelligence): يقرأ شكاوى أو ملاحظات العميل ليحدد مدى رضاه،
    ويعطي نسبة لاحتمالية تركه للمركز (Churn Risk) لاتخاذ إجراء فوري.
    """
    system_instruction = (
        "You are a Customer Success AI for an automotive service center. Analyze the customer's text/notes. "
        "Determine their sentiment (Positive, Neutral, Angry/Frustrated) and calculate a 'churn_risk_percentage' (0-100). "
        "Return strictly a JSON object with keys: 'sentiment' (string in Arabic), 'churn_risk_percentage' (integer), "
        "and 'recommended_action' (string in Arabic: e.g. 'Offer a 10% discount immediately' or 'Thank them for loyalty')."
    )
    
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"Analyze this customer feedback: '{customer_notes_or_complaints}'"}
    ]
    
    raw_response = call_gemini_layer(messages, json_mode=True)
    if raw_response:
        try: return json.loads(raw_response)
        except: return None
    return None