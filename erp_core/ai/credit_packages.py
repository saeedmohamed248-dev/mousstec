"""
🎁 Catalog of one-time top-up packages.
محدد هنا بدل ما يتسجل في DB عشان يبقى سهل نضيف/نعدل بدون migration.
"""
from __future__ import annotations
from decimal import Decimal
from typing import Optional


# 💳 شحن العملاء (في الماركت بليس)
# تسعير: 3 ج/تصميم (تكلفة 2.5 ج + هامش 20%)
CUSTOMER_TOPUPS = [
    {'slug': 'cust_20',  'designs': 20, 'price': Decimal('60.00'),  'name': '20 تصميم',  'badge': 'مبتدئ'},
    {'slug': 'cust_40',  'designs': 40, 'price': Decimal('120.00'), 'name': '40 تصميم',  'badge': '🔥 الأكثر مبيعاً'},
    {'slug': 'cust_80',  'designs': 80, 'price': Decimal('240.00'), 'name': '80 تصميم',  'badge': '💎 أوفر'},
]

# 💳 شحن الشركات (داخل لوحة التحكم)
TENANT_TOPUPS = [
    {'slug': 'tnt_40',  'designs': 40,  'price': Decimal('120.00'), 'name': '40 تصميم',  'badge': 'مناسب'},
    {'slug': 'tnt_120', 'designs': 120, 'price': Decimal('360.00'), 'name': '120 تصميم', 'badge': '🔥 الأكثر مبيعاً'},
    {'slug': 'tnt_200', 'designs': 200, 'price': Decimal('600.00'), 'name': '200 تصميم', 'badge': '💎 أوفر'},
]

# 🎁 هدية التسجيل
SIGNUP_BONUS_DESIGNS_CUSTOMER = 1
SIGNUP_BONUS_DESIGNS_TENANT = 3


def get_topup_by_slug(slug: str, audience: str) -> dict | None:
    """Lookup helper آمن."""
    catalog = CUSTOMER_TOPUPS if audience == 'customer' else TENANT_TOPUPS
    for pkg in catalog:
        if pkg['slug'] == slug:
            return pkg
    return None
