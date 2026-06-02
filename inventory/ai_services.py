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
# 🛡️ Cognitive AI Gateway — Together AI for text/JSON, Gemini for vision
# =====================================================================
_TOGETHER_CHAT_URL = 'https://api.together.xyz/v1/chat/completions'
_DEFAULT_TOGETHER_MODEL = 'meta-llama/Llama-3.3-70B-Instruct-Turbo'


def _messages_contain_image(messages):
    for msg in messages:
        content = msg.get('content')
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'image_url':
                    return True
    return False


def _strip_json_fences(text):
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'^```\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


def _call_together_text(messages, json_mode, max_retries):
    api_key = str(getattr(settings, 'TOGETHER_API_KEY', '') or '').strip()
    if not api_key:
        logger.warning("⚠️ [COGNITIVE AGENT]: TOGETHER_API_KEY missing — text layer disabled.")
        return None

    model = str(getattr(settings, 'TOGETHER_LLM_MODEL', '') or _DEFAULT_TOGETHER_MODEL).strip()
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}

    payload = {
        'model': model,
        'messages': messages,
        'temperature': 0.2,
        'max_tokens': 800,   # ✅ بدون الحد ده الموديل بيولّد response طويل ويعدّي الـ timeout
    }
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}

    last_error = None
    for attempt in range(max_retries):
        try:
            # ✅ Daphne default = 60s؛ نسيب margin مناسب لرد 70B JSON
            response = requests.post(_TOGETHER_CHAT_URL, headers=headers, json=payload, timeout=45)

            if response.status_code == 200:
                try:
                    raw_content = response.json()['choices'][0]['message']['content']
                except (KeyError, IndexError, ValueError) as e:
                    logger.error(f"🔴 [COGNITIVE AGENT]: Malformed Together response: {response.text[:300]} — {e}")
                    return None
                if json_mode:
                    raw_content = _strip_json_fences(raw_content)
                return raw_content

            if response.status_code == 429:
                logger.warning(f"⏳ [COGNITIVE AGENT]: Together rate limit. Attempt {attempt + 1}/{max_retries}.")
                time.sleep(2 ** attempt)
                last_error = 'together_429'
                continue

            logger.error(f"🔴 [COGNITIVE AGENT] Together {model} HTTP {response.status_code}: {response.text[:300]}")
            last_error = f'together_{response.status_code}'
            break

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.warning(f"⏳ [COGNITIVE AGENT]: Together connection lost. Attempt {attempt + 1}/{max_retries} — {e}")
            time.sleep(2 ** attempt)
            last_error = str(e)
        except Exception as e:
            logger.error(f"🔴 [COGNITIVE AGENT FATAL] Together: {e}")
            last_error = str(e)
            break

    logger.error(f"🛑 [COGNITIVE AGENT]: Together text path exhausted. Last error: {last_error}")
    return None


