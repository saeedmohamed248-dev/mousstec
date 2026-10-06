"""
ASGI config for erp_core project.
It exposes the ASGI callable as a module-level variable named ``application``.
"""

import os
import uuid
import urllib.parse
import json  # 🚀 تم إضافة استدعاء مكتبة الجيسون لمنع الكراش
from django.core.asgi import get_asgi_application

# 1. تحميل إعدادات السيستم أولاً
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'erp_core.settings')

# 2. تهيئة تطبيق جانجو الأساسي (HTTP)
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddleware
from channels.sessions import CookieMiddleware, SessionMiddleware as ChannelsSessionMiddleware
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
    try:
        clean_host = host.split(':')[0]
        cache_key = f"tenant_domain_{clean_host}"
        tenant = None
        
        try:
            tenant = cache.get(cache_key)
        except Exception as cache_error:
            logger.warning(f"⚠️ [CACHE MISS] Redis is down: {cache_error}")
        
        if not tenant:
            from clients.models import Domain
            domain = Domain.objects.select_related('tenant').get(domain=clean_host)
            tenant = domain.tenant
            
            try:
                cache.set(cache_key, tenant, 3600)
            except Exception:
                pass 
                
        return tenant
    except Exception as e:
        logger.warning(f"⚠️ [ASGI] Failed to resolve tenant for host {host}: {e}")
        return None

# =====================================================================
# 🛡️ الوسيط المدرع (Anti-DDoS & Valid ASGI Closer)
# =====================================================================
class TenantAuthMiddleware:
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        try:
            headers = dict(scope.get('headers', []))
            client_ip = "Unknown"
            if b'x-forwarded-for' in headers:
                client_ip = headers[b'x-forwarded-for'].decode('utf-8').split(',')[0].strip()
            else:
                client_ip = scope.get('client', ['unknown'])[0]

            trace_id = str(uuid.uuid4())[:8]
            scope['trace_id'] = f"WS-{trace_id}"
            scope['client_ip'] = client_ip

            # جدار الحماية (Anti-DDoS)
            rate_limit_key = f"ws_throttle_{client_ip}"
            try:
                conn_count = cache.get(rate_limit_key)
                conn_count = int(conn_count) if conn_count is not None else 0
                
                if conn_count > 20:
                    logger.critical(f"🛑 [DDoS SHIELD] Blocked WS flood from IP: {client_ip}")
                    # 🚀 إصلاح الـ ASGI: إرسال إغلاق شرعي بدلاً من return None
                    await send({"type": "websocket.close", "code": 4000})
                    return 
                
                cache.set(rate_limit_key, conn_count + 1, 60)
            except Exception:
                pass 

            host_bytes = headers.get(b'host', b'')
            if not host_bytes:
                logger.error(f"🛑 [ASGI Shield] Missing Host header. IP: {client_ip}")
                await send({"type": "websocket.close", "code": 4000})
                return 
            
            host = host_bytes.decode('utf-8')
            
            query_string = scope.get('query_string', b'').decode('utf-8')
            query_params = urllib.parse.parse_qs(query_string)
            scope['device_token'] = query_params.get('token', [None])[0]
            scope['subprotocol'] = headers.get(b'sec-websocket-protocol', b'').decode('utf-8')
            
            tenant = await get_tenant_from_request(host)
            
            if tenant:
                scope['tenant'] = tenant
                scope['schema_name'] = tenant.schema_name
            else:
                scope['tenant'] = None
                scope['schema_name'] = 'public'
            
            return await self.inner(scope, receive, send)
            
        except Exception as e:
            logger.error(f"⚠️ [ASGI MIDDLEWARE ERROR] {e}")
            if scope.get('type') == 'websocket':
                await send({"type": "websocket.close", "code": 1011})
            return

