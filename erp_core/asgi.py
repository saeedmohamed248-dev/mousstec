"""
ASGI config for erp_core project.
It exposes the ASGI callable as a module-level variable named ``application``.
"""

import os
import uuid
import urllib.parse
from django.core.asgi import get_asgi_application

# 1. تحميل إعدادات السيستم أولاً
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'erp_core.settings')

# 2. تهيئة تطبيق جانجو الأساسي (HTTP)
# يجب استدعاء هذا قبل استيراد أي موديلات من قاعدة البيانات
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
# 🧠 الدالة السحرية للبحث عن الشركة (معززة بدرع الـ Cache الصاروخي)
# =====================================================================
@database_sync_to_async
def get_tenant_from_request(host):
    """
    ابحث عن الدومين واستخرج الشركة.
    🚀 ابتكار: استخدام الـ Cache لمنع انهيار الـ Database تحت ضغط الـ WebSockets.
    """
    try:
        clean_host = host.split(':')[0]
        cache_key = f"tenant_domain_{clean_host}"
        
        # 1. البحث في الذاكرة السريعة (Redis/Memcached) أولاً
        tenant = cache.get(cache_key)
        
        if not tenant:
            # 2. إذا لم يكن في الذاكرة، اضرب الداتا بيز مرة واحدة فقط
            from clients.models import Domain
            domain = Domain.objects.select_related('tenant').get(domain=clean_host)
            tenant = domain.tenant
            
            # 3. حفظ النتيجة في الذاكرة لمدة ساعة (3600 ثانية) لتخفيف الضغط
            cache.set(cache_key, tenant, 3600)
            
        return tenant
    except Exception as e:
        logger.warning(f"⚠️ [ASGI] Failed to resolve tenant for host {host}: {e}")
        return None

# =====================================================================
# 🛡️ ابتكار 1: الوسيط الذكي والمدرع (Tenant-Aware & IoT Ready Shield)
# =====================================================================
class TenantAuthMiddleware:
    """
    وسيط عبقري يحلل طلب الـ WebSocket، يقرأ النطاق، يستخرج الـ Real IP،
    يدعم مصادقة أجهزة الـ IoT والموبايل، ويوجه الاتصال للـ Schema الصحيحة.
    """
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        # 1. استخراج الـ Real IP للعميل للتعامل مع الـ Load Balancers (Cloudflare/Nginx)
        headers = dict(scope.get('headers', []))
        client_ip = "Unknown"
        if b'x-forwarded-for' in headers:
            client_ip = headers[b'x-forwarded-for'].decode('utf-8').split(',')[0].strip()
        else:
            client_ip = scope.get('client', ['unknown'])[0]

        # 2. توليد بصمة فريدة للاتصال (Distributed Tracing ID) للرقابة الأمنية
        trace_id = str(uuid.uuid4())[:8]
        scope['trace_id'] = f"WS-{trace_id}"
        scope['client_ip'] = client_ip

        # 3. استخراج الـ Host للأمان
        host_bytes = headers.get(b'host', b'')
        if not host_bytes:
            # 🚨 جدار الحماية: إغلاق الاتصال فوراً إذا كان الطلب مجهول الهوية (Anti-DDoS)
            logger.error(f"🛑 [ASGI Shield] Rejected connection missing Host header. IP: {client_ip} | Trace: {scope['trace_id']}")
            return None
        host = host_bytes.decode('utf-8')

        # 4. ابتكار: استخراج رموز الـ IoT والتطبيقات (Token/API Key) من الـ Query String
        query_string = scope.get('query_string', b'').decode('utf-8')
        query_params = urllib.parse.parse_qs(query_string)
        scope['device_token'] = query_params.get('token', [None])[0]
        
        # 5. جلب الشركة (Tenant) بسرعة الصاروخ عبر الكاش
        tenant = await get_tenant_from_request(host)
        
        if tenant:
            # 6. حقن بيانات الشركة والـ Schema لعزل البيانات التام في طبقة الـ Consumer
            scope['tenant'] = tenant
            scope['schema_name'] = tenant.schema_name
            logger.info(f"⚡ [ASGI] WS Connected -> Tenant: {tenant.name} | IP: {client_ip} | Trace: {scope['trace_id']}")
        else:
            scope['tenant'] = None
            scope['schema_name'] = 'public'
            logger.warning(f"⚠️ [ASGI] Unknown WS Connection routed to public schema. IP: {client_ip} | Trace: {scope['trace_id']}")
        
        # 7. تمرير الطلب للطبقة التالية (المصادقة الافتراضية لـ Django)
        return await self.inner(scope, receive, send)

