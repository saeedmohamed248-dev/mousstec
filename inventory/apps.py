from django.apps import AppConfig
from django.core.checks import register, Tags, Warning, Error
from django.utils.translation import gettext_lazy as _ 
import logging
import sys
import threading 

# 🟢 تهيئة مسجل الأحداث بهوية Mouss Tec الموحدة
logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🛡️ 1. نظام الفحص الذاتي المتقدم (Enterprise System Checks)
# =====================================================================
@register(Tags.security, Tags.compatibility)
def check_enterprise_configuration(app_configs, **kwargs):
    """
    نظام تشخيص استباقي يراقب إعدادات خادم الـ SaaS ويمنع تشغيل السيستم 
    في بيئة الإنتاج إذا وجدت أخطاء كافية لتعطيل البوتات الحية.
    """
    errors = []
    from django.conf import settings
    
    # 1. 🛡️ حارس المعمارية المعزولة (Tenant Architecture Guard)
    if hasattr(settings, 'TENANT_APPS') and 'inventory' not in settings.TENANT_APPS:
        errors.append(
            Error(
                '🏢 خطأ معماري خطير: تطبيق المخزون والورشة (inventory) يغرد خارج السرب!',
                hint='تأكد من إضافة "inventory" داخل مصفوفة TENANT_APPS في ملف settings.py لمنع تداخل وبيانات الفروع.',
                id='mousstec.E002',
            )
        )

    # 2. 🔗 رادار الربط مع السوق المركزي (Mouss Tec Core Connectivity)
    if hasattr(settings, 'SHARED_APPS') and 'clients' not in settings.SHARED_APPS:
        errors.append(
            Error(
                '🌐 خطأ في العبور السحابي: انقطاع الاتصال بالسوق المركزي للمنصة!',
                hint='تطبيق "clients" غير موجود في SHARED_APPS. الورش لن تتمكن من الدخول لغرف الـ Blind Bidding.',
                id='mousstec.E003',
            )
        )

    # 3. ⚡ فحص محرك الاتصالات الحية (WebSockets Engine Check)
    if 'daphne' not in settings.INSTALLED_APPS:
        errors.append(
            Warning(
                '⚠️ محرك الاتصالات اللحظية Daphne غير مفعل بصدر لوحة التحكم.',
                hint='بدون دمج Daphne في مقدمة التطبيقات، لن تعمل غرف المزادات العكسية أو مزامنة الـ POS لايف.',
                id='mousstec.W003',
            )
        )

    # 4. 🧠 فحص كفاءة كاش الخزائن (Two-Tier Cache Diagnostics)
    cache_backend = settings.CACHES.get('default', {}).get('BACKEND', '')
    if 'LocMemCache' in cache_backend:
        errors.append(
            Warning(
                '⚠️ النظام يعمل بذاكرة كيش محلية ضعيفة (LocMemCache) كـ محرك أساسي.',
                hint='يُنصح بشدة تفعيل Redis في بيئة الإنتاج الفعلي لتشغيل كاش السوق المشترك ومزامنة مسدس الباركود.',
                id='mousstec.W001',
            )
        )

    # 5. 📧 فحص محرك التحصيل والبريد الإلكتروني (FinTech Notification Check)
    if not getattr(settings, 'EMAIL_HOST_USER', None) or settings.EMAIL_HOST_USER == 'your-email@gmail.com':
        errors.append(
            Warning(
                '📧 محرك إشعارات البريد الإلكتروني المحاسبي غير مبرمج بالبيئة.',
                hint='لن يتلقى التجار إشعارات بترسية المزادات عليهم وتحرير أموال الـ Escrow حتى يتم ضبط أسرار الـ SMTP.',
                id='mousstec.W002',
            )
        )

    # 6. 🤖 فحص رادار ومفاتيح مستشار الذكاء الاصطناعي (AI Copilot Activation)
    if getattr(settings, 'ENABLE_AI_PREDICTIONS', False):
        if not getattr(settings, 'AI_VISION_API_KEY', ''):
            errors.append(
                Error(
                    '🤖 ميزة مستشار الذكاء الاصطناعي مفعلة بالإعدادات ولكن المفتاح السري السيادي مفقود!',
                    hint='يرجى وضع مفتاح الـ API المعتمد لـ Gemini AI داخل ملف الـ .env المخفي.',
                    id='mousstec.E001',
                )
            )
            
    # 7. 🚀 فحص محرك المهام غير المتزامنة (Celery Queue Sync)
    if not getattr(settings, 'CELERY_BROKER_URL', None):
        errors.append(
            Warning(
                '⚙️ طابور المهام الخلفية Celery غير متصل برابط السيرفر الموزع.',
                hint='بدون اتصال Celery Broker، ستتم معالجة تقارير الـ AI الثقيلة بشكل متزامن مما قد يسبب خنق السيرفر الأساسي.',
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
        # منع اشتعال الرادارات أثناء عمليات التأسيس الهيكلي (Migrations) أو أوامر الجرد النصي
        active_servers = ['runserver', 'gunicorn', 'uvicorn', 'daphne']
        if not any(server in sys.argv[0] or server in sys.argv for server in active_servers):
            return

        # 1. 🔗 ربط نظام الإشارات (Signals) لتنفيذ القيود المحاسبية وحماية هوامش الربح
        try:
            import inventory.signals
            logger.info("🟢 Mouss Tec Engine: Inventory Framework Signals connected successfully.")
        except ImportError:
            logger.warning("⚠️ Mouss Tec Engine: Signals bridge failed to initialize automatically.")

        # 2. 🔥 إطلاق محرك الذكاء الاصطناعي والتسخين الاستباقي للـ POS في الخلفية
        warmup_thread = threading.Thread(target=self.smart_inventory_engine, daemon=True)
        warmup_thread.start()

        logger.info("🚀 Mouss Tec Inventory Command and Cache Warmup Engine is ONLINE.")

    # =====================================================================
    # 🧠 الابتكارات الحصرية الشاملة (Distributed Cache Warming Engine)
    # =====================================================================
    def smart_inventory_engine(self):
        """
        🚀 محرك التسخين المسبق والمراقبة الاستباقية للمخازن المعزولة:
        يقوم بقراءة الأصناف الأعلى مبيعاً وحقنها في الـ Redis لتسريع استجابة شاشات الـ POS،
        معزز بـ Distributed Lock لمنع تعليق أو خنق الداتابيز عند تعدد الـ App Workers.
        """
        import time
        from django.core.cache import cache
        from django.db import close_old_connections
        from django_tenants.utils import schema_context
        
        # انتظار تكتيكي حامٍ لمدة 15 ثانية حتى تستقر قنوات الـ Connection Pools للسيرفر تماماً
        time.sleep(15) 
        
        try:
            # 🛡️ الحماية من الـ Thundering Herd Pattern بين الـ Gunicorn/Daphne Workers:
            # العامل الأول فقط الذي يقتنص القفل هو من يقوم بتحديث وتحميل الكاش الاستباقي للـ SaaS
            lock_acquired = cache.add('mousstec_catalog_warming_lock', 'active', 600) # قفل لمدة 10 دقائق
            if not lock_acquired:
                logger.info("📡 [CACHE WARMUP ENGINE]: Warmup loop bypassed. Already populated by another cluster node.")
                return

            close_old_connections() 
            
            # استدعاء داخلي مرن للموديلز لحماية السيرفر من الـ AppRegistryNotReady Error
            from clients.models import Client
            from inventory.models import Product
            from django.db.models import Count
            
            # 🚀 العبور بالسياق المعماري للنطاق العام public لجلب قائمة الشركات المعتمدة بالنظام
            with schema_context('public'):
                active_tenants = list(Client.objects.filter(schema_name__isnull=False, is_active=True).exclude(schema_name='public'))
            
            warmed_count = 0
            for tenant in active_tenants:
                # عزل كامل لكل ورشة/شركة على حدة بناءً على الـ Tenant Schema المخصصة لها
                with schema_context(tenant.schema_name):
                    # جلب الـ Top 50 صنفاً الأكثر حركة ومبيعاً لتسخين الكاش السريع الخاص بنقاط البيع (POS)
                    top_products = Product.objects.annotate(
                        sales_count=Count('saleinvoiceitem')
                    ).filter(sales_count__gt=0).order_by('-sales_count')[:50]
                    
                    if top_products.exists():
                        cache_key = f"{tenant.schema_name}:pos_fast_catalog"
                        product_data = [
                            {
                                "id": p.id, 
                                "name": p.name, 
                                "part_number": p.part_number, 
                                "price": float(p.retail_price or 0)
                            } for p in top_products
                        ]
                        
                        # ترحيل وحقن البيانات المجهزة في طبقة كاش الـ Redis الموزع لمدة 12 ساعة كاملة
                        cache.set(cache_key, product_data, timeout=43200)
                        warmed_count += 1
                    
            if warmed_count > 0:
                logger.info(f"🔥 [WARMUP ENGINE SUCCESS]: Multi-Tenant POS Fast-Catalog populated for {warmed_count} active domains.")

        except Exception as e:
            logger.error(f"🔴 [WARMUP ENGINE CRITICAL FAILURE]: Operation aborted - {e}")
        finally:
            # 🛡️ إغلاق وقفل القنوات المحلي بعد انتهاء المعالجة حمايةً للسيرفر من الـ Connection Leak
            close_old_connections()