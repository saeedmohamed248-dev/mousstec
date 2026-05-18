from django.apps import AppConfig
from django.core.checks import register, Tags, Error
from django.utils.translation import gettext_lazy as _
import logging
import sys
import threading

# تهيئة رادار المراقبة المركزي
logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 🛡️ 1. الفحوصات الذاتية للأمان (Global System Checks)
# =====================================================================
@register(Tags.security)
def check_tenant_architecture(app_configs, **kwargs):
    """
    دالة هندسية تقوم بفحص معمارية النظام وتمنع الأخطاء البشرية الكارثية.
    """
    errors = []
    from django.conf import settings
    
    if hasattr(settings, 'SHARED_APPS') and 'clients' not in settings.SHARED_APPS:
        errors.append(
            Error(
                '🏢 خطأ معماري خطير: تطبيق الإدارة المركزية (clients) يغرد خارج السرب!',
                hint='تأكد من إضافة "clients" داخل مصفوفة SHARED_APPS في ملف settings.py لمنع انهيار الـ SaaS.',
                id='mousstec.E001',
            )
        )
    return errors


class ClientsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'clients'
    verbose_name = _('🏢 الإدارة المركزية (Mouss Tec Ecosystem)')

    # =====================================================================
    # 🚀 مفتاح الكونتاكت: إقلاع السيرفر
    # =====================================================================
    def ready(self):
        active_servers = ['runserver', 'gunicorn', 'uvicorn', 'daphne']
        if not any(server in sys.argv[0] or server in sys.argv for server in active_servers):
            return

        # 1. 🔗 ربط الإشارات (Signals) السحرية
        try:
            import clients.signals
            logger.info("🟢 Mouss Tec Core: Tenant Signals connected successfully.")
        except ImportError:
            pass

        # 2. 🚀 نبض النظام: تشغيل مراقب المزادات الآمن
        watchdog_thread = threading.Thread(target=self.ecosystem_watchdog, daemon=True)
        watchdog_thread.start()

        logger.info("🚀 Mouss Tec Central Command is FULLY OPERATIONAL.")

    # =====================================================================
    # 🧠 الابتكارات الحصرية (Exclusive SaaS Watchdog)
    # =====================================================================
    def ecosystem_watchdog(self):
        """
        🧠 ابتكار: محرك خلفي مزود بـ (Distributed Lock) لمنع التداخل بين الـ Workers،
        وفرز ذكي للمزادات المنتهية (إلغاء أو تحويل للترسية الآلية).
        """
        import time
        from django.utils import timezone
        from django.db import close_old_connections
        from django.db.models import Count
        from django.core.cache import cache
        from django_tenants.utils import schema_context
        
        # انتظار 15 ثانية حتى يكتمل إقلاع السيرفر بالكامل
        time.sleep(15) 
        logger.info("🛡️ Watchdog: Mouss Tec Escrow & Bidding radar is now active.")
        
        while True:
            try:
                # 🛑 تنظيف الاتصالات لمنع (Memory Leak)
                close_old_connections()
                
                # 🛡️ ابتكار: (Distributed Lock) 
                # يضمن أن Worker واحد فقط ينفذ هذا الكود كل 60 ثانية حتى لو كان لدينا 10 سيرفرات
                lock_acquired = cache.add('mousstec_watchdog_lock', 'locked', 50) 
                
                if lock_acquired:
                    # 💓 تسجيل نبض الحياة (Heartbeat) للمراقبة الخارجية
                    cache.set('mousstec_watchdog_heartbeat', timezone.now(), 120)
                    
                    from .models import BlindBiddingRequest
                    
                    with schema_context('public'):
                        current_time = timezone.now()
                        
                        # جلب المزادات المفتوحة التي انتهى وقتها
                        # (نستخدم timezone.now للحماية من أخطاء التوقيت المحلي)
                        expired_bids = BlindBiddingRequest.objects.annotate(
                            offers_count=Count('offers') # 👈 الاعتماد على جدول العروض الجديد
                        ).filter(
                            status='open', 
                            expires_at__lte=current_time
                        )

                        cancelled_count = 0
                        awarding_count = 0

                        # 🧠 ابتكار: التوجيه الذكي (Smart Routing)
                        for bid in expired_bids:
                            if bid.offers_count > 0:
                                # إذا كان هناك عروض متقدمة -> حوّله للترسية
                                bid.status = 'awarding'
                                awarding_count += 1
                            else:
                                # إذا لم يتقدم أحد -> يتم الإلغاء
                                bid.status = 'cancelled'
                                cancelled_count += 1
                            
                            # حفظ التغييرات
                            bid.save(update_fields=['status'])
                        
                        # إعداد تقرير للوج في حالة وجود حركات
                        if cancelled_count > 0 or awarding_count > 0:
                            logger.info(
                                f"⚖️ Watchdog Cycle: "
                                f"[Time: {current_time.strftime('%H:%M:%S')}] | "
                                f"Auto-cancelled {cancelled_count} bids | "
                                f"Moved {awarding_count} bids to Awarding stage."
                            )

            except Exception as e:
                logger.error(f"🔴 Watchdog Engine Error: {e}")
            
            finally:
                close_old_connections()
            
            # راحة 60 ثانية قبل الدورة القادمة
            time.sleep(60)