def _call_gemini_vision(messages, json_mode, max_retries):
    """Image-only path. Kept on Gemini until a Together vision model is wired up."""
    api_key = str(getattr(settings, 'AI_VISION_API_KEY', '') or '').strip()
    if not api_key:
        logger.warning("⚠️ [COGNITIVE AGENT]: AI_VISION_API_KEY missing — vision layer disabled.")
        return None

    primary_model = 'gemini-2.5-flash'
    fallback_models = ['gemini-2.0-flash', 'gemini-2.0-flash-lite']
    models_to_try = [primary_model] + [m for m in fallback_models if m != primary_model]

    def _build_url(model):
        return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    gemini_contents = []
    system_instruction = None
    for msg in messages:
        if msg['role'] == 'system':
            system_instruction = {'parts': [{'text': msg['content']}]}
            continue
        parts = []
        if isinstance(msg['content'], list):
            for item in msg['content']:
                if item.get('type') == 'text':
                    parts.append({'text': item['text']})
                elif item.get('type') == 'image_url':
                    b64_data = item['image_url']['url'].split(',', 1)[-1]
                    parts.append({'inline_data': {'mime_type': 'image/jpeg', 'data': b64_data}})
        else:
            parts.append({'text': msg['content']})
        gemini_contents.append({'role': 'user' if msg['role'] == 'user' else 'model', 'parts': parts})

    payload = {'contents': gemini_contents, 'generationConfig': {'temperature': 0.2}}
    if system_instruction:
        payload['systemInstruction'] = system_instruction
    if json_mode:
        payload['generationConfig']['responseMimeType'] = 'application/json'

    headers = {'Content-Type': 'application/json'}
    last_error = None
    for model in models_to_try:
        for attempt in range(max_retries):
            try:
                response = requests.post(_build_url(model), headers=headers, json=payload, timeout=30)
                if response.status_code == 200:
                    try:
                        raw_content = response.json()['candidates'][0]['content']['parts'][0]['text']
                    except (KeyError, IndexError, ValueError) as e:
                        logger.error(f"🔴 [COGNITIVE AGENT]: Malformed Gemini response from {model} — {e}")
                        last_error = 'malformed'
                        break
                    if json_mode:
                        raw_content = _strip_json_fences(raw_content)
                    if model != primary_model:
                        logger.info(f"✅ [COGNITIVE AGENT]: Vision succeeded on fallback {model}")
                    return raw_content
                if response.status_code == 429:
                    logger.warning(f"⏳ [COGNITIVE AGENT]: Vision rate limit {model}. Attempt {attempt + 1}/{max_retries}")
                    time.sleep(2 ** attempt)
                    last_error = 'vision_429'
                    continue
                if response.status_code in (400, 403, 404):
                    logger.warning(f"⚠️ [COGNITIVE AGENT]: Vision model {model} unavailable ({response.status_code}): {response.text[:200]}")
                    last_error = f'{model}_{response.status_code}'
                    break
                logger.error(f"🔴 [COGNITIVE AGENT ERROR] Vision {model} {response.status_code}: {response.text[:300]}")
                last_error = f'{model}_{response.status_code}'
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning(f"⏳ [COGNITIVE AGENT]: Vision connection lost. Attempt {attempt + 1}/{max_retries} — {e}")
                time.sleep(2 ** attempt)
                last_error = str(e)
            except Exception as e:
                logger.error(f"🔴 [COGNITIVE AGENT FATAL] Vision {model}: {e}")
                last_error = str(e)
                break

    logger.error(f"🛑 [COGNITIVE AGENT]: Vision path exhausted. Last error: {last_error}")
    return None


def call_llm_layer(messages, json_mode=False, max_retries=3, require_pro=False):
    """
    Unified cognitive gateway:
      • Text / JSON  → Together AI (TOGETHER_LLM_MODEL)
      • Vision (image_url in payload) → Gemini, until a Together vision model is wired up.
    Input/output shape preserved so all 9 legacy callers keep working unchanged.
    `require_pro` is accepted for compatibility but currently ignored (single Together model).
    """
    if not getattr(settings, 'ENABLE_AI_PREDICTIONS', False):
        logger.warning("⚠️ [COGNITIVE AGENT]: AI Engine disabled (ENABLE_AI_PREDICTIONS=False).")
        return None

    if _messages_contain_image(messages):
        return _call_gemini_vision(messages, json_mode=json_mode, max_retries=max_retries)
    return _call_together_text(messages, json_mode=json_mode, max_retries=max_retries)


# 🪡 Deprecated alias — kept temporarily so old imports don't crash.
# All new code MUST call `call_llm_layer` directly. The router decides
# Together-vs-Gemini based on payload (image_url ⇒ Gemini).
def call_gemini_layer(*args, **kwargs):
    logger.warning(
        "⚠️ [DEPRECATED] call_gemini_layer is a misleading alias — use call_llm_layer. "
        "Text/JSON goes to Together AI (Llama-3.3); vision goes to Gemini."
    )
    return call_llm_layer(*args, **kwargs)

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
    
    raw_response = call_llm_layer(messages, json_mode=True)
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
    
    raw_response = call_llm_layer(messages, json_mode=True, max_retries=2, require_pro=True)
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
    
    raw_response = call_llm_layer(messages, json_mode=True)
    if raw_response:
        try:
            parsed_data = json.loads(raw_response)
            cache.set(cache_key, parsed_data, timeout=7 * 24 * 60 * 60)
            return parsed_data
        except Exception: pass
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
    
    raw_response = call_llm_layer(messages, json_mode=True)
    if raw_response:
        try: return json.loads(raw_response)
        except Exception: pass
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
    
    raw_response = call_llm_layer(messages, json_mode=True)
    if raw_response:
        try:
            parsed = json.loads(raw_response)
            cache.set(cache_key, parsed, timeout=2 * 24 * 60 * 60)
            return parsed
        except Exception: pass
    return {"elasticity_index": 1.0, "suggested_retail": average_cost * 1.25, "market_status": "طبيعي"}