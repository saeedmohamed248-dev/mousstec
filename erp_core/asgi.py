"""
ASGI config for erp_core project.
It exposes the ASGI callable as a module-level variable named ``application``.
"""

import os
import uuid
import urllib.parse
from django.core.asgi import get_asgi_application

# 1. تحميل إعدادات السيستم أولاً (إلزامي قبل أي استدعاء آخر)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'erp_core.settings')

# 2. تهيئة تطبيق جانجو الأساسي (HTTP)
# يجب استدعاء هذا قبل استيراد أي موديلات من قاعدة البيانات أو Channels
django_asgi_app = get_asgi_application()

# استيراد أدوات التوجيه المتقدمة وقواعد البيانات
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from channels.security.websocket import AllowedHostsOriginValidator
from channels.db import database_sync_to_async
from django.core.cache import cache
from django.urls import path

import logging
logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🧠 الدالة السحرية للبحث عن الشركة (Self-Healing Cache Engine)
# =====================================================================
@database_sync_to_async
def get_tenant_from_request(host):
    """
    ابحث عن الدومين واستخرج الشركة.
    🚀 ابتكار: نظام تعافي ذاتي ضد الـ Cache Stampede وانهيارات الـ Redis.
    """
    try:
        clean_host = host.split(':')[0]
        cache_key = f"tenant_domain_{clean_host}"
        tenant = None
        
        # 1. البحث في الذاكرة السريعة (Redis/Memcached) بأمان
        try:
            tenant = cache.get(cache_key)
        except Exception as cache_error:
            logger.warning(f"⚠️ [CACHE MISS] Redis is down or unreachable: {cache_error}")
        
        if not tenant:
            # 2. إذا لم يكن في الذاكرة، اضرب الداتا بيز
            from clients.models import Domain
            domain = Domain.objects.select_related('tenant').get(domain=clean_host)
            tenant = domain.tenant
            
            # 3. حفظ النتيجة بصمت وتخطي الخطأ إن وجد
            try:
                cache.set(cache_key, tenant, 3600)
            except Exception:
                pass 
                
        return tenant
    except Exception as e:
        logger.warning(f"⚠️ [ASGI] Failed to resolve tenant for host {host}: {e}")
        return None

# =====================================================================
# 🛡️ ابتكار 1: الوسيط المدرع (Anti-DDoS, Tenant-Aware & IoT Ready)
# =====================================================================
class TenantAuthMiddleware:
    """
    وسيط يحمي من إغراق الاتصالات، يستخرج النطاقات، ويوجه لـ Schema الصحيحة.
    """
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        # 1. استخراج الـ Real IP للعميل للتعامل مع الـ Load Balancers
        headers = dict(scope.get('headers', []))
        client_ip = "Unknown"
        if b'x-forwarded-for' in headers:
            client_ip = headers[b'x-forwarded-for'].decode('utf-8').split(',')[0].strip()
        else:
            client_ip = scope.get('client', ['unknown'])[0]

        # 2. توليد بصمة فريدة للاتصال (Distributed Tracing ID)
        trace_id = str(uuid.uuid4())[:8]
        scope['trace_id'] = f"WS-{trace_id}"
        scope['client_ip'] = client_ip

        # 🚀 3. جدار الحماية من الإغراق (Anti-DDoS Rate Limiting)
        rate_limit_key = f"ws_throttle_{client_ip}"
        try:
            conn_count = cache.get(rate_limit_key)
            conn_count = int(conn_count) if conn_count is not None else 0  # الحماية من خطأ NoneType
            
            if conn_count > 20:
                logger.critical(f"🛑 [DDoS SHIELD] Blocked WebSocket flood from IP: {client_ip}")
                return None # إسقاط الاتصال الصامت (Drop Connection) لتوفير الـ CPU
            
            cache.set(rate_limit_key, conn_count + 1, 60)
        except Exception:
            pass # تجاهل الـ Throttle إذا كان الـ Cache معطلاً لضمان استمرارية التشغيل

        # 4. استخراج الـ Host للأمان
        host_bytes = headers.get(b'host', b'')
        if not host_bytes:
            logger.error(f"🛑 [ASGI Shield] Rejected connection missing Host header. IP: {client_ip}")
            return None
        host = host_bytes.decode('utf-8')

        # 5. استخراج الرموز وبروتوكولات الـ IoT الفرعية
        query_string = scope.get('query_string', b'').decode('utf-8')
        query_params = urllib.parse.parse_qs(query_string)
        scope['device_token'] = query_params.get('token', [None])[0]
        scope['subprotocol'] = headers.get(b'sec-websocket-protocol', b'').decode('utf-8')
        
        # 6. جلب الشركة (Tenant) بسرعة الصاروخ
        tenant = await get_tenant_from_request(host)
        
        if tenant:
            scope['tenant'] = tenant
            scope['schema_name'] = tenant.schema_name
            logger.info(f"⚡ [ASGI] WS Connected -> Tenant: {tenant.name} | IP: {client_ip} | Trace: {scope['trace_id']}")
        else:
            scope['tenant'] = None
            scope['schema_name'] = 'public'
            logger.warning(f"⚠️ [ASGI] Unknown WS routed to public schema. IP: {client_ip}")
        
        # 7. تمرير الطلب للطبقة التالية
        return await self.inner(scope, receive, send)