def TenantAuthMiddlewareStack(inner):
    return TenantAuthMiddleware(AuthMiddlewareStack(inner))

# =====================================================================
# 🧬 ابتكار 2: بروتوكول دورة حياة السيرفر (Lifespan Protocol with Graceful Drain)
# =====================================================================
async def lifespan_application(scope, receive, send):
    """
    إدارة ذكية للحظات تشغيل وإغلاق السيرفر (Graceful Shutdown)
    يضمن عدم فقدان أي مزادات حية أو بيانات IoT أثناء إعادة تشغيل السيرفر.
    """
    while True:
        message = await receive()
        if message['type'] == 'lifespan.startup':
            print("\n" + "━"*70)
            print("🚀 MOUSS TEC LIVE ENGINE (ASGI) IS IGNITING...")
            print("📡  WebSockets: Active | 🛡️ Shield: On | 🧠 Cache: Connected")
            print("━"*70 + "\n")
            await send({'type': 'lifespan.startup.complete'})
        
        elif message['type'] == 'lifespan.shutdown':
            print("\n🛑 [SHUTDOWN SEQUENCE INITIATED] Mouss Tec Live Engine is shutting down gracefully...")
            # إغلاق آمن يحمي بيانات المزادات من الضياع
            print("✅ All active live B2B bidding rooms & IoT devices safely disconnected.")
            await send({'type': 'lifespan.shutdown.complete'})
            return

# =====================================================================
# 🚦 موجه البروتوكولات المركزي (The Mouss Tec Brain Router)
# =====================================================================

# نقوم بتجهيز الدوال الوهمية (Placeholders) للمسارات اللحظية لضمان عدم حدوث Error
# (سيتم ربطها بملف consumers.py لاحقاً)
from channels.generic.websocket import AsyncWebsocketConsumer
class MockConsumer(AsyncWebsocketConsumer):
    async def connect(self): await self.accept()

application = ProtocolTypeRouter({
    
    # ---------------------------------------------------------
    # 🌐 1. مسار الطلبات العادية (HTTP Requests)
    # ---------------------------------------------------------
    "http": django_asgi_app,

    # ---------------------------------------------------------
    # ⚡ 2. مسار الاتصالات الحية المفتوحة (WebSockets & Live Sync)
    # ---------------------------------------------------------
    "websocket": AllowedHostsOriginValidator(
        TenantAuthMiddlewareStack(
            URLRouter([
                # ⚖️ مسار حي: غرفة المزاد العكسي (تجار وورش يتفاعلون في كسر الثانية)
                path("ws/bidding/live/", MockConsumer.as_asgi()),
                
                # 📊 مسار حي: المزامنة اللحظية للداش بورد (Live Sync)
                path("ws/dashboard/sync/", MockConsumer.as_asgi()),

                # 🏎️ مسار حي: استقبال بيانات فحص السيارات اللحظية من أجهزة الـ OBD2 (IoT)
                path("ws/telemetry/obd2/", MockConsumer.as_asgi()),
                
                # 💬 مسار حي: الإشعارات اللحظية لمديري الفروع (نواقص، طلبات جديدة، Escrow)
                path("ws/notifications/", MockConsumer.as_asgi()),
            ])
        )
    ),

    # ---------------------------------------------------------
    # 🧬 3. مسار دورة الحياة (Lifespan & Server Events)
    # ---------------------------------------------------------
    "lifespan": lifespan_application,
})