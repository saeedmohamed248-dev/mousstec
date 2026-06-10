"""
🕘 Business Hours service — توقيت القاهرة، 9 صباحاً → 5 مساءً، ما عدا الجمعة.

الاستخدام:
    from clients.services.business_hours import is_business_hours, get_offline_message
    if is_business_hours():
        ...
"""
from datetime import time
from zoneinfo import ZoneInfo
from django.utils import timezone

CAIRO_TZ = ZoneInfo("Africa/Cairo")
WORK_START = time(9, 0)
WORK_END = time(17, 0)
FRIDAY = 4  # Monday=0 .. Friday=4


def is_business_hours(now=None) -> bool:
    now = (now or timezone.now()).astimezone(CAIRO_TZ)
    if now.weekday() == FRIDAY:
        return False
    return WORK_START <= now.time() < WORK_END


def get_offline_message() -> str:
    return (
        "🕘 مواعيد عملنا 9 صباحاً حتى 5 مساءً (عدا يوم الجمعة).\n"
        "اترك رسالتك في النموذج وهنرد عليك عبر البريد في أقرب وقت."
    )


def get_status_payload() -> dict:
    """يرجّع payload جاهز للاستخدام في JSON response."""
    now = timezone.now().astimezone(CAIRO_TZ)
    return {
        'is_open': is_business_hours(now),
        'cairo_time': now.strftime('%H:%M'),
        'cairo_date': now.strftime('%Y-%m-%d'),
        'work_start': WORK_START.strftime('%H:%M'),
        'work_end': WORK_END.strftime('%H:%M'),
        'closed_days': ['Friday'],
        'offline_message': get_offline_message(),
    }
