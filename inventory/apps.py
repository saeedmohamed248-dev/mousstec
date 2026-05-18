from django.apps import AppConfig
from django.core.checks import register, Tags, Warning, Error
from django.utils.translation import gettext_lazy as _ 
import logging
import sys
import threading 

# 🟢 تهيئة مسجل الأحداث (Logger) بهوية Mouss Tec
logger = logging.getLogger('mousstec_inventory')

# =====================================================================
# 🛡️ 1. نظام الفحص الذاتي المتقدم (Enterprise System Checks)
# 🚀 ابتكار: تم نقله خارج الكلاس ليتوافق مع معايير جانجو الصارمة ويمنع تسريب الذاكرة
# =====================================================================
@register(Tags.security, Tags.compatibility)
def check_enterprise_configuration(app_configs, **kwargs):
    errors = []
    from django.conf import settings
    
    # 1. 🛡️ حارس العمارة السحابية (Tenant Architecture Guard)
    if hasattr(settings, 'TENANT_APPS') and 'inventory' not in settings.TENANT_APPS:
        errors.append(
            Error(
                '🏢 خطأ معماري خطير: تطبيق المخزون يغرد خارج السرب!',
                hint='تأكد من إضافة "inventory" داخل مصفوفة TENANT_APPS في ملف settings.py لمنع تسريب البيانات بين الفروع.',
                id='mousstec.E002',
            )
        )

    # 2. 🔗 رادار الربط مع السوق المركزي (Mouss Tec Core Connectivity)
    if hasattr(settings, 'SHARED_APPS') and 'clients' not in settings.SHARED_APPS:
        errors.append(
            Error(
                '🌐 انقطاع الاتصال بالسوق المركزي!',
                hint='تطبيق "clients" غير موجود في SHARED_APPS. الورش لن تتمكن من الدخول للمزاد العكسي.',
                id='mousstec.E003',
            )
        )

    # 3. ⚡ فحص محرك الاتصالات الحية (WebSockets Engine Check)
    if 'daphne' not in settings.INSTALLED_APPS:
        errors.append(
            Warning(
                '⚠️ محرك Daphne غير مفعل.',
                hint='بدون Daphne، لن تعمل المزادات العكسية (Blind Bidding) بشكل لحظي.',
                id='mousstec.W003',
            )
        )

    # 4. 🧠 فحص كفاءة الذاكرة (Cache)
    cache_backend = settings.CACHES.get('default', {}).get('BACKEND', '')
    if 'LocMemCache' in cache_backend:
        errors.append(
            Warning(
                '⚠️ النظام يعمل بذاكرة كيش محلية ضعيفة (LocMemCache).',
                hint='يُنصح بشدة تفعيل Redis لضمان سرعة سوق التجار العام وتسخين بيانات الـ POS.',
                id='mousstec.W001',
            )
        )

    # 5. 📧 فحص محرك البريد الإلكتروني
    if not getattr(settings, 'EMAIL_HOST_USER', None) or settings.EMAIL_HOST_USER == 'your-email@gmail.com':
        errors.append(
            Warning(
                '📧 محرك البريد الإلكتروني غير مهيأ بشكل كامل.',
                hint='لن يتلقى التجار إشعارات بترسية المزادات عليهم (Escrow Alerts) حتى يتم ضبط الإيميل.',
                id='mousstec.W002',
            )
        )

    # 6. 🤖 فحص نبض الذكاء الاصطناعي (AI Endpoint Check)
    if getattr(settings, 'ENABLE_AI_PREDICTIONS', False):
        ai_url = getattr(settings, 'AI_MODEL_ENDPOINT', '')
        if not getattr(settings, 'AI_VISION_API_KEY', ''):
            errors.append(
                Error(
                    '🤖 ميزة الذكاء الاصطناعي (مترجم الأعطال) مفعلة ولكن المفتاح السري مفقود!',
                    hint='ضع مفتاح الـ API الخاص بـ Gemini AI في ملف .env.',
                    id='mousstec.E001',
                )
            )
            
    # 7. 🚀 فحص محرك المهام الموزعة (Celery - للذكاء الاصطناعي والتقارير)
    if not getattr(settings, 'CELERY_BROKER_URL', None):
        errors.append(
            Warning(
                '⚙️ محرك Celery غير متصل.',
                hint='بدون Celery Broker، ستتم معالجة مهام الـ AI بشكل متزامن مما سيبطئ النظام.',
                id='mousstec.W004',
            )
        )

    return errors


