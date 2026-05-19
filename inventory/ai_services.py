import requests
import json
import logging
import time
import re
from django.conf import settings
from django.core.cache import caches

logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🧠 طبقة جلب الكاش المركزي للوكلاء الذكيين
# =====================================================================
def _get_cache():
    """يجلب طبقة الكاش السريعة الذكية للوكلاء لتفادي استهلاك كوتة الـ API"""
    return caches['local_tier'] if 'local_tier' in caches else caches['default']

# =====================================================================
# 🛡️ المحرك المركزي المعزز (Cognitive AI Gateway - MAS Compliant)
# =====================================================================
def call_gemini_layer(messages, json_mode=False, max_retries=3, require_pro=False):
    """
    🚀 بوابة الاتصال الذكية والمحصنة للوكلاء (Agents):
    تم دمج التحصين الشامل: تطهير المفتاح، استخدام v1 Endpoint، ومقصلة تنظيف الـ JSON.
    """
    if not getattr(settings, 'ENABLE_AI_PREDICTIONS', False) or not getattr(settings, 'AI_VISION_API_KEY', None):
        logger.warning("⚠️ [COGNITIVE AGENT]: AI Engine disabled or Missing Key.")
        return None

    # نماذج Gemini 2.0 المستقرة (2026)
    model_name = "gemini-2.0-flash" if require_pro else "gemini-2.0-flash-lite"
    
    # 🔥 تطهير المفتاح من أي \r أو \n أو مسافات عشوائية من ملف الـ .env
    clean_key = str(settings.AI_VISION_API_KEY).strip()
    
    # 🌐 استخدام الـ Stable Production Endpoint المباشر لعام 2026
    url = f"https://generativelanguage.googleapis.com/v1/models/{model_name}:generateContent?key={clean_key}"
    headers = {"Content-Type": "application/json"}

    gemini_contents = []
    system_instruction = None
    
    for msg in messages:
        if msg["role"] == "system":
            system_instruction = {"parts": [{"text": msg["content"]}]}
        else:
            parts = []
            if isinstance(msg["content"], list):
                for item in msg["content"]:
                    if item.get("type") == "text":
                        parts.append({"text": item["text"]})
                    elif item.get("type") == "image_url":
                        b64_data = item["image_url"]["url"].split(",")[1]
                        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64_data}})
            else:
                parts.append({"text": msg["content"]})
            
            gemini_contents.append({"role": "user" if msg["role"] == "user" else "model", "parts": parts})

    payload = {
        "contents": gemini_contents,
        "generationConfig": {
            "temperature": 0.2, 
        }
    }
    
    if system_instruction:
        payload["systemInstruction"] = system_instruction
        
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            if response.status_code == 200:
                res_data = response.json()
                raw_content = res_data['candidates'][0]['content']['parts'][0]['text']
                
                # 🪓 مقصلة تنظيف الهلوسة (تجريد رد جوجل من علامات الماركداون إذا طلبنا JSON)
                if json_mode:
                    raw_content = re.sub(r'^```json\s*', '', raw_content)
                    raw_content = re.sub(r'^```\s*', '', raw_content)
                    raw_content = re.sub(r'\s*```$', '', raw_content)
                    
                return raw_content
                
            elif response.status_code == 429:
                logger.warning(f"⏳ [COGNITIVE AGENT]: Rate Limit Hit. Attempt {attempt + 1}/{max_retries}...")
                time.sleep(2 ** attempt)
                continue
            else:
                logger.error(f"🔴 [COGNITIVE AGENT ERROR] {response.status_code}: {response.text}")
                return None
                
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.warning(f"⏳ [COGNITIVE AGENT]: Connection lost. Attempt {attempt + 1}/{max_retries}... ({e})")
            time.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"🔴 [COGNITIVE AGENT FATAL]: {e}")
            return None
            
    logger.error("🛑 [COGNITIVE AGENT]: All retries exhausted.")
    return None

# =====================================================================
# 🚗 1. مستشار الأعطال وتوقع قطع الغيار (Diagnostic Bot)
# =====================================================================
def predict_parts_from_dtc(dtc_code):
    dtc_clean = str(dtc_code).strip().upper()
    cache_key = f"mas_ai_dtc_{dtc_clean}"
    
    cache = _get_cache()
    cached_result = cache.get(cache_key)
    if cached_result:
        logger.info(f"⚡ [DIAGNOSTIC BOT]: Served '{dtc_clean}' from semantic cache.")
        return cached_result

    system_instruction = (
        "You are an automotive diagnostic AI agent for BMW & MINI. "
        "Given a DTC, return strictly a JSON object with 'recommendations' (array). "
        "Each object: 'part_name' (Arabic), 'probability' (int 0-100), 'p_n' (string)."
    )
    
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"DTC: {dtc_clean}"}
    ]
    
    raw_response = call_gemini_layer(messages, json_mode=True)
    if raw_response:
        try:
            parsed_data = json.loads(raw_response)
            cache.set(cache_key, parsed_data, timeout=30 * 24 * 60 * 60)
            return parsed_data
        except json.JSONDecodeError as e:
            logger.error(f"🔴 [DIAGNOSTIC BOT]: JSON Parse Error - {e}")
    return {"recommendations": []}

