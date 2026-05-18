from celery import shared_task
from django.utils import timezone
from clients.models import Client
import logging

logger = logging.getLogger('mouss_tec_core')

@shared_task
def suspend_expired_trials():
    """
    💸 أتمتة الفخ الذهبي: رصد الحسابات التجريبية المنتهية وتعليقها تلقائياً عند منتصف الليل
    """
    today = timezone.now().date()
    expired_tenants = Client.objects.filter(status='trial', trial_ends_at__lt=today, is_active=True)
    
    count = expired_tenants.count()
    if count > 0:
        for tenant in expired_tenants:
            tenant.status = 'suspended'
            tenant.save(update_fields=['status'])
            logger.info(f"🔒 [CRON] Tenant '{tenant.schema_name}' trial expired. Account suspended safely.")
    return f"Successfully processed and suspended {count} expired accounts."

@shared_task
def update_market_trust_scores():
    """
    🤖 محرك تقييم التجار بالذكاء الاصطناعي:
    يعيد حساب نقاط الثقة لكل تاجر بناءً على النزاعات والصفقات، ويحظر النصابين تلقائياً.
    """
    tenants = Client.objects.filter(is_active=True)
    for tenant in tenants:
        base_score = 100
        # خصم نقاط بناءً على نسبة النزاعات
        if tenant.dispute_rate > 0:
            base_score -= int(tenant.dispute_rate * 2)
        
        # مكافأة للتجار المستقرين أصحاب الصفقات الناجحة
        if tenant.successful_deals > 50:
            base_score += 5
            
        tenant.ai_trust_score = max(min(base_score, 100), 1)
        
        # حظر آلي حماية للسوق إذا هبط مؤشر الأمان تحت 40%
        if tenant.ai_trust_score < 40:
            tenant.is_fraud_flagged = True
            logger.warning(f"🚨 [AI SHIELD] Tenant '{tenant.schema_name}' flagged as fraud due to low trust score ({tenant.ai_trust_score}).")
        
        tenant.save(update_fields=['ai_trust_score', 'is_fraud_flagged'])
    return "AI Trust scores calculated and synchronized successfully."