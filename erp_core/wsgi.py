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

try:
    import resource # يعمل على أنظمة Linux/Mac لمراقبة الذاكرة بدقة
except ImportError:
    resource = None

from django.core.wsgi import get_wsgi_application

# =====================================================================
# 🌍 1. تهيئة البيئة الأساسية
# =====================================================================
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'erp_core.settings')

# 🚀 ابتكار: مسار ملف الصيانة الديناميكي (Fast Lock File)
# بمجرد إنشاء هذا الملف (touch /tmp/mousstec_maintenance.lock)، يدخل السيرفر وضع الصيانة فوراً بدون Restart
MAINTENANCE_LOCK_FILE = Path('/tmp/mousstec_maintenance.lock')

# =====================================================================
# 🚀 2. نواة مراقبة الأداء اللحظي (APM Initialization - Enterprise Ready)
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

# استدعاء تطبيق جانجو الأساسي (يجب أن يتم بعد التهيئة وقبل الـ Wrapper)
django_application = get_wsgi_application()

# =====================================================================
# 🛡️ 3. درع الـ SaaS الخفي (Mouss Tec Enterprise WSGI Firewall)
# =====================================================================
class MoussTecEnterpriseWrapper:
    """
    غلاف WSGI ذكي يعترض الطلبات قبل وصولها لجانجو.
    يقوم بحماية السيرفر من الهجمات، يراقب استهلاك الذاكرة، ويتيح الصيانة اللحظية.
    """
    def __init__(self, application):
        self.application = application
        self.logger = logging.getLogger('mousstec_wsgi')
        
        # محاولة الاتصال بـ Redis لتوحيد حظر الـ IPs بين جميع الـ WSGI Workers
        self.redis_client = None
        try:
            import redis
            redis_url = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/1')
            self.redis_client = redis.from_url(redis_url, socket_connect_timeout=1)
        except Exception:
            self.ip_hits_fallback = {} # حماية بديلة في حال سقوط أو غياب الـ Redis

    def __call__(self, environ, start_response):
        # -------------------------------------------------------------
        # 🛑 وضع الصيانة الديناميكي (Zero-Restart Maintenance)
        # -------------------------------------------------------------
        if MAINTENANCE_LOCK_FILE.exists():
            status = '503 Service Unavailable'
            headers = [('Content-Type', 'application/json')]
            start_response(status, headers)
            return [b'{"status": "maintenance", "message": "Mouss Tec Ecosystem is currently upgrading its core. Please try again in a few minutes."}']

        # 🆔 توليد بصمة تتبع فريدة لكل طلب (Request ID Tracing)
        request_id = environ.get('HTTP_X_REQUEST_ID', str(uuid.uuid4()))
        environ['mousstec.request_id'] = request_id
        
        client_ip = environ.get('HTTP_X_FORWARDED_FOR', environ.get('REMOTE_ADDR', 'unknown')).split(',')[0].strip()
        path = environ.get('PATH_INFO', '')
        content_length = environ.get('CONTENT_LENGTH', '')

        # -------------------------------------------------------------
        # 💣 مصد القنابل البيانية (Anti-JSON Bomb Shield)
        # -------------------------------------------------------------
        if content_length and content_length.isdigit():
            # حد أقصى 2.5 ميجابايت لطلبات الـ API (حماية الرامات من הـ Overload)
            if int(content_length) > 2.5 * 1024 * 1024 and '/api/' in path: 
                status = '413 Payload Too Large'
                headers = [('Content-Type', 'application/json')]
                start_response(status, headers)
                self.logger.warning(f"🚨 Bomb Shield: Blocked huge payload ({content_length} bytes) on {path} from {client_ip}.")
                return [b'{"error": "payload_too_large", "message": "Mouss Tec Firewall: Request payload exceeds the maximum allowed size (2.5 MB)."}']

        # -------------------------------------------------------------
        # 🛑 خنق البوتات الموزع السريع (Distributed Rate Limiting)
        # -------------------------------------------------------------
        # حماية مسارات المزادات والـ APIs الحساسة فقط (لتخفيف العبء)
        if '/api/v1/b2b/bidding/' in path or '/api/v1/b2b/market/' in path:
            limit_exceeded = False
            if self.redis_client:
                try:
                    cache_key = f"wsgi_rate_limit:{client_ip}"
                    hits = self.redis_client.incr(cache_key)
                    if hits == 1:
                        self.redis_client.expire(cache_key, 60) # تصفير بعد 60 ثانية
                    if hits > 120: # السماح بـ 120 طلب في الدقيقة كحد أقصى للمزادات
                        limit_exceeded = True
                except Exception:
                    pass # تجاهل خطأ Redis (Fail-open) للتركيز على استقرار الخدمة
            else:
                # الفولباك (Fallback) للذاكرة المحلية (In-memory)
                current_time = time.time()
                self.ip_hits_fallback = {ip: h for ip, h in self.ip_hits_fallback.items() if current_time - h['time'] < 60}
                if client_ip in self.ip_hits_fallback:
                    if self.ip_hits_fallback[client_ip]['count'] > 120:
                        limit_exceeded = True
                    self.ip_hits_fallback[client_ip]['count'] += 1
                else:
                    self.ip_hits_fallback[client_ip] = {'count': 1, 'time': current_time}

            if limit_exceeded:
                status = '429 Too Many Requests'
                headers = [('Content-Type', 'application/json')]
                start_response(status, headers)
                return [b'{"error": "rate_limit", "message": "Mouss Tec Firewall: Too many requests. Please slow down your automated calls."}']

        # تسجيل وقت وبصمة الذاكرة قبل بدء التنفيذ داخل جانجو
        start_time = time.time()
        start_mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else 0

        # -------------------------------------------------------------
        # ⚖️ التخفيف الذكي للحمل (Smart Load Shedding)
        # -------------------------------------------------------------
        import sys
        is_mac = sys.platform == 'darwin'
        current_mem_mb = start_mem / (1024.0 * 1024.0) if is_mac else start_mem / 1024.0
        
        # إذا كان הـ Worker الحالي يستهلك أكثر من 850 ميجا رامات (حالة طوارئ قصوى)
        if current_mem_mb > 850.0:
            # نرفض الطلبات غير الحرجة (مثل البحث الاستكشافي) للحفاظ على السيرفر
            critical_paths = ['/escrow/', '/bidding/submit-offer/', '/admin/', '/pos/']
            if not any(crit in path for crit in critical_paths):
                status = '503 Service Unavailable'
                headers = [('Content-Type', 'application/json')]
                start_response(status, headers)
                self.logger.critical(f"🚨 Load Shedding Active! Rejected {path} due to extremely high memory usage ({current_mem_mb:.2f} MB).")
                return [b'{"error": "server_overload", "message": "High traffic spike detected. Non-critical requests are temporarily paused to protect the core."}']

        # -------------------------------------------------------------
        # 🔄 معالجة الرد (Response Handling, Tracing & Telemetry)
        # -------------------------------------------------------------
        status_code = [None]
        response_headers = []

        def custom_start_response(status, headers, exc_info=None):
            status_code[0] = status
            process_time = time.time() - start_time
            
            # 🧠 مراقبة تسريب الذاكرة (Memory Leak Watchdog)
            if resource:
                end_mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                mem_diff_mb = (end_mem - start_mem) / (1024.0 * 1024.0) if is_mac else (end_mem - start_mem) / 1024.0
                if mem_diff_mb > 60: # تنبيه إذا استهلك هذا الطلب بالذات أكثر من 60 ميجابايت فجأة
                    self.logger.warning(f"⚠️ [MEMORY LEAK ALERT] Request {request_id} ({path}) consumed a massive {mem_diff_mb:.2f} MB!")

            # إضافة بصمات Mouss Tec الاحترافية (Custom Headers) للردود
            headers.append(('X-Request-ID', request_id))
            headers.append(('X-SaaS-Processing-Time', f"{process_time:.4f}s"))
            headers.append(('X-Powered-By', 'Mouss Tec Ecosystem Engine'))
            headers.append(('X-Server-Node', os.environ.get('NODE_NAME', 'Node-Prime-01')))
            
            # منع الكاش לلـ APIs لضمان أقصى درجات الأمان المالي
            if '/api/' in path:
                headers.append(('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0'))
            
            response_headers.extend(headers)
            return start_response(status, headers, exc_info)

        try:
            # تمرير الطلب لـ Django الأساسي لمعالجته
            return self.application(environ, custom_start_response)
        
        except Exception as e:
            # 🛡️ الدرع الواقي من الانهيار الكامل (API Crash Shield - Fail Gracefully)
            self.logger.critical(f"🚨 [FATAL WSGI CRASH] Request {request_id} | Path {path}: {str(e)}")
            
            error_response = {
                "error": "critical_system_failure",
                "message": "Mouss Tec Core Engine intercepted a critical internal failure. Engineers have been notified.",
                "request_id": request_id
            }
            
            status = '500 Internal Server Error'
            headers = [('Content-Type', 'application/json')]
            start_response(status, headers)
            return [json.dumps(error_response).encode('utf-8')]

# =====================================================================
# 🚀 تشغيل النظام المغلف
# =====================================================================
application = MoussTecEnterpriseWrapper(django_application)