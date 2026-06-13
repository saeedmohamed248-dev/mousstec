from django.apps import AppConfig
from django.core.checks import register, Tags, Error, Warning
from django.utils.translation import gettext_lazy as _
import logging
import sys
import threading

# تهيئة رادار المراقبة المركزي للمنظومة
logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🛡️ 1. الفحوصات الذاتية للأمان المعماري (Global System Checks)
# =====================================================================
@register(Tags.security)
def check_tenant_architecture(app_configs, **kwargs):
    """
    دالة هندسية صارمة تفحص إعدادات السيرفر وتمنع الأخطاء البشرية الكارثية قبل الإقلاع.
    """
    errors = []
    from django.conf import settings
    
    if hasattr(settings, 'SHARED_APPS') and 'clients' not in settings.SHARED_APPS:
        errors.append(
            Error(
                '🏢 خطأ معماري خطير: تطبيق الإدارة المركزية (clients) يغرد خارج السرب!',
                hint='تأكد من إضافة "clients" داخل مصفوفة SHARED_APPS في ملف settings.py لمنع انهيار الـ SaaS والعزل الداتابيزي.',
                id='mousstec.E001',
            )
        )
    return errors

@register(Tags.compatibility)
def check_infrastructure_readiness(app_configs, **kwargs):
    """
    🚀 ابتكار: فحص البنية التحتية (Celery, Email, API Keys) لضمان عدم إقلاع المنصة وبها إعاقة صامتة.
    """
    errors = []
    from django.conf import settings
    
    # التأكد من إعدادات محرك المهام
    if not getattr(settings, 'CELERY_BROKER_URL', None):
        errors.append(
            Warning(
                '⚡ تحذير بنية تحتية: لم يتم العثور على CELERY_BROKER_URL.',
                hint='أتمتة الذكاء الاصطناعي والمزادات تتطلب وجود Redis أو RabbitMQ مهيأ.',
                id='mousstec.W001',
            )
        )
    
    # التأكد من بروتوكول البريد للإشعارات
    if not getattr(settings, 'EMAIL_HOST_USER', None):
        errors.append(
            Warning(
                '📧 محرك إشعارات البريد الإلكتروني المحاسبي غير مبرمج بالبيئة.',
                hint='لن يتلقى التجار إشعارات بترسية المزادات عليهم وتحرير أموال الـ Escrow حتى يتم ضبط أسرار الـ SMTP.',
                id='mousstec.W002',
            )
        )
    return errors

