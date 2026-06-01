"""
🎁 Catalog of one-time top-up packages.
محدد هنا بدل ما يتسجل في DB عشان يبقى سهل نضيف/نعدل بدون migration.
"""
from __future__ import annotations
from decimal import Decimal
from typing import Optional


# 💳 شحن العملاء (في الماركت بليس)
CUSTOMER_TOPUPS = [
    {'slug': 'cust_50',  'designs': 50,  'price': Decimal('25.00'),  'name': '50 تصميم',  'badge': 'مبتدئ'},
    {'slug': 'cust_100', 'designs': 100, 'price': Decimal('35.00'),  'name': '100 تصميم', 'badge': '🔥 الأكثر مبيعاً'},
    {'slug': 'cust_500', 'designs': 500, 'price': Decimal('150.00'), 'name': '500 تصميم', 'badge': '💎 أوفر'},
]

# 💳 شحن الشركات (داخل لوحة التحكم)
TENANT_TOPUPS = [
    {'slug': 'tnt_1000', 'designs': 1000, 'price': Decimal('250.00'), 'name': '1000 تصميم', 'badge': 'مناسب'},
    {'slug': 'tnt_2500', 'designs': 2500, 'price': Decimal('500.00'), 'name': '2500 تصميم', 'badge': '🔥 الأكثر مبيعاً'},
    {'slug': 'tnt_5000', 'designs': 5000, 'price': Decimal('900.00'), 'name': '5000 تصميم', 'badge': '💎 أوفر'},
]

# 🎁 هدية التسجيل (لكل من العميل والشركة)
SIGNUP_BONUS_DESIGNS = 10


def get_topup_by_slug(slug: str, audience: str) -> dict | None:
    """Lookup helper آمن."""
    catalog = CUSTOMER_TOPUPS if audience == 'customer' else TENANT_TOPUPS
    for pkg in catalog:
        if pkg['slug'] == slug:
            return pkg
    return None