# =====================================================================
# 👤 مصادقة الويب سوكيت داخل schema الفرع الصحيح
# =====================================================================
@database_sync_to_async
def _resolve_ws_user(scope):
    """يجيب المستخدم من schema الفرع — ومن غير ما يمسح جلسة المستخدم أبداً.

    🐛 [إصلاح جذري] `channels.auth.get_user` بيدوّر على المستخدم بالـ id
    المخزّن في الجلسة **في الـ schema النشط وقت التنفيذ**. مسار الويب سوكيت
    مبيعدّيش على TenantMainMiddleware، فالـ schema بيفضل `public` — و
    `auth_user` في TENANT_APPS، يعني id=1 في public ده مالك المنصة مش
    صاحب الجلسة. النتيجة: الـ session auth hash ما بيطابقش، فـ channels
    بيعمل `session.flush()` **فيمسح صفّ الجلسة من الداتابيز**.

    وساعتها بيحصل الآتي للمستخدم على الموقع العادي: القراءة شغالة (الجلسة
    لسه في كاش Redis بمفتاح مسبوق باسم schema الفرع، والـ flush مسح مفتاح
    public بس)، لكن أول طلب بيكتب في الجلسة (تبديل الفرع مثلاً) بيفشل الـ
    UPDATE → SessionInterrupted → صفحة "Bad Request (400)".

    الإصلاح هنا حاجتين:
      1. البحث عن المستخدم جوه `schema_context(schema_name)` — فبيتلاقى صح.
      2. عدم عمل flush خالص لو الهاش ما طابقش — بنرجّع زائر مجهول وبس.
         الجلسة مش ملك الويب سوكيت عشان يمسحها؛ لو فيها مشكلة حقيقية
         (تغيير باسورد مثلاً) فـ AuthenticationMiddleware على مسار الـ HTTP
         هيتصرّف فيها في الـ schema الصح.
    """
    from django.conf import settings as _settings
    from django.contrib.auth import (
        BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, load_backend,
    )
    from django.contrib.auth.models import AnonymousUser
    from django.utils.crypto import constant_time_compare
    from django_tenants.utils import schema_context

    anonymous = AnonymousUser()
    session = scope.get('session')
    if session is None:
        return anonymous

    try:
        user_id = session[SESSION_KEY]
        backend_path = session[BACKEND_SESSION_KEY]
    except KeyError:
        return anonymous
    if backend_path not in _settings.AUTHENTICATION_BACKENDS:
        return anonymous

    schema_name = scope.get('schema_name') or 'public'
    try:
        with schema_context(schema_name):
            from django.contrib.auth import get_user_model
            user_id = get_user_model()._meta.pk.to_python(user_id)
            user = load_backend(backend_path).get_user(user_id)
            if user is None:
                return anonymous
            session_hash = session.get(HASH_SESSION_KEY)
            if not (session_hash and constant_time_compare(
                    session_hash, user.get_session_auth_hash())):
                return anonymous
            return user
    except Exception as e:  # noqa: BLE001 — مصادقة WS ما تكسرش الاتصال
        logger.warning(f"⚠️ [WS AUTH] failed for schema {schema_name}: {e}")
        return anonymous


class TenantAwareAuthMiddleware(AuthMiddleware):
    """نفس AuthMiddleware بس بيحلّ المستخدم في schema الفرع (شوف الدالة فوق)."""

    async def resolve_scope(self, scope):
        scope["user"]._wrapped = await _resolve_ws_user(scope)


def TenantAuthMiddlewareStack(inner):
    # نفس ترتيب AuthMiddlewareStack (كوكيز → جلسة → مستخدم) بس بمصادقة
    # واعية بالفرع بدل النسخة الافتراضية اللي بتشتغل على public.
    return TenantAuthMiddleware(
        CookieMiddleware(ChannelsSessionMiddleware(TenantAwareAuthMiddleware(inner)))
    )

# =====================================================================
# 🧬 بروتوكول دورة حياة السيرفر (Lifespan)
# =====================================================================
async def lifespan_application(scope, receive, send):
    from django.db import close_old_connections
    while True:
        try:
            message = await receive()
        except Exception:
            return

        if message['type'] == 'lifespan.startup':
            try:
                await database_sync_to_async(close_old_connections)()
                await send({'type': 'lifespan.startup.complete'})
            except Exception as e:
                await send({'type': 'lifespan.startup.failed', 'message': str(e)})
        
        elif message['type'] == 'lifespan.shutdown':
            try:
                await database_sync_to_async(close_old_connections)()
                await send({'type': 'lifespan.shutdown.complete'})
            except Exception as e:
                await send({'type': 'lifespan.shutdown.failed', 'message': str(e)})
            return

# =====================================================================
# 🚦 موجه البروتوكولات المركزي (MAS Brain Router)
# =====================================================================
from channels.generic.websocket import AsyncWebsocketConsumer

class MockConsumer(AsyncWebsocketConsumer):
    async def connect(self): 
        await self.accept()
        
    async def receive(self, text_data=None, bytes_data=None):
        try:
            if text_data:
                # 🚀 تم حل مشكلة الـ json بفضل الاستدعاء بالأعلى
                data = json.loads(text_data)
                await self.send(text_data=json.dumps({
                    "status": "acknowledged", 
                    "trace_id": self.scope.get('trace_id')
                }))
        except Exception as e:
            logger.error(f"⚠️ [MOCK CONSUMER ERR] Payload exception: {e}")

# 🔧 Smart Diagnostics — live telemetry routes
from smart_diagnostics.routing import websocket_urlpatterns as _diag_ws_patterns

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        TenantAuthMiddlewareStack(
            URLRouter([
                path("ws/bidding/live/", MockConsumer.as_asgi()),
                path("ws/dashboard/sync/", MockConsumer.as_asgi()),
                path("ws/telemetry/obd2/", MockConsumer.as_asgi()),
                path("ws/notifications/", MockConsumer.as_asgi()),
                *_diag_ws_patterns,
            ])
        )
    ),
    "lifespan": lifespan_application,
})