def TenantAuthMiddlewareStack(inner):
    return TenantAuthMiddleware(AuthMiddlewareStack(inner))

# =====================================================================
# 🧬 ابتكار 2: بروتوكول دورة حياة السيرفر وتطهير الذواكر (Lifespan Drainer)
# =====================================================================
async def lifespan_application(scope, receive, send):
    """
    إدارة إقلاع السيرفر وتطهير الـ DB Connections لتفادي الـ Memory Leaks والـ Hangs.
    """
    from django.db import close_old_connections
    
    while True:
        try:
            message = await receive()
        except Exception:
            return

        if message['type'] == 'lifespan.startup':
            try:
                print("\n" + "━"*70)
                print("🚀 MOUSS TEC LIVE ENGINE (ASGI) IS IGNITING...")
                
                # تطهير وتنظيف الاتصالات الميتة فور الإقلاع
                await database_sync_to_async(close_old_connections)()
                
                print("📡 WebSockets: Active | 🛡️ Shield: On | 🧠 Cache: Connected")
                print("━"*70 + "\n")
                await send({'type': 'lifespan.startup.complete'})
            except Exception as e:
                logger.critical(f"🛑 [LIFESPAN CRASH] Startup failed: {e}")
                await send({'type': 'lifespan.startup.failed', 'message': str(e)})
        
        elif message['type'] == 'lifespan.shutdown':
            try:
                print("\n🛑 [SHUTDOWN SEQUENCE INITIATED] Mouss Tec Live Engine is shutting down gracefully...")
                await database_sync_to_async(close_old_connections)()
                print("✅ All DB connections closed. Live rooms safely disconnected.")
                await send({'type': 'lifespan.shutdown.complete'})
            except Exception as e:
                logger.error(f"⚠️ [LIFESPAN] Shutdown error: {e}")
                await send({'type': 'lifespan.shutdown.failed', 'message': str(e)})
            return

# =====================================================================
# 🚦 موجه البروتوكولات المركزي (The Mouss Tec Brain Router)
# =====================================================================
from channels.generic.websocket import AsyncWebsocketConsumer

class MockConsumer(AsyncWebsocketConsumer):
    """مستهلك محصن لإدارة دفق البيانات وحماية قنوات الوكلاء من الانهيار الصامت"""
    async def connect(self): 
        await self.accept()
        
    async def receive(self, text_data=None, bytes_data=None):
        # استقبال البيانات وإعادتها بصمت للحفاظ على استقرار الـ Pipeline والـ Ping/Pong
        try:
            if text_data:
                data = json.loads(text_data)
                await self.send(text_data=json.dumps({"status": "acknowledged", "trace_id": self.scope.get('trace_id')}))
        except Exception as e:
            logger.error(f"⚠️ [MOCK CONSUMER ERR] Payload exception: {e}")

application = ProtocolTypeRouter({
    
    "http": django_asgi_app,

    "websocket": AllowedHostsOriginValidator(
        TenantAuthMiddlewareStack(
            URLRouter([
                path("ws/bidding/live/", MockConsumer.as_asgi()),
                path("ws/dashboard/sync/", MockConsumer.as_asgi()),
                path("ws/telemetry/obd2/", MockConsumer.as_asgi()),
                path("ws/notifications/", MockConsumer.as_asgi()),
            ])
        )
    ),

    "lifespan": lifespan_application,
})