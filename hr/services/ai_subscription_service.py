"""
AI Design Subscription Service — إدارة اشتراكات الذكاء الاصطناعي للمصممين.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')


class AISubscriptionService:
    """
    Service Layer لإدارة اشتراكات AI التصميم.
    - تفعيل / إلغاء / تجديد
    - فحص صلاحية الاشتراك
    - دفع إلكتروني (Visa) أو يدوي من الأدمن
    """

    @staticmethod
    def activate_subscription(designer, plan, payment_method='visa',
                              payment_reference='', card_last_four='',
                              activated_by_user=None, admin_notes='',
                              duration_days=30):
        """
        تفعيل اشتراك AI جديد للمصمم.
        - إذا كان لديه اشتراك نشط → يمدد المدة
        - إذا لم يكن → ينشئ اشتراك جديد
        """
        from hr.models import AIDesignSubscription

        prices = AIDesignSubscription.PLAN_PRICES
        limits = AIDesignSubscription.PLAN_LIMITS
        today = timezone.now().date()

        # فحص اشتراك نشط حالي
        existing = AIDesignSubscription.objects.filter(
            designer=designer, status='active',
            end_date__gte=today,
        ).first()

        if existing:
            # تمديد الاشتراك الحالي
            existing.end_date = existing.end_date + timedelta(days=duration_days)
            existing.plan = plan
            existing.ai_generations_limit = limits.get(plan, 50)
            existing.ai_generations_used = 0  # إعادة تعيين عداد الاستخدام
            existing.price_paid = existing.price_paid + prices.get(plan, Decimal('99.00'))
            if payment_reference:
                existing.payment_reference = payment_reference
            if card_last_four:
                existing.card_last_four = card_last_four
            if admin_notes:
                existing.admin_notes = admin_notes
            existing.save()
            logger.info("[AI SUB] Extended subscription for %s — plan=%s, new_end=%s",
                        designer, plan, existing.end_date)
            return existing

        # إنشاء اشتراك جديد
        sub = AIDesignSubscription.objects.create(
            designer=designer,
            plan=plan,
            status='active',
            start_date=today,
            end_date=today + timedelta(days=duration_days),
            payment_method=payment_method,
            price_paid=prices.get(plan, Decimal('99.00')),
            payment_reference=payment_reference,
            card_last_four=card_last_four,
            ai_generations_limit=limits.get(plan, 50),
            ai_generations_used=0,
            activated_by=activated_by_user,
            admin_notes=admin_notes,
            auto_renew=(payment_method == 'visa'),
        )
        logger.info("[AI SUB] Created subscription for %s — plan=%s, ends=%s",
                     designer, plan, sub.end_date)
        return sub

    @staticmethod
    def cancel_subscription(subscription_id, cancelled_by_user=None, reason=''):
        """إلغاء اشتراك AI."""
        from hr.models import AIDesignSubscription

        sub = AIDesignSubscription.objects.get(pk=subscription_id)
        sub.status = 'cancelled'
        sub.auto_renew = False
        sub.cancelled_by = cancelled_by_user
        if reason:
            sub.admin_notes = (sub.admin_notes or '') + f"\n[إلغاء] {reason}"
        sub.save(update_fields=['status', 'auto_renew', 'cancelled_by', 'admin_notes', 'updated_at'])
        logger.info("[AI SUB] Cancelled subscription #%s for %s", sub.pk, sub.designer)
        return sub

    @staticmethod
    def check_ai_access(designer):
        """
        فحص هل المصمم يملك اشتراك AI نشط؟
        يُستخدم قبل كل عملية توليد AI.
        Returns: (bool, subscription_or_None, message)
        """
        from hr.models import AIDesignSubscription

        today = timezone.now().date()
        sub = AIDesignSubscription.objects.filter(
            designer=designer, status='active',
        ).order_by('-end_date').first()

        if not sub:
            return False, None, "ليس لديك اشتراك AI نشط. يرجى تفعيل الاشتراك أولاً."

        # فحص انتهاء الصلاحية
        if sub.end_date and today > sub.end_date:
            sub.status = 'expired'
            sub.save(update_fields=['status', 'updated_at'])
            return False, sub, "انتهت صلاحية اشتراكك في AI. يرجى التجديد."

        # فحص حد الاستخدام
        if sub.ai_generations_limit > 0 and sub.ai_generations_used >= sub.ai_generations_limit:
            return False, sub, f"استنفدت حد التصاميم ({sub.ai_generations_limit}). يرجى ترقية الباقة."

        return True, sub, "OK"

    @staticmethod
    def consume_generation(designer):
        """
        تسجيل استخدام توليد AI واحد.
        يُستدعى بعد كل تصميم ناجح بالذكاء الاصطناعي.
        """
        from hr.models import AIDesignSubscription

        today = timezone.now().date()
        sub = AIDesignSubscription.objects.filter(
            designer=designer, status='active',
            end_date__gte=today,
        ).order_by('-end_date').first()

        if sub:
            sub.ai_generations_used += 1
            sub.save(update_fields=['ai_generations_used', 'updated_at'])
            return True
        return False

    @staticmethod
    def get_designer_subscription(designer):
        """جلب الاشتراك الحالي (نشط أو آخر اشتراك)."""
        from hr.models import AIDesignSubscription

        # أولاً: اشتراك نشط
        today = timezone.now().date()
        active = AIDesignSubscription.objects.filter(
            designer=designer, status='active',
        ).order_by('-end_date').first()

        if active:
            # فحص الانتهاء التلقائي
            if active.end_date and today > active.end_date:
                active.status = 'expired'
                active.save(update_fields=['status', 'updated_at'])
            return active

        # ثانياً: آخر اشتراك (لعرض التاريخ)
        return AIDesignSubscription.objects.filter(
            designer=designer,
        ).order_by('-created_at').first()

    @staticmethod
    def process_visa_payment(designer, plan, card_token, card_last_four=''):
        """
        معالجة الدفع بالفيزا وتفعيل الاشتراك تلقائياً.
        card_token: رمز البطاقة من بوابة الدفع (Stripe/Paymob/etc.)

        ⚠️ في الإنتاج: يتم ربط هذا بـ Paymob أو Stripe webhook.
        حالياً: نحاكي عملية الدفع الناجحة.
        """
        from hr.models import AIDesignSubscription

        prices = AIDesignSubscription.PLAN_PRICES
        amount = prices.get(plan, Decimal('99.00'))

        # ═══ محاكاة بوابة الدفع ═══
        # في الإنتاج: يتم إرسال card_token إلى Paymob/Stripe
        # والتحقق من نجاح العملية عبر webhook callback
        payment_success = True  # سيتم استبداله بالاتصال الفعلي
        payment_ref = f"VISA-{timezone.now().strftime('%Y%m%d%H%M%S')}-{designer.pk}"

        if not payment_success:
            raise Exception("فشلت عملية الدفع. يرجى المحاولة مرة أخرى.")

        # تفعيل الاشتراك بعد نجاح الدفع
        sub = AISubscriptionService.activate_subscription(
            designer=designer,
            plan=plan,
            payment_method='visa',
            payment_reference=payment_ref,
            card_last_four=card_last_four,
            duration_days=30,
        )

        logger.info("[AI SUB] Visa payment processed — ref=%s, designer=%s, amount=%s",
                     payment_ref, designer, amount)
        return sub

    @staticmethod
    def admin_activate(designer, plan, admin_user, duration_days=30, notes=''):
        """تفعيل يدوي من الأدمن / السوبر أدمن."""
        return AISubscriptionService.activate_subscription(
            designer=designer,
            plan=plan,
            payment_method='admin_manual',
            payment_reference=f"ADMIN-{admin_user.username}-{timezone.now().strftime('%Y%m%d')}",
            activated_by_user=admin_user,
            admin_notes=notes or f"تفعيل يدوي بواسطة {admin_user.username}",
            duration_days=duration_days,
        )

    @staticmethod
    def admin_cancel(subscription_id, admin_user, reason=''):
        """إلغاء من الأدمن / السوبر أدمن."""
        return AISubscriptionService.cancel_subscription(
            subscription_id=subscription_id,
            cancelled_by_user=admin_user,
            reason=reason or f"إلغاء بواسطة {admin_user.username}",
        )
