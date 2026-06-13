from django.conf import settings
from django.db import models, transaction
from django_tenants.models import TenantMixin, DomainMixin
from clients.soft_delete import SoftDeleteMixin
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.db.models import F
from datetime import timedelta
from decimal import Decimal
import uuid
import logging

logger = logging.getLogger('mouss_tec_core')


# Cross-domain references resolved via:
from .marketplace_c2c import *  # noqa: F401, F403

# Customer-tier diagnostics subscriptions (car owners, not workshops).

# =====================================================================
# 💎 Customer-tier Diagnostics Subscription (car owners — not workshops)
# =====================================================================
class CustomerDiagnosticsSubscription(models.Model):
    """اشتراك التشخيص لعميل سوق السيارات (MarketplaceCustomer).

    منفصل تماماً عن TenantSubscription (الوِرَش) — العميل بيشخّص عربيته بنفسه.

    دورة الحياة:
      register → trial (7 أيام، 5 سكانات) → upgrade لـ paid → renew/cancel.

    الـ tier هو مصدر الحقيقة:
      trial   → trial_ends_at  حصري
      basic|pro|empire → paid_until + scans_per_month + features
    """
    TIER_CHOICES = (
        ('trial',  _('تجربة مجانية')),
        ('basic',  _('Basic — 99 ج/شهر')),
        ('pro',    _('Pro — 199 ج/شهر')),
        ('empire', _('Empire — 399 ج/شهر')),
        ('expired', _('منتهية')),
    )
    TIER_PRICES_EGP = {
        'trial':  Decimal('0.00'),
        'basic':  Decimal('99.00'),
        'pro':    Decimal('199.00'),
        'empire': Decimal('399.00'),
    }
    TIER_QUOTAS = {  # سكانات/شهر
        'trial':  5,
        'basic':  30,
        'pro':    100,
        'empire': 10_000,  # عملياً غير محدود
    }
    TIER_FEATURES = {
        'trial':  ['ai_diagnosis'],
        'basic':  ['ai_diagnosis', 'vehicle_history'],
        'pro':    ['ai_diagnosis', 'vehicle_history', 'live_data', 'pdf_reports', 'tech_chat'],
        'empire': ['ai_diagnosis', 'vehicle_history', 'live_data', 'pdf_reports', 'tech_chat',
                   'priority_support', 'multi_vehicle', 'parts_rewards'],
    }
    TRIAL_DAYS = 7

    customer = models.OneToOneField(
        MarketplaceCustomer, on_delete=models.CASCADE,
        related_name='diagnostics_subscription',
    )
    tier = models.CharField(max_length=12, choices=TIER_CHOICES, default='trial', db_index=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    paid_until = models.DateTimeField(null=True, blank=True)
    auto_renew = models.BooleanField(default=False)

    # Quota tracking — refilled at the start of each paid month
    period_start = models.DateTimeField(default=timezone.now)
    scans_used = models.IntegerField(default=0)
    lifetime_scans = models.IntegerField(default=0)

    # Payment audit — last successful upgrade
    last_payment_at = models.DateTimeField(null=True, blank=True)
    last_payment_egp = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    last_payment_ref = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("اشتراك تشخيص عميل")
        verbose_name_plural = _("💎 اشتراكات تشخيص العملاء")

    def __str__(self):
        return f"{self.customer.full_name} — {self.get_tier_display()}"

    # ── State helpers ──────────────────────────────────────────────────
    def is_active(self) -> bool:
        """True iff customer can use diagnostics right now."""
        now = timezone.now()
        if self.tier == 'trial':
            return bool(self.trial_ends_at and now < self.trial_ends_at)
        if self.tier in ('basic', 'pro', 'empire'):
            return bool(self.paid_until and now < self.paid_until)
        return False

    def days_remaining(self) -> int:
        end = self.paid_until if self.tier != 'trial' else self.trial_ends_at
        if not end:
            return 0
        delta = end - timezone.now()
        return max(delta.days, 0)

    def quota_remaining(self) -> int:
        return max(self.TIER_QUOTAS.get(self.tier, 0) - self.scans_used, 0)

    def has_feature(self, code: str) -> bool:
        return code in self.TIER_FEATURES.get(self.tier, [])

    def can_scan(self) -> tuple[bool, str]:
        if not self.is_active():
            return False, "الاشتراك منتهي — جدّد للاستمرار."
        if self.quota_remaining() <= 0:
            return False, "انتهت السكانات الشهرية — رقّي الباقة أو انتظر التجديد."
        return True, ""

    def record_scan(self) -> None:
        """Atomic scan counter — safe under concurrent requests."""
        type(self).objects.filter(pk=self.pk).update(
            scans_used=F('scans_used') + 1,
            lifetime_scans=F('lifetime_scans') + 1,
        )
        self.refresh_from_db(fields=['scans_used', 'lifetime_scans'])

    def reset_period_if_needed(self) -> None:
        """Refill scans at the start of each 30-day window for paid tiers."""
        if self.tier not in ('basic', 'pro', 'empire'):
            return
        if (timezone.now() - self.period_start) >= timedelta(days=30):
            self.period_start = timezone.now()
            self.scans_used = 0
            self.save(update_fields=['period_start', 'scans_used'])

    def upgrade(self, new_tier: str, payment_ref: str = '') -> None:
        """Activate a paid tier for 30 days. Caller is responsible for payment."""
        if new_tier not in ('basic', 'pro', 'empire'):
            raise ValueError(f"Invalid tier: {new_tier}")
        now = timezone.now()
        # Stack on top of any remaining paid time (don't burn user's days)
        base = self.paid_until if (self.paid_until and self.paid_until > now) else now
        self.tier = new_tier
        self.paid_until = base + timedelta(days=30)
        self.period_start = now
        self.scans_used = 0
        self.last_payment_at = now
        self.last_payment_egp = self.TIER_PRICES_EGP[new_tier]
        self.last_payment_ref = payment_ref[:64]
        self.save()

    @classmethod
    def grant_trial(cls, customer: 'MarketplaceCustomer') -> 'CustomerDiagnosticsSubscription':
        """Idempotent: returns existing sub if any, else creates a 7-day trial."""
        sub, created = cls.objects.get_or_create(
            customer=customer,
            defaults={
                'tier': 'trial',
                'trial_ends_at': timezone.now() + timedelta(days=cls.TRIAL_DAYS),
            },
        )
        return sub


