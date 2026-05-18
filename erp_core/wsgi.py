"""
WSGI config for erp_core project.
It exposes the WSGI callable as a module-level variable named ``application``.
"""

import os
import time
import uuid
import json
import logging
from pathlib import Path
import urllib.parse

try:
    import resource # يعمل على أنظمة Linux/Mac لمراقبة الذاكرة بدقة
except ImportError:
    resource = None

from django.core.wsgi import get_wsgi_application

# =====================================================================
# 🌍 1. تهيئة البيئة الأساسية
# =====================================================================
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'erp_core.settings')

MAINTENANCE_LOCK_FILE = Path('/tmp/mousstec_maintenance.lock')

# =====================================================================
# 🚀 2. نواة مراقبة الأداء اللحظي (APM Initialization)
# =====================================================================
def initialize_telemetry():
    """تهيئة نظام الـ APM إذا كان مفعلاً في السحابة"""
    if os.environ.get('ENABLE_ENTERPRISE_APM') == 'True':
        try:
            import newrelic.agent
            newrelic.agent.initialize('newrelic.ini')
            print("🟢 Mouss Tec APM: Enterprise Telemetry Connected Successfully.")
        except ImportError:
            pass

initialize_telemetry()

# استدعاء تطبيق جانجو الأساسي
django_application = get_wsgi_application()

