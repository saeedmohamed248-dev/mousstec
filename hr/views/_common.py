"""🧰 شاشات الموارد البشرية — مساعدات مشتركة (رد JSON موحّد ونص الخطأ للمستخدم)."""

import logging

from django.conf import settings
from django.http import JsonResponse

logger = logging.getLogger('mouss_tec_core')


def _json_response(data, status=200):
    """Safe JSON response wrapper — masks errors in production."""
    if status >= 500 and not settings.DEBUG:
        if 'error' in data:
            data = {"error": "حدث خطأ داخلي. يرجى المحاولة لاحقاً."}
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})


def _err_text(exc):
    """رسالة الخطأ للمستخدم — ValidationError كانت بتطلع «['...']»."""
    msgs = getattr(exc, 'messages', None)
    if msgs:
        return '؛ '.join(str(m) for m in msgs)
    from decimal import InvalidOperation
    if isinstance(exc, (InvalidOperation, ValueError, TypeError)):
        return "قيمة غير صالحة."
    return str(exc)
