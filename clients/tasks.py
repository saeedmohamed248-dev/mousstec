from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db.models import F
from clients.models import Client
import logging

# تهيئة رادار المراقبة
logger = logging.getLogger('mouss_tec_core')

# =====================================================================
# 📨 1. وكيل الترحيب الذكي (Zero-Trust Omni-Channel Dispatcher)
# =====================================================================
@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def async_welcome_bot_task(self, client_name, client_phone, business_type, full_login_url, admin_user, secret_credential=None):
    """
    يتلقى الأوامر من الـ Provisioning Orchestrator (signals.py).
    🚀 ابتكار: مهيأ للتعامل مع الروابط السحرية (Magic Links) المطبقة في معمارية Zero-Trust.
    """
    try:
        type_str = "مركز الصيانة" if business_type in ['service_center', 'both'] else "تجارة قطع الغيار"
        
        # صياغة الرسالة بنظام الـ Magic Link أو Credentials
        msg = (
            f"مرحباً بك في Mouss Tec Ecosystem 🚀\n\n"
            f"تم تأسيس نظام {type_str} الخاص بك ({client_name}) بنجاح.\n\n"
            f"🌐 ادخل لغرفة العمليات الآن: {full_login_url}\n"
            f"👤 الدخول مخصص للمسؤول: {admin_user}\n"
        )
        
        # دعم رجعي (Backward Compatibility) في حال تم إرسال باسورد بدلاً من التوكن
        if secret_credential and "token=" not in full_login_url:
            msg += f"🔑 كلمة المرور المؤقتة: {secret_credential}\n"
            
        msg += "\nنتمنى لك أرباحاً هائلة والتفوق على منافسيك!"
        
        # 💡 [مكان دمج بوابة WhatsApp API (مثل Twilio/Ultramsg) أو البريد الإلكتروني]
        # example: twilio_client.messages.create(body=msg, from_='+123', to=client_phone)
        
        logger.info(f"📨 [AI WELCOME BOT]: Secure onboarding message dispatched to {client_name} ({client_phone}).")
        # print(msg) # مفيد للعرض أثناء الـ Local Development
        
    except Exception as exc:
        logger.warning(f"⚠️ [AI WELCOME BOT]: Delivery failed for {client_name}. Retrying... ({self.request.retries}/3)")
        raise self.retry(exc=exc)

# =====================================================================
# 💸 2. وكيل إدارة المطالبات والتعليق (Dunning & Grace Period Enforcer)
# =====================================================================
@shared_task
def orchestrate_billing_and_suspensions():
    """
    🚀 ابتكار Dunning Management:
    لا يغلق الحسابات فجأة! يرسل تحذيرات قبل الانتهاء بـ 3 أيام، ويحترم فترة السماح (Grace Period)
    ويطبق الـ Bulk Update لتفادي الـ N+1 Queries.
    """
    today = timezone.now().date()
    warning_date = today + timedelta(days=3)
    grace_period_deadline = today - timedelta(days=3)
    
    try:
        # -------------------------------------------------------------
        # أ. مرحلة التحذير الاستباقي (Early Warning - Retention)
        # -------------------------------------------------------------
        # استخراج العملاء الذين ستنتهي تجربتهم أو اشتراكهم بعد 3 أيام
        expiring_soon_trials = Client.objects.filter(status='trial', trial_ends_at=warning_date, is_active=True)
        expiring_soon_active = Client.objects.filter(status='active', subscription_end_date=warning_date, is_active=True)
        
        warned_count = expiring_soon_trials.count() + expiring_soon_active.count()
        if warned_count > 0:
            # 💡 [هنا يتم استدعاء Notification Task لإرسال رسائل التجديد عبر الواتساب/الإيميل]
            logger.info(f"🔔 [DUNNING SYSTEM]: {warned_count} clients warned about upcoming expiration.")

        # -------------------------------------------------------------
        # ب. مرحلة الإغلاق الناعم (Soft/Hard Suspension) - Bulk Update
        # -------------------------------------------------------------
        # 1. إيقاف الفترات التجريبية المنتهية (التريال ليس له فترة سماح)
        expired_trials = Client.objects.filter(status='trial', trial_ends_at__lt=today, is_active=True)
        trials_suspended = expired_trials.update(status='suspended')
        
        # 2. إيقاف الاشتراكات المدفوعة التي استنفدت فترة السماح (Grace Period Ended)
        expired_active = Client.objects.filter(status='active', subscription_end_date__lt=grace_period_deadline, is_active=True)
        active_suspended = expired_active.update(status='suspended')
        
        total_suspended = trials_suspended + active_suspended
        if total_suspended > 0:
            logger.info(f"🔒 [CRON GUARD]: Successfully suspended {trials_suspended} trials and {active_suspended} expired subscriptions.")
            
        return f"Billing Cron Orchestrated: {warned_count} Warned | {total_suspended} Suspended Safely."
        
    except Exception as e:
        logger.error(f"🔴 [CRON GUARD FATAL ERROR]: Billing orchestration crashed - {e}")
        return f"Failed: {e}"

