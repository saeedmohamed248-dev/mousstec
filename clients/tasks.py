from celery import shared_task
from django.utils import timezone
from clients.models import Client
import logging

# تهيئة رادار المراقبة
logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 📨 1. وكيل الترحيب الذكي (AI Onboarding Dispatcher)
# =====================================================================
@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def async_welcome_bot_task(self, client_name, client_phone, business_type, full_login_url, admin_user, admin_pass):
    """
    يتلقى الأوامر من الـ Provisioning Orchestrator (signals.py).
    مزود بخاصية إعادة المحاولة (Auto-Retry) في حال فشل بوابات الإرسال.
    """
    try:
        type_str = "مركز الصيانة" if business_type == 'service_center' else "تجارة قطع الغيار"
        
        msg = (
            f"مرحباً بك في Mouss Tec Ecosystem 🚀\n\n"
            f"تم تأسيس نظام {type_str} الخاص بك ({client_name}) بنجاح.\n\n"
            f"🌐 رابط لوحة التحكم: {full_login_url}\n"
            f"👤 اسم المستخدم: {admin_user}\n"
            f"🔑 كلمة المرور: {admin_pass}\n\n"
            f"ننصح بتغيير كلمة المرور فور الدخول. نتمنى لك أرباحاً هائلة!"
        )
        
        # 💡 [مكان دمج API الواتساب أو البريد الإلكتروني لاحقاً]
        # مثال: twilio_client.messages.create(...)
        
        logger.info(f"📨 [AI WELCOME BOT]: Credentials successfully generated and ready for {client_name} ({client_phone}).")
        # print(msg) # للعرض أثناء التطوير
        
    except Exception as exc:
        logger.warning(f"⚠️ [AI WELCOME BOT]: Delivery failed for {client_name}. Retrying... ({self.request.retries}/3)")
        raise self.retry(exc=exc)

# =====================================================================
# 💸 2. وكيل المراجعة المالية (The Gold Trap Enforcer)
# =====================================================================
@shared_task
def suspend_expired_trials():
    """
    رصد الحسابات التجريبية المنتهية وتعليقها ليلاً.
    معزول ضد الانهيارات (Fault-Tolerant Loop).
    """
    today = timezone.now().date()
    expired_tenants = Client.objects.filter(status='trial', trial_ends_at__lt=today, is_active=True)
    
    success_count = 0
    error_count = 0
    
    for tenant in expired_tenants:
        try:
            tenant.status = 'suspended'
            tenant.save(update_fields=['status'])
            logger.info(f"🔒 [CRON GUARD]: Tenant '{tenant.schema_name}' trial expired. Suspended safely.")
            success_count += 1
        except Exception as e:
            logger.error(f"🔴 [CRON GUARD ERROR]: Failed to suspend '{tenant.schema_name}' - {e}")
            error_count += 1
            continue # تخطي الخطأ وإكمال السلسلة
            
    return f"Expired Trials Cron: {success_count} suspended, {error_count} failed."

# =====================================================================
# 🤖 3. وكيل الثقة ومكافحة الاحتيال (AI Market Forensics Agent)
# =====================================================================
@shared_task
def update_market_trust_scores():
    """
    محرك تقييم التجار بالذكاء الاصطناعي:
    يحسب النقاط ويحظر النصابين. معزول لضمان تحديث كامل السوق.
    """
    tenants = Client.objects.filter(is_active=True)
    success_count = 0
    
    for tenant in tenants:
        try:
            base_score = 100
            
            # خصم نقاط بناءً على نسبة النزاعات (Penalty)
            if tenant.dispute_rate > 0:
                base_score -= int(tenant.dispute_rate * 2)
            
            # مكافأة الاستقرار (Bonus)
            if getattr(tenant, 'successful_deals', 0) > 50:
                base_score += 5
                
            tenant.ai_trust_score = max(min(base_score, 100), 1)
            
            # حظر آلي لحماية السوق (Auto-Ban)
            if tenant.ai_trust_score < 40 and not tenant.is_fraud_flagged:
                tenant.is_fraud_flagged = True
                logger.critical(f"🚨 [AI SHIELD]: Tenant '{tenant.schema_name}' AUTO-BANNED due to critical trust score ({tenant.ai_trust_score}).")
            
            tenant.save(update_fields=['ai_trust_score', 'is_fraud_flagged'])
            success_count += 1
            
        except Exception as e:
            logger.error(f"🔴 [AI SHIELD ERROR]: Failed to update trust score for '{tenant.schema_name}' - {e}")
            continue # لا توقف تقييم باقي السوق
            
    return f"AI Trust Orchestrator: Successfully synchronized scores for {success_count} merchants."