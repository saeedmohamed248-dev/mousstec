from django.apps import AppConfig
from django.core.checks import register, Tags, Error
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


class ClientsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'clients'
    verbose_name = _('🏢 الإدارة المركزية (Mouss Tec Ecosystem)')

    # =====================================================================
    # 🚀 مفتاح الكونتاكت: إقلاع السيرفر واشتعال المحرك
    # =====================================================================
    def ready(self):
        # التأكد من عدم تشغيل الـ Watchdog أثناء عمليات الميجريشن أو الجرد اليدوي لتوفير الموارد
        active_servers = ['runserver', 'gunicorn', 'uvicorn', 'daphne']
        if not any(server in sys.argv[0] or server in sys.argv for server in active_servers):
            return

        # 1. 🔗 ربط الإشارات (Signals) السحرية لتفعيل أتمتة الـ Onboarding الفورية
        try:
            import clients.signals
            logger.info("🟢 Mouss Tec Core: Tenant Provisioning Signals connected successfully.")
        except ImportError:
            logger.warning("⚠️ Mouss Tec Core: Signals file not found or failed to load.")

        # 2. 🚀 نبض النظام: تشغيل رادار مراقبة السوق والمزادات المجمّدة
        watchdog_thread = threading.Thread(target=self.ecosystem_watchdog, daemon=True)
        watchdog_thread.start()

        logger.info("🚀 Mouss Tec Central Command & Multi-Agent Matrix is FULLY OPERATIONAL.")

    # =====================================================================
    # 🧠 الابتكارات الحصرية الشاملة (Live Ecosystem Watchdog Agent)
    # =====================================================================
    def ecosystem_watchdog(self):
        """
        🧠 رادار المراقبة السحابي:
        يمسح سوق الـ B2B، يلتقط المزادات المنتهية، ويقوم باستدعاء وتفعيل وكلاء الـ AI 
        الخلفية ذرياً لتنفيذ الترسية التلقائية وحقن محافظ الـ Escrow ماليًا.
        """
        import time
        from django.utils import timezone
        from django.db import close_old_connections
        from django.db.models import Count
        from django.core.cache import cache
        from django_tenants.utils import schema_context
        from celery import current_app # استدعاء نواة الكرفان لربط البوتات
        
        # انتظار 15 ثانية آمنة حتى يكتمل بناء طبقات الـ Network والـ DB Pools بالكامل
        time.sleep(15) 
        logger.info("🛡️ Watchdog: Mouss Tec Escrow & Bidding radar is now active and monitoring.")
        
        while True:
            try:
                # 🛑 تنظيف الـ DB Connections الميتة بكل لفة لحماية رامات السيرفر من الـ Memory Leaks
                close_old_connections()
                
                # 🛡️ ابتكار: جدار القفل الموزع (Distributed Lock Pattern)
                # يضمن الحماية التامة من الـ Race Conditions؛ سيرفر واحد فقط ينفذ الدورة حتى لو رفعنا الـ Cluster لـ 100 سيرفر
                lock_acquired = cache.add('mousstec_watchdog_lock', 'locked', 50) 
                
                if lock_acquired:
                    # 💓 تسجيل نبض الحياة (Heartbeat) في الـ Cache لتمكين لوحات الـ Devops من مراقبة صحة الرادار
                    cache.set('mousstec_watchdog_heartbeat', timezone.now(), 120)
                    
                    from .models import BlindBiddingRequest
                    
                    with schema_context('public'):
                        current_time = timezone.now()
                        
                        # جلب كافة طلبات الشراء والمزادات المفتوحة التي تخطت تاريخ الصلاحية
                        expired_bids = BlindBiddingRequest.objects.annotate(
                            offers_count=Count('offers')
                        ).filter(
                            status='open', 
                            expires_at__lte=current_time
                        )

                        cancelled_count = 0
                        awarding_count = 0

                        for bid in expired_bids:
                            if bid.offers_count > 0:
                                # تغيير الحالة إلى جاري الترسية لمنع التقاط المزاد مرتين
                                bid.status = 'awarding'
                                bid.save(update_fields=['status'])
                                awarding_count += 1
                                
                                # 🚀 🚀 الاندماج الحقيقي (Full Agent Integration):
                                # استدعاء بوت الـ AI المسؤول عن فلترة العروض، تقييم الـ Match Score، 
                                # اختيار الفائز، وتجميد الأموال في حساب الضمان (Escrow) آلياً وبدون Blocking للسيستم
                                try:
                                    current_app.send_task('clients.tasks.process_ai_bidding_award', args=[bid.id])
                                    logger.info(f"🤖 [AI DISPATCHER]: Triggered AI Forensics Awarding Agent for Auction #{bid.id}")
                                except Exception as celery_err:
                                    logger.error(f"🔴 [AI DISPATCHER ERROR]: Failed to dispatch Celery worker for Auction #{bid.id} - {celery_err}")
                            else:
                                # إذا انتهى وقت المزاد ولم يتقدم أي تاجر بـ Offer، يتم الإلغاء فوراً ورد السيستم للوضعية الطبيعية
                                bid.status = 'cancelled'
                                bid.save(update_fields=['status'])
                                cancelled_count += 1
                        
                        # تدوين الأنشطة في الـ Audit Log المالي للمنصة عند حدوث حركات تشغيلية
                        if cancelled_count > 0 or awarding_count > 0:
                            logger.info(
                                f"⚖️ [WATCHDOG RADAR]: Cycle executed successfully. "
                                f"Auto-cancelled {cancelled_count} dead auctions | "
                                f"Dispatched AI Agents to award {awarding_count} active auctions."
                            )

            except Exception as e:
                logger.error(f"🔴 [WATCHDOG FATAL ERROR]: Core loop interrupted - {e}")
            
            finally:
                close_old_connections()
            
            # الراحة التكتيكية لمدة دقيقة كاملة قبل بدء مسح الشبكة من جديد
            time.sleep(60)