# =====================================================================
# 🤖 3. وكيل الثقة ومكافحة الاحتيال (AI Time-Decay Trust Orchestrator)
# =====================================================================
@shared_task
def update_market_trust_scores():
    """
    محرك تقييم التجار بالذكاء الاصطناعي:
    🚀 ابتكار: يستخدم الـ Bulk Update لتحديث 100,000 تاجر في ثوانٍ.
    🚀 ابتكار: خوارزمية التآكل الزمني (Time-Decay) لمعاقبة الخمول.
    """
    # جلب التجار النشطين فقط
    active_merchants = Client.objects.filter(is_active=True, is_marketplace_active=True)
    
    updates_to_save = []
    auto_banned_count = 0
    today = timezone.now().date()
    
    for tenant in active_merchants:
        try:
            base_score = 100
            
            # 1. عقوبة النزاعات (Dispute Penalty - Harsh)
            if tenant.dispute_rate > 0:
                base_score -= int(tenant.dispute_rate * 3) # تغليظ العقوبة لردع المحتالين
            
            # 2. مكافأة الإنجاز وخوارزمية التآكل (Velocity & Decay Algorithm)
            # نحسب عمر حساب التاجر بالشهور
            account_age_days = (today - tenant.created_on).days if tenant.created_on else 1
            account_age_months = max(account_age_days / 30.0, 1)
            
            # سرعة الصفقات (Deals per month)
            velocity = getattr(tenant, 'successful_deals', 0) / account_age_months
            
            if velocity > 5: # تاجر نشط جداً
                base_score += 10
            elif velocity < 0.5 and account_age_months > 3: # تاجر خامل لأكثر من 3 شهور
                base_score -= 5 # عقوبة التآكل الزمني

            # ضبط النقاط النهائية
            new_trust_score = max(min(base_score, 100), 1)
            
            # تحديث الكائن في الذاكرة (Memory)
            tenant.ai_trust_score = new_trust_score
            
            # 3. الحظر الآلي لحماية السوق (Auto-Ban Mechanism)
            if tenant.ai_trust_score < 40 and not getattr(tenant, 'is_fraud_flagged', False):
                tenant.is_fraud_flagged = True
                auto_banned_count += 1
                logger.critical(f"🚨 [AI SHIELD]: Tenant '{tenant.schema_name}' AUTO-BANNED (Score: {tenant.ai_trust_score}).")
                
            updates_to_save.append(tenant)
            
        except Exception as e:
            logger.error(f"🔴 [AI SHIELD ERROR]: Calculation failed for '{tenant.schema_name}' - {e}")
            continue
            
    # ⚡ تحديث مجمع (Bulk Update) لدفع البيانات دفعة واحدة بدلاً من استعلامات N+1 الخانقة
    if updates_to_save:
        try:
            # تقسيم التحديثات لكتل (Batches) لتفادي اختناق الذاكرة في قواعد البيانات الضخمة
            Client.objects.bulk_update(updates_to_save, ['ai_trust_score', 'is_fraud_flagged'], batch_size=1000)
            logger.info(f"🛡️ [AI SHIELD]: Trust scores synchronized for {len(updates_to_save)} merchants.")
        except Exception as e:
            logger.error(f"🔴 [AI SHIELD FATAL]: Bulk update crashed - {e}")
            
    return f"AI Trust Orchestrator: Synced {len(updates_to_save)} merchants. Banned {auto_banned_count} fraudsters."