# =====================================================================
# 🛡️ 3. درع الـ SaaS الخفي (Mouss Tec Edge WAF & WSGI Firewall)
# =====================================================================
class MoussTecEnterpriseWrapper:
    """
    غلاف WSGI ذكي يعترض الطلبات לפני وصولها لجانجو.
    يحمي السيرفر من الهجمات (WAF)، يمنع اختناق הـ I/O، ويطبق Load Shedding.
    """
    def __init__(self, application):
        self.application = application
        self.logger = logging.getLogger('mousstec_wsgi')
        
        # 🚀 ابتكار: Micro-Cache لمنع اختناق الـ I/O عند فحص ملف الصيانة
        self._maintenance_cached_status = False
        self._maintenance_last_check = 0
        
        # 🚀 ابتكار: تتبع متوسط زمن الاستجابة (Latency) للـ Load Shedding
        self._avg_response_time = 0.5 

        # الاتصال بـ Redis
        self.redis_client = None
        try:
            import redis
            redis_url = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/1')
            self.redis_client = redis.from_url(redis_url, socket_connect_timeout=1)
        except Exception:
            self.ip_hits_fallback = {} 

        # بصمات الهجمات الشائعة للـ Edge WAF
        self.malicious_signatures = [b'<script>', b'union select', b'base64_', b'eval(', b'../']

    def __call__(self, environ, start_response):
        current_time = time.time()
        
        # -------------------------------------------------------------
        # ⚡ كاش الصيانة الميكروي (Zero-I/O Maintenance Lock)
        # -------------------------------------------------------------
        # فحص الهارد ديسك مرة واحدة فقط كل 5 ثوانٍ للـ Worker الواحد
        if current_time - self._maintenance_last_check > 5.0:
            self._maintenance_cached_status = MAINTENANCE_LOCK_FILE.exists()
            self._maintenance_last_check = current_time

        if self._maintenance_cached_status:
            status = '503 Service Unavailable'
            headers = [('Content-Type', 'application/json')]
            start_response(status, headers)
            return [b'{"status": "maintenance", "message": "Mouss Tec Ecosystem is currently upgrading its core. Please try again in a few minutes."}']

        # 🆔 تجهيز البصمات
        request_id = environ.get('HTTP_X_REQUEST_ID', str(uuid.uuid4()))
        environ['mousstec.request_id'] = request_id
        
        client_ip = environ.get('HTTP_X_FORWARDED_FOR', environ.get('REMOTE_ADDR', 'unknown')).split(',')[0].strip()
        path = environ.get('PATH_INFO', '')
        query_string = environ.get('QUERY_STRING', '').lower().encode('utf-8')
        content_length = environ.get('CONTENT_LENGTH', '')
        host = environ.get('HTTP_HOST', '')

        # -------------------------------------------------------------
        # 🛡️ جدار الحماية الطرفي (Edge WAF - Anti-Spoofing & SQLi)
        # -------------------------------------------------------------
        if not host:
            status = '400 Bad Request'
            start_response(status, [('Content-Type', 'text/plain')])
            return [b"Mouss Tec WAF: Missing Host Header."]

        # فحص سريع جداً للـ Query String ضد الهجمات
        if query_string:
            # استخدام urllib لفك التشفير (URL Decode) لكشف الحيل
            decoded_query = urllib.parse.unquote_to_bytes(query_string)
            if any(sig in decoded_query for sig in self.malicious_signatures):
                self.logger.critical(f"🚨 [WAF SHIELD] Dropped malicious payload on {path} from {client_ip}.")
                status = '403 Forbidden'
                start_response(status, [('Content-Type', 'application/json')])
                return [b'{"error": "security_policy_violation", "message": "Mouss Tec WAF: Malicious payload intercepted and blocked."}']

        # -------------------------------------------------------------
        # 💣 مصد القنابل البيانية (Anti-JSON Bomb Shield)
        # -------------------------------------------------------------
        if content_length and content_length.isdigit():
            if int(content_length) > 2.5 * 1024 * 1024 and '/api/' in path: 
                status = '413 Payload Too Large'
                start_response(status, [('Content-Type', 'application/json')])
                self.logger.warning(f"🚨 Bomb Shield: Blocked huge payload ({content_length} bytes) on {path} from {client_ip}.")
                return [b'{"error": "payload_too_large", "message": "Mouss Tec Firewall: Payload exceeds 2.5 MB."}']

        # -------------------------------------------------------------
        # 🛑 خنق البوتات الموزع السريع (Distributed Rate Limiting)
        # -------------------------------------------------------------
        if '/api/v1/b2b/bidding/' in path or '/api/v1/b2b/market/' in path:
            limit_exceeded = False
            if self.redis_client:
                try:
                    cache_key = f"wsgi_rl:{client_ip}"
                    hits = self.redis_client.incr(cache_key)
                    if hits == 1:
                        self.redis_client.expire(cache_key, 60) 
                    if hits > 120: 
                        limit_exceeded = True
                except Exception:
                    pass 
            else:
                self.ip_hits_fallback = {ip: h for ip, h in self.ip_hits_fallback.items() if current_time - h['time'] < 60}
                if client_ip in self.ip_hits_fallback:
                    if self.ip_hits_fallback[client_ip]['count'] > 120:
                        limit_exceeded = True
                    self.ip_hits_fallback[client_ip]['count'] += 1
                else:
                    self.ip_hits_fallback[client_ip] = {'count': 1, 'time': current_time}

            if limit_exceeded:
                status = '429 Too Many Requests'
                start_response(status, [('Content-Type', 'application/json')])
                return [b'{"error": "rate_limit", "message": "Mouss Tec Firewall: Too many requests. Slow down."}']

        start_time = time.time()
        start_mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else 0

        # -------------------------------------------------------------
        # ⚖️ التخفيف التنبؤي للأحمال (Predictive Latency & RAM Shedding)
        # -------------------------------------------------------------
        import sys
        is_mac = sys.platform == 'darwin'
        current_mem_mb = start_mem / (1024.0 * 1024.0) if is_mac else start_mem / 1024.0
        
        # 🚀 ابتكار: إذا كان الـ Worker يستهلك رامات ضخمة، أو زمن الاستجابة التراكمي أصبح بطيئاً جداً (> 3 ثوانٍ)
        if current_mem_mb > 850.0 or self._avg_response_time > 3.0:
            critical_paths = ['/escrow/', '/bidding/submit-offer/', '/admin/', '/pos/', '/webhooks/']
            if not any(crit in path for crit in critical_paths):
                status = '503 Service Unavailable'
                start_response(status, [('Content-Type', 'application/json')])
                self.logger.critical(f"🚨 Load Shedding Active! Latency: {self._avg_response_time:.2f}s | RAM: {current_mem_mb:.2f} MB. Dropped: {path}")
                return [b'{"error": "server_overload", "message": "High traffic spike. Non-critical requests paused to protect core operations."}']

        # -------------------------------------------------------------
        # 🔄 معالجة الرد (Response Handling & Telemetry)
        # -------------------------------------------------------------
        status_code = [None]
        response_headers = []

        def custom_start_response(status, headers, exc_info=None):
            status_code[0] = status
            process_time = time.time() - start_time
            
            # تحديث متوسط زمن الاستجابة (Moving Average)
            self._avg_response_time = (self._avg_response_time * 0.9) + (process_time * 0.1)
            
            # 🧠 مراقبة تسريب الذاكرة (Memory Leak Watchdog)
            if resource:
                end_mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                mem_diff_mb = (end_mem - start_mem) / (1024.0 * 1024.0) if is_mac else (end_mem - start_mem) / 1024.0
                if mem_diff_mb > 60: 
                    self.logger.warning(f"⚠️ [MEMORY LEAK] Req {request_id} ({path}) consumed {mem_diff_mb:.2f} MB!")

            # إضافة بصمات Mouss Tec الاحترافية 
            headers.append(('X-Request-ID', request_id))
            headers.append(('X-SaaS-Latency', f"{process_time:.4f}s"))
            headers.append(('X-Powered-By', 'Mouss Tec Enterprise Engine'))
            
            if '/api/' in path:
                headers.append(('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0'))
            
            response_headers.extend(headers)
            return start_response(status, headers, exc_info)

        try:
            return self.application(environ, custom_start_response)
        
        except Exception as e:
            # 🛡️ الدرع الواقي من الانهيار الكامل (Fail Gracefully)
            self.logger.critical(f"🚨 [FATAL WSGI CRASH] Req {request_id} | Path {path}: {str(e)}")
            
            error_response = {
                "error": "critical_system_failure",
                "message": "Mouss Tec Core intercepted a critical failure. Engineers notified.",
                "request_id": request_id
            }
            
            status = '500 Internal Server Error'
            start_response(status, [('Content-Type', 'application/json')])
            return [json.dumps(error_response).encode('utf-8')]

# =====================================================================
# 🚀 تشغيل النظام المغلف
# =====================================================================
application = MoussTecEnterpriseWrapper(django_application)