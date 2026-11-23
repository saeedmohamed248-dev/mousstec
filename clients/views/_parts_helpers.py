"""
🧰 دوال مشتركة لسوق قطع الغيار — بيستخدمها parts_marketplace_views و parts_wanted_views.
(في موديول لوحدها عشان مايبقاش فيه import دايري بين الاتنين.)
"""
from decimal import Decimal

_ACTIVE_ORDER_STATUSES = ('pending_payment', 'paid_held', 'shipped', 'delivered',
                          'refund_requested', 'disputed')


def _parse_int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_money(value):
    """Decimal > 0 with at most 2 decimals, or None if invalid."""
    from decimal import InvalidOperation
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount <= 0 or amount >= Decimal('10000000000'):
        return None
    return amount.quantize(Decimal('0.01'))
