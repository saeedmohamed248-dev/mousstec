"""Helpers for building subscription+plan+vehicle fixtures."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django_tenants.utils import schema_context


def get_or_create_premium_plan():
    from clients.models import Plan
    plan, _ = Plan.objects.get_or_create(
        slug='premium_diagnostics',
        defaults={
            'name': 'Premium Diagnostics',
            'industry': 'automotive',
            'monthly_price': 6000,
            'entitlements': {
                'diagnostics_live_data': {'enabled': True},
                'diagnostics_guided_tests': {'enabled': True},
                'diagnostics_smart_parts_finder': {'enabled': True},
                'diagnostics_external_api_scans': {'enabled': True, 'monthly_limit': 200},
            },
        },
    )
    return plan


def get_or_create_basic_plan():
    from clients.models import Plan
    plan, _ = Plan.objects.get_or_create(
        slug='basic_no_diag',
        defaults={
            'name': 'Basic (no diagnostics)',
            'industry': 'automotive',
            'monthly_price': 500,
            'entitlements': {},  # NO diagnostics features
        },
    )
    return plan


def attach_subscription(tenant, plan, *, quota_remaining: int = 100, active: bool = True):
    from django.utils import timezone as _tz
    from clients.models import TenantSubscription
    sub, _ = TenantSubscription.objects.update_or_create(
        tenant=tenant,
        defaults={
            'plan': plan,
            'is_active': active,
            'current_period_start': date.today(),
            'current_period_end': date.today() + timedelta(days=30),
            'diag_api_quota_remaining': quota_remaining,
            'locked_entitlements': dict(plan.entitlements or {}),
            'locked_monthly_price': plan.monthly_price,
            'locked_at': _tz.now(),
        },
    )
    # Bust the OneToOne reverse cache on the tenant instance so a stale
    # subscription from a prior test in the same class is not reused.
    try:
        tenant._state.fields_cache.pop('subscription', None)
    except Exception:
        pass
    return sub


def make_user(username='tech', is_staff=False):
    User = get_user_model()
    user, _ = User.objects.get_or_create(
        username=username,
        defaults={'email': f'{username}@test.local', 'is_staff': is_staff},
    )
    return user


def make_customer_and_vehicle(vin='1HGCM82633A123456', plate='ABC-001', brand='BMW'):
    """Must run inside tenant schema."""
    from inventory.models import Customer, Vehicle
    customer, _ = Customer.objects.get_or_create(
        name='Test Customer', defaults={'phone': '0100000000'}
    )
    vehicle, _ = Vehicle.objects.get_or_create(
        chassis_number=vin,
        defaults={
            'customer': customer,
            'car_plate': plate,
            'brand': brand,
            'model_name': 'F30',
            'last_mileage': 50000,
        },
    )
    return vehicle