# =====================================================================
# 👁️ 2. قناص الفواتير (Vision Procurement Bot)
# =====================================================================
def scan_invoice_image_ai(image_base64):
    system_instruction = (
        "You are a B2B Procurement AI Agent. Extract invoice data. "
        "Return strictly JSON: 'vendor_name' (string), 'invoice_total' (float), "
        "'items' (array: 'part_number' (string), 'name' (Arabic string), 'qty' (int), 'cost' (float))."
    )
    
    messages = [
        {"role": "system", "content": system_instruction},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Extract structured data from this invoice."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
            ]
        }
    ]
    
    raw_response = call_gemini_layer(messages, json_mode=True, max_retries=2, require_pro=True)
    if raw_response:
        try: return json.loads(raw_response)
        except json.JSONDecodeError: pass
    return {"vendor_name": "مجهول", "invoice_total": 0.0, "items": []}

# =====================================================================
# 📈 3. رادار الصيانة الاستباقية (Prognostic Maintenance Bot)
# =====================================================================
def predict_future_failures(brand, model_name, mileage):
    cache_key = f"mas_ai_prog_{brand}_{model_name}_{int(mileage/10000)}k"
    cache = _get_cache()
    cached_result = cache.get(cache_key)
    if cached_result: return cached_result

    system_instruction = (
        "You are a Prognostic AI Agent. Predict top 3 failing parts in the next 10,000 KM. "
        "Return strictly JSON: 'preventive_maintenance' (array: 'system' (string), 'warning_message' (Arabic string), 'urgency' (High/Medium))."
    )
    
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"Brand: {brand}, Model: {model_name}, Mileage: {mileage} KM."}
    ]
    
    raw_response = call_gemini_layer(messages, json_mode=True)
    if raw_response:
        try:
            parsed_data = json.loads(raw_response)
            cache.set(cache_key, parsed_data, timeout=7 * 24 * 60 * 60)
            return parsed_data
        except: pass
    return {"preventive_maintenance": []}

# =====================================================================
# 🎭 4. محلل مخاطر التسرب (CRM Churn Forensics Bot)
# =====================================================================
def analyze_customer_sentiment(customer_notes_or_complaints):
    if not customer_notes_or_complaints: return {"sentiment": "محايد", "churn_risk_percentage": 0}
    
    system_instruction = (
        "You are a CRM AI Agent. Analyze customer feedback. "
        "Return strictly JSON: 'sentiment' (Arabic string), 'churn_risk_percentage' (int 0-100), 'recommended_action' (Arabic string)."
    )
    
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"Feedback: '{customer_notes_or_complaints}'"}
    ]
    
    raw_response = call_gemini_layer(messages, json_mode=True)
    if raw_response:
        try: return json.loads(raw_response)
        except: pass
    return {"sentiment": "غير محدد", "churn_risk_percentage": 50, "recommended_action": "مراجعة يدوية"}

# =====================================================================
# 💸 5. وكيل المرونة السعرية والتسعير (Elastic Pricing Bot)
# =====================================================================
def predict_market_price_elasticity(part_name, condition, average_cost):
    """
    🚀 ابتكار: وكيل يغذي الـ Inventory Models و الـ B2B Marketplace.
    يتوقع مؤشر المرونة (1.0 = سعر عادي، 1.5 = طلب عالي وندرة، 0.8 = متوفر بكثرة).
    """
    cache_key = f"mas_ai_elastic_{part_name}_{condition}"
    cache = _get_cache()
    cached_result = cache.get(cache_key)
    if cached_result: return cached_result

    system_instruction = (
        "You are a B2B Automotive Pricing AI Agent. Analyze the part for market elasticity. "
        "Return strictly JSON: 'elasticity_index' (float between 0.7 and 2.0. High > 1 means rare/high demand), "
        "'suggested_retail' (float based on average cost), 'market_status' (Arabic string explaining the index)."
    )
    
    messages = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"Part: {part_name}, Condition: {condition}, Avg Cost: {average_cost} EGP"}
    ]
    
    raw_response = call_gemini_layer(messages, json_mode=True)
    if raw_response:
        try:
            parsed = json.loads(raw_response)
            cache.set(cache_key, parsed, timeout=2 * 24 * 60 * 60)
            return parsed
        except: pass
    return {"elasticity_index": 1.0, "suggested_retail": average_cost * 1.25, "market_status": "طبيعي"}