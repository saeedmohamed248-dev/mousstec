"""
💳💳 Central Credit Ledger — توحيد منطق التصاميم المتاحة لكل الجهات.
=====================================================================

🏢 Tenant (المطبعة/الشركة) — مصادر الرصيد بترتيب الاستهلاك:
   1. حصة الباقة الشهرية (Plan.monthly_ai_designs_quota)
   2. هدايا الإدارة (AIBonusGrant)
   3. شحنات Top-up المدفوعة (TenantDesignTopUp)

👤 MarketplaceCustomer — مصادر الرصيد بترتيب الاستهلاك:
   1. التصاميم المجانية (free_designs_total - free_designs_used) — تشمل هدية التسجيل
   2. شراء باقات (DesignPurchase الـ paid)

كل الـ consumes atomic (transaction.atomic + SELECT FOR UPDATE pattern عبر update()).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from .credit_packages import SIGNUP_BONUS_DESIGNS

logger = logging.getLogger('mouss_tec_core')


# =============================================================================
# 🏢 TENANT BALANCE & CONSUMPTION
# =============================================================================
def get_tenant_balance(tenant) -> dict[str, Any]:
    """
    يجيب رصيد الشركة المتاح من كل المصادر.

    Returns:
        {
            'plan_quota': int,           # حصة الباقة الشهرية
            'plan_used_this_month': int, # المستهلك في الشهر الحالي
            'plan_remaining': int,
            'grants_remaining': int,     # هدايا الإدارة
            'topups_remaining': int,     # الـ top-ups المدفوعة
            'total': int,                # إجمالي المتاح
        }
    """
    from clients.models import AIBonusGrant, AILimitTracker, TenantDesignTopUp

    # 1. حصة الباقة
    sub = getattr(tenant, 'subscription', None)
    plan_quota = 0
    if sub and sub.is_active and sub.plan:
        plan_quota = int(sub.plan.monthly_ai_designs_quota or 0)

    # المستهلك في الشهر الحالي (calendar month)
    now = timezone.now()
    used_this_month = AILimitTracker.objects.filter(
        tenant=tenant,
        action_type='ai_generation',
        used_at__year=now.year,
        used_at__month=now.month,
    ).count()
    plan_remaining = max(plan_quota - used_this_month, 0)

    # 2. هدايا الإدارة (active + not expired)
    grants_qs = AIBonusGrant.objects.filter(tenant=tenant, is_active=True)
    grants_remaining = 0
    for g in grants_qs:
        if g.expires_at and g.expires_at < now:
            continue
        grants_remaining += max(int(g.granted_designs) - int(g.consumed_designs), 0)

    # 3. شحنات الـ Top-up المدفوعة
    topups_qs = TenantDesignTopUp.objects.filter(tenant=tenant, status='paid')
    topups_remaining = 0
    for t in topups_qs:
        if t.expires_at and t.expires_at < now:
            continue
        topups_remaining += max(int(t.designs_total) - int(t.designs_used), 0)

    return {
        'plan_quota': plan_quota,
        'plan_used_this_month': used_this_month,
        'plan_remaining': plan_remaining,
        'grants_remaining': grants_remaining,
        'topups_remaining': topups_remaining,
        'total': plan_remaining + grants_remaining + topups_remaining,
    }


@transaction.atomic
def consume_tenant_credit(tenant, metadata: dict | None = None) -> dict[str, Any]:
    """
    يخصم تصميم واحد من رصيد الشركة. ترتيب الاستهلاك:
    1. حصة الباقة الشهرية → ينضاف صف لـ AILimitTracker
    2. أقدم AIBonusGrant فيه رصيد → consumed_designs += 1
    3. أقدم TenantDesignTopUp فيه رصيد → designs_used += 1

    Returns: {'success': True, 'source': 'plan|grant|topup', 'remaining_total': int}
             أو {'success': False, 'reason': 'no_credit'}
    """
    from clients.models import AIBonusGrant, AILimitTracker, TenantDesignTopUp

    metadata = metadata or {}
    now = timezone.now()

    # 1. جرّب حصة الباقة
    sub = getattr(tenant, 'subscription', None)
    if sub and sub.is_active and sub.plan and sub.plan.monthly_ai_designs_quota:
        used = AILimitTracker.objects.filter(
            tenant=tenant, action_type='ai_generation',
            used_at__year=now.year, used_at__month=now.month,
        ).count()
        if used < int(sub.plan.monthly_ai_designs_quota):
            AILimitTracker.objects.create(
                tenant=tenant,
                action_type='ai_generation',
                metadata={**metadata, 'source': 'plan_monthly_quota'},
            )
            bal = get_tenant_balance(tenant)
            return {'success': True, 'source': 'plan', 'remaining_total': bal['total'], 'balance': bal}

    # 2. جرّب AIBonusGrant (أقدم grant فيه رصيد)
    grant = (
        AIBonusGrant.objects.filter(tenant=tenant, is_active=True)
        .filter(granted_designs__gt=F('consumed_designs'))
        .order_by('granted_at')
        .select_for_update(skip_locked=True)
        .first()
    )
    if grant:
        # تأكد إن مش منتهي
        if not (grant.expires_at and grant.expires_at < now):
            AIBonusGrant.objects.filter(pk=grant.pk).update(consumed_designs=F('consumed_designs') + 1)
            AILimitTracker.objects.create(
                tenant=tenant,
                action_type='ai_generation',
                metadata={**metadata, 'source': 'admin_grant', 'grant_id': grant.pk},
            )
            bal = get_tenant_balance(tenant)
            return {'success': True, 'source': 'grant', 'remaining_total': bal['total'], 'balance': bal}

    # 3. جرّب TenantDesignTopUp (أقدم top-up فيه رصيد)
    topup = (
        TenantDesignTopUp.objects.filter(tenant=tenant, status='paid')
        .filter(designs_total__gt=F('designs_used'))
        .order_by('paid_at', 'created_at')
        .select_for_update(skip_locked=True)
        .first()
    )
    if topup:
        if not (topup.expires_at and topup.expires_at < now):
            topup.consume_design()
            AILimitTracker.objects.create(
                tenant=tenant,
                action_type='ai_generation',
                metadata={**metadata, 'source': 'topup', 'topup_id': topup.pk},
            )
            bal = get_tenant_balance(tenant)
            return {'success': True, 'source': 'topup', 'remaining_total': bal['total'], 'balance': bal}

    return {'success': False, 'reason': 'no_credit', 'balance': get_tenant_balance(tenant)}


# =============================================================================
# 👤 CUSTOMER BALANCE & CONSUMPTION
# =============================================================================
def get_customer_balance(customer) -> dict[str, Any]:
    """رصيد عميل الماركت بليس."""
    from clients.models import DesignPurchase

    free_remaining = max(
        int(customer.free_designs_total or 0) - int(customer.free_designs_used or 0), 0
    )

    paid_remaining = 0
    purchases = DesignPurchase.objects.filter(customer=customer, status='paid')
    now = timezone.now()
    for p in purchases:
        if p.expires_at and p.expires_at < now:
            continue
        paid_remaining += max(int(p.designs_total) - int(p.designs_used), 0)

    return {
        'free_remaining': free_remaining,
        'paid_remaining': paid_remaining,
        'total': free_remaining + paid_remaining,
    }


@transaction.atomic
def consume_customer_credit(customer, metadata: dict | None = None) -> dict[str, Any]:
    """
    يخصم تصميم من رصيد العميل. الأولوية: مجاني → مدفوع.
    """
    from clients.models import MarketplaceCustomer, DesignPurchase

    metadata = metadata or {}
    now = timezone.now()

    # 1. مجاني الأول
    free_remaining = max(
        int(customer.free_designs_total or 0) - int(customer.free_designs_used or 0), 0
    )
    if free_remaining > 0:
        MarketplaceCustomer.objects.filter(pk=customer.pk).update(
            free_designs_used=F('free_designs_used') + 1,
        )
        customer.refresh_from_db(fields=['free_designs_used'])
        bal = get_customer_balance(customer)
        return {'success': True, 'source': 'free', 'remaining_total': bal['total'], 'balance': bal}

    # 2. أقدم باقة مدفوعة فيها رصيد
    purchase = (
        DesignPurchase.objects.filter(customer=customer, status='paid')
        .filter(designs_total__gt=F('designs_used'))
        .order_by('paid_at', 'created_at')
        .select_for_update(skip_locked=True)
        .first()
    )
    if purchase:
        if not (purchase.expires_at and purchase.expires_at < now):
            purchase.consume_design()
            bal = get_customer_balance(customer)
            return {'success': True, 'source': 'paid', 'remaining_total': bal['total'], 'balance': bal}

    return {'success': False, 'reason': 'no_credit', 'balance': get_customer_balance(customer)}


# =============================================================================
# 🎁 SIGNUP BONUS
# =============================================================================
def grant_signup_bonus_customer(customer) -> bool:
    """يدي عميل جديد 10 تصاميم مجانية. Idempotent — مش بيكرر."""
    from clients.models import MarketplaceCustomer

    # Idempotent: لو already granted, ما نكررش
    if (customer.free_designs_total or 0) >= SIGNUP_BONUS_DESIGNS:
        return False
    MarketplaceCustomer.objects.filter(pk=customer.pk).update(
        free_designs_total=SIGNUP_BONUS_DESIGNS,
    )
    customer.refresh_from_db(fields=['free_designs_total'])
    logger.info(f'[SIGNUP BONUS] Customer {customer.pk} granted {SIGNUP_BONUS_DESIGNS} designs')
    return True


def grant_signup_bonus_tenant(tenant) -> bool:
    """يدي شركة جديدة 10 تصاميم مجانية كـ AIBonusGrant. Idempotent."""
    from clients.models import AIBonusGrant

    # Idempotent: لو فيه already grant بنفس الـ reason ما نكررش
    existing = AIBonusGrant.objects.filter(
        tenant=tenant, reason='signup_bonus', is_active=True,
    ).exists()
    if existing:
        return False

    AIBonusGrant.objects.create(
        tenant=tenant,
        granted_designs=SIGNUP_BONUS_DESIGNS,
        consumed_designs=0,
        reason='signup_bonus',
        is_active=True,
    )
    logger.info(f'[SIGNUP BONUS] Tenant {tenant.schema_name} granted {SIGNUP_BONUS_DESIGNS} designs')
    return True


# =============================================================================
# 💳 PURCHASE HELPERS
# =============================================================================
@transaction.atomic
def create_tenant_topup(tenant, designs: int, price, payment_method: str = 'paymob',
                       payment_reference: str = '', mark_paid: bool = False):
    """يخلق TenantDesignTopUp record. لو mark_paid=True بيقفل status=paid فوراً."""
    from clients.models import TenantDesignTopUp

    now = timezone.now()
    topup = TenantDesignTopUp.objects.create(
        tenant=tenant,
        designs_total=int(designs),
        designs_used=0,
        price_paid=price,
        payment_method=payment_method,
        payment_reference=payment_reference,
        status='paid' if mark_paid else 'pending',
        paid_at=now if mark_paid else None,
    )
    return topup
