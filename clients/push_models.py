"""🔔 Web Push subscriptions — PWA notifications (VAPID).

جدول مشترك في public schema (زي VisitorLog). الهوية بتتخزن بشكلين:

  • عملاء الماركت بليس → FK حقيقي على MarketplaceCustomer (نفس الـ schema).
  • موظفي الـ tenants → (tenant_schema, user_id) كقيم صريحة بدون FK —
    جدول auth_user متكرر في كل schema فالـ FK cross-schema مستحيل أصلاً.

الاشتراك بيتسجل من المتصفح عبر /push/subscribe/ بعد ما المستخدم يوافق على
الإشعارات، والإرسال بيتم من clients.services.webpush (pywebpush + VAPID).
اشتراك ميت (410 Gone من الـ push service) بيتعطل تلقائياً.
"""
from __future__ import annotations

from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _


class PushSubscription(models.Model):
    endpoint = models.URLField(max_length=500, unique=True, verbose_name=_("Push Endpoint"))
    p256dh = models.CharField(max_length=255, verbose_name=_("مفتاح P-256"))
    auth = models.CharField(max_length=255, verbose_name=_("Auth Secret"))

    # 🎯 صاحب الاشتراك — واحد من الاتنين:
    marketplace_customer = models.ForeignKey(
        'clients.MarketplaceCustomer', on_delete=models.CASCADE,
        null=True, blank=True, related_name='push_subscriptions',
        verbose_name=_("عميل السوق"),
    )
    tenant_schema = models.CharField(max_length=63, blank=True, default='',
                                     db_index=True, verbose_name=_("Schema"))
    user_id = models.IntegerField(null=True, blank=True, verbose_name=_("User ID (داخل الـ schema)"))

    user_agent = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_("نشط"))
    failure_count = models.PositiveIntegerField(default=0, verbose_name=_("مرات الفشل المتتالية"))
    created_at = models.DateTimeField(auto_now_add=True)
    last_success_at = models.DateTimeField(null=True, blank=True, verbose_name=_("آخر إرسال ناجح"))

    class Meta:
        verbose_name = _("اشتراك إشعارات")
        verbose_name_plural = _("🔔 اشتراكات الإشعارات (Web Push)")
        indexes = [
            models.Index(fields=['tenant_schema', 'user_id']),
        ]

    def __str__(self):
        owner = (f"mp:{self.marketplace_customer_id}" if self.marketplace_customer_id
                 else f"{self.tenant_schema}:{self.user_id}")
        return f"Push {owner} — {self.endpoint[:40]}…"


# ─────────────────────────────────────────────────────────────────────
# 🔗 CustomerNotification → push mirror
# ─────────────────────────────────────────────────────────────────────

@receiver(post_save, sender='clients.CustomerNotification',
          dispatch_uid='push_mirror_customer_notification')
def _push_on_customer_notification(sender, instance, created, **kwargs):
    """كل إشعار in-app جديد لعميل ماركت بليس يتبعت له push كمان.

    الإرسال async عبر Celery (queue=notifications) — الـ signal بيعمل
    enqueue بس، ولو Celery/الإعدادات مش جاهزة بيفشل بصمت (الإشعار
    الـ in-app هو المصدر الأساسي دايماً).
    """
    if not created:
        return
    try:
        from clients.services.webpush import webpush_configured
        if not webpush_configured():
            return
        from clients.tasks import send_web_push
        send_web_push.delay(
            marketplace_customer_id=instance.customer_id,
            title=instance.title,
            body=instance.body[:200],
            url='/marketplace/notifications/',
        )
    except Exception:
        pass