# =====================================================================
# 📦 2. إعدادات تطبيق المخزون والمحرك الخلفي (App Config)
# =====================================================================
class InventoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'inventory'
    verbose_name = _('📦 إدارة المخزن والورشة (Mouss Tec Engine)')

    def ready(self):
        # ⚡ التعرف على خوادم الـ ASGI / WSGI الفعلية لمنع تكرار التشغيل أثناء הـ Migrations
        active_servers = ['runserver', 'gunicorn', 'uvicorn', 'daphne']
        if not any(server in sys.argv[0] or server in sys.argv for server in active_servers):
            return

        # 1. 🔗 ربط نظام الإشارات (Signals)
        try:
            import inventory.signals
            logger.info("🟢 Mouss Tec Engine: Field Signals connected successfully.")
        except ImportError:
            pass # تم الربط بالفعل في أماكن أخرى

        # 2. 🔥 إطلاق محرك الذكاء الاصطناعي والتسخين (Smart Background Engine)
        warmup_thread = threading.Thread(target=self.smart_inventory_engine, daemon=True)
        warmup_thread.start()

        logger.info("🚀 Mouss Tec Inventory Engine is FULLY OPERATIONAL.")

    # =====================================================================
    # 🧠 الابتكارات الحصرية (Exclusive SaaS Logic)
    # =====================================================================
    def smart_inventory_engine(self):
        """
        🚀 ابتكار عالمي: محرك التسخين المسبق والمراقبة (True Multi-Tenant Engine).
        يقرأ الـ Top 50 صنف لكل شركة ויخزنهم في הـ Redis لتسريع نقاط البيع (POS).
        """
        import time
        from django.core.cache import cache
        from django.db import close_old_connections
        from django_tenants.utils import schema_context
        
        # انتظار 15 ثانية لضمان استقرار السيرفر وقواعد البيانات تماماً بعد الإقلاع
        time.sleep(15) 
        
        try:
            close_old_connections() # 🛡️ حماية من استنزاف اتصالات הדاتا بيز (Connection Leak)
            
            # استيراد النماذج بالداخل لتجنب مشكلة (AppRegistryNotReady)
            from clients.models import Client
            from inventory.models import Product
            from django.db.models import Count
            
            # جلب כל الشركات النشطة الفعالة فقط
            active_tenants = Client.objects.filter(schema_name__isnull=False, is_active=True).exclude(schema_name='public')
            
            warmed_count = 0
            for tenant in active_tenants:
                with schema_context(tenant.schema_name):
                    # 💡 التسخين الفعلي لبيانات הـ POS (Cache Warming)
                    # جلب أكثر 50 صنف مبيعاً في هذه الشركة تحديداً
                    top_products = Product.objects.annotate(
                        sales_count=Count('saleinvoiceitem')
                    ).filter(sales_count__gt=0).order_by('-sales_count')[:50]
                    
                    if top_products.exists():
                        cache_key = f"{tenant.schema_name}:pos_fast_catalog"
                        product_data = [{"id": p.id, "name": p.name, "part_number": p.part_number, "price": float(p.retail_price or 0)} for p in top_products]
                        
                        # التخزين في הـ Redis لمدة 12 ساعة
                        cache.set(cache_key, product_data, timeout=43200)
                        warmed_count += 1
                    
                    # 🤖 2. رادار النواقص الذكي (Auto-Restock Watchdog Foundation)
                    # (مساحة مخصصة لإطلاق Celery Tasks مستقبلاً لجرد المخازن ليلياً)

            logger.info(f"🔥 Smart Inventory Engine [SUCCESS]: Multi-Tenant POS Cache Warmed Up for {warmed_count} active tenants.")

        except Exception as e:
            logger.error(f"🔴 Smart Inventory Engine [FAILED]: {e} - Will retry on next boot.")
        finally:
            # 🛡️ إغلاق الاتصال بأمان بعد انتهاء الـ Thread لمنع תעليق הـ DB
            close_old_connections()