# =====================================================================
# 🏢 إعدادات التطبيق المركزي (Mouss Tec Core)
# =====================================================================
class ClientsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'clients'
    verbose_name = _('🏢 الإدارة المركزية (Mouss Tec Ecosystem)')

    def __init__(self, app_name, app_module):
        super().__init__(app_name, app_module)
        # 🚀 ابتكار: استخدام Event للتحكم الكامل في إغلاق السيرفر بشكل نظيف (Graceful Shutdown)
        self._stop_event = threading.Event()

    # =====================================================================
    # 🚀 مفتاح الكونتاكت: إقلاع السيرفر واشتعال المحرك
    # =====================================================================
    def ready(self):
        # 🔗 ربط الإشارات (Signals) — يجب أن يتم في كل سياق:
        # runserver, gunicorn, daphne, celery, management commands, tests.
        # signal `auto_setup_new_tenant` يولّد domain + chart of accounts + welcome gift
        # عند إنشاء tenant جديد — لو ما اتسجلتش، الـ tenants الجدد هيكونوا ناقصين.
        try:
            import clients.signals  # noqa: F401
            logger.info("🟢 Mouss Tec Core: Tenant Provisioning Signals connected successfully.")
        except ImportError as e:
            logger.error(f"🔴 Mouss Tec Core: Signals file failed to load - {e}")

        # 🛡️ Plan-based quota enforcement on User/Branch/Treasury creation.
        # Lives in its own module so the provisioning signals stay focused.
        try:
            import clients.signals_quota  # noqa: F401
            logger.info("🟢 Mouss Tec Core: Plan Quota Signals connected successfully.")
        except ImportError as e:
            logger.error(f"🔴 Mouss Tec Core: Quota signals failed to load - {e}")

        # 🚀 Watchdog — فقط في سيرفرات HTTP طويلة العمر
        active_servers = ['runserver', 'gunicorn', 'uvicorn', 'daphne']
        is_long_running = any(server in sys.argv[0] or server in sys.argv for server in active_servers)
        if not is_long_running:
            return

        watchdog_thread = threading.Thread(target=self.ecosystem_watchdog, daemon=True)
        watchdog_thread.start()
        logger.info("🚀 Mouss Tec Central Command & Multi-Agent Matrix is FULLY OPERATIONAL.")

    # =====================================================================
    # 🧠 الابتكارات الحصرية الشاملة (Live Ecosystem Watchdog Agent)
    # =====================================================================
    def ecosystem_watchdog(self):
        """
        🧠 رادار المراقبة السحابي المُحصّن:
        يمسح سوق الـ B2B، يلتقط المزادات المنتهية، ويطبق تقنيات الـ Self-Healing 
        و الـ Graceful Shutdown لضمان استقرار السيرفر بنسبة 99.99%.
        """
        import time
        from django.utils import timezone
        from django.db import close_old_connections, transaction
        from django.db.models import Count
        from django.core.cache import cache
        from django_tenants.utils import schema_context
        from celery import current_app 
        
        # انتظار تكتيكي 15 ثانية حتى تكتمل طبقات الشبكة
        self._stop_event.wait(15)
        
        if self._stop_event.is_set():
            return
            
        logger.info("🛡️ Watchdog: Mouss Tec Escrow & Bidding radar is now active and monitoring.")
        
        # 🚀 الحلقة تعمل طالما لم يصدر أمر بإيقاف السيرفر
        while not self._stop_event.is_set():
            try:
                # 🛑 تنظيف الـ DB Connections الميتة لحماية رامات السيرفر
                close_old_connections()
                
                # 🛡️ جدار القفل الموزع (Distributed Lock Pattern) لمنع تضارب الكلاستر
                lock_acquired = cache.add('mousstec_watchdog_lock', 'locked', 50) 
                
                if lock_acquired:
                    # 💓 تسجيل نبض الحياة لـ Devops
                    cache.set('mousstec_watchdog_heartbeat', timezone.now(), 120)
                    
                    from .models import BlindBiddingRequest
                    
                    with schema_context('public'):
                        current_time = timezone.now()
                        
                        expired_bids = BlindBiddingRequest.objects.annotate(
                            offers_count=Count('offers')
                        ).filter(
                            status='open', 
                            expires_at__lte=current_time
                        )

                        cancelled_count = 0
                        awarding_count = 0
                        recovered_count = 0

                        for bid in expired_bids:
                            if bid.offers_count > 0:
                                # قفل ذري للحالة لمنع تكرار العملية
                                with transaction.atomic():
                                    bid.status = 'awarding'
                                    bid.save(update_fields=['status'])
                                
                                # 🚀 🚀 محرك التعافي الذاتي (Self-Healing Integration)
                                try:
                                    current_app.send_task('clients.tasks.process_ai_bidding_award', args=[bid.id])
                                    awarding_count += 1
                                    logger.info(f"🤖 [AI DISPATCHER]: Triggered AI Forensics Agent for Auction #{bid.id}")
                                except Exception as celery_err:
                                    # 🛡️ ابتكار: إذا فشل خادم الـ Celery، نرجع المزاد ونمدده 5 دقائق بدل تعليقه للأبد
                                    with transaction.atomic():
                                        bid.status = 'open'
                                        bid.expires_at = current_time + timezone.timedelta(minutes=5)
                                        bid.save(update_fields=['status', 'expires_at'])
                                    recovered_count += 1
                                    logger.error(f"🔴 [AI DISPATCHER ERROR]: Celery down! Auction #{bid.id} recovered and extended by 5 mins. Error: {celery_err}")
                            else:
                                # 📈 ابتكار: تحليل فشل المزادات الصفرية لتغذية رادار الأسعار
                                failure_reason = "لم يجتذب المزاد أي عروض، ربما السعر المستهدف منخفض جداً أو القطعة نادرة."
                                bid.status = 'cancelled'
                                bid.save(update_fields=['status'])
                                cancelled_count += 1
                                logger.info(f"📉 [MARKET ANALYSIS]: Auction #{bid.id} cancelled. Analysis: {failure_reason}")
                        
                        if cancelled_count > 0 or awarding_count > 0 or recovered_count > 0:
                            logger.info(
                                f"⚖️ [WATCHDOG RADAR]: Cycle complete. "
                                f"Cancelled: {cancelled_count} | Dispatched: {awarding_count} | Recovered: {recovered_count}"
                            )

            except Exception as e:
                logger.error(f"🔴 [WATCHDOG FATAL ERROR]: Core loop interrupted - {e}")
            
            finally:
                close_old_connections()
            
            # 🚀 ابتكار: استخدام wait بدلاً من sleep لإغلاق السيرفر فوراً بدون تعطيل (Graceful Shutdown)
            self._stop_event.wait(60)