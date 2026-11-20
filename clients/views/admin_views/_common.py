"""🧰 أدوات السوبر أدمن — مساعدات مشتركة (تحويل slug الباقة، حالات شراء التصاميم المدفوعة)."""
from __future__ import annotations

import logging
import os

from django.db import connection
from django.http import HttpResponseForbidden

from django.contrib.auth import get_user_model

from clients.models import (
    Plan,
)


logger = logging.getLogger('mouss_tec_core')
User = get_user_model()


ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal')


def _plan_slug_candidates(value):
    """slug الباقة نفسه + المقابل بتاعه لو القيمة من Client.plan القديمة ('gold' ⇒ 'auto-gold')."""
    from tenancy.services.plan_mapping import resolve_plan_slug
    value = (value or '').strip()
    return [v for v in (value, resolve_plan_slug(value)) if v]


def _resolve_active_plan(value):
    """أول باقة نشطة بالترتيب: الـ slug بالظبط، وبعدين المقابل القديم — ترتيب ثابت مش حسب الـ DB."""
    for slug in _plan_slug_candidates(value):
        plan = Plan.objects.filter(slug=slug, is_active=True).first()
        if plan is not None:
            return plan
    return None


# مشتريات باقات التصاميم اللي اتدفعت فعلاً (المستهلكة بالكامل اتدفعت برضه)
PAID_PURCHASE_STATUSES = ('paid', 'exhausted')


def _require_superadmin(request):
    """Common guard: must be public schema + authenticated superuser. Returns
    an HttpResponseForbidden when the check fails, otherwise None."""
    if getattr(connection, 'schema_name', 'public') != 'public':
        return HttpResponseForbidden('Access Denied (tenant schema)')
    if not request.user.is_authenticated or not request.user.is_superuser:
        return HttpResponseForbidden('Superuser only')
    return None
