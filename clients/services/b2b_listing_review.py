"""🌐 مراجعة طلبات النشر في سوق B2B من لوحة السوبر أدمن (Mouss Tec).

🐛 [FIX]: طلب النشر (``inventory.B2BListingRequest``) بيتسجّل جوه schema الشركة
نفسها، ومكانش فيه أي صفحة في السوبر أدمن (public) بتقراه — فالشركة تدوس «نشر في
سوق التجار» والطلب مايظهرش لإدارة Mouss Tec خالص. هنا بنلف على schemas الشركات
ونجمع الطلبات، والموافقة/الرفض بيتنفّذوا جوه schema الشركة.

ملحوظة: مستخدمين السوبر أدمن في public مش في schema الشركة، فمانقدرش نحطّهم في
``reviewed_by`` (FK على auth_user بتاع الشركة) — اسم المراجع بيتكتب في الملاحظات.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.utils import timezone

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ('active', 'trial', 'grace')


def _tenants():
    from clients.models import Client
    return (Client.objects.exclude(schema_name='public')
            .filter(status__in=_ACTIVE_STATUSES).only('id', 'schema_name', 'name')
            .order_by('name'))


def collect_requests(status='pending', limit=300):
    """كل طلبات النشر بالحالة دي من كل الشركات (الأحدث الأول)."""
    from django_tenants.utils import schema_context
    from inventory.models import B2BListingRequest

    rows = []
    for tenant in _tenants():
        try:
            with schema_context(tenant.schema_name):
                qs = B2BListingRequest.objects.select_related('product', 'requested_by')
                if status:
                    qs = qs.filter(status=status)
                for r in qs.order_by('-created_at')[:limit]:
                    p = r.product
                    rows.append({
                        'tenant_id': tenant.id, 'tenant_name': tenant.name,
                        'schema': tenant.schema_name, 'id': r.pk, 'status': r.status,
                        'status_label': r.get_status_display(),
                        'product_name': p.name, 'part_number': p.part_number,
                        'brand': p.brand or '', 'car_model': p.car_model or '',
                        'condition': p.get_condition_display() if hasattr(p, 'get_condition_display') else '',
                        'stock': p.total_inventory_qty,
                        'retail_price': p.retail_price, 'requested_price': r.requested_price,
                        'approved_price': r.approved_price, 'review_notes': r.review_notes,
                        'requested_by': (r.requested_by.get_full_name() or r.requested_by.username)
                        if r.requested_by else '',
                        'created_at': r.created_at,
                        'image_url': _image_url(p),
                    })
        except Exception:  # noqa: BLE001 — schema بايظ مايوقّعش الصفحة كلها
            logger.exception("[B2B REVIEW] failed to read schema %s", tenant.schema_name)
    rows.sort(key=lambda x: x['created_at'], reverse=True)
    return rows[:limit]


def _image_url(product):
    img = getattr(product, 'image', None)
    try:
        return img.url if img else ''
    except Exception:  # noqa: BLE001
        return ''


def pending_count():
    from django_tenants.utils import schema_context
    from inventory.models import B2BListingRequest
    n = 0
    for tenant in _tenants():
        try:
            with schema_context(tenant.schema_name):
                n += B2BListingRequest.objects.filter(status='pending').count()
        except Exception:  # noqa: BLE001
            logger.exception("[B2B REVIEW] count failed for %s", tenant.schema_name)
    return n


class ReviewError(Exception):
    pass


def decide(schema_name, request_id, action, *, reviewer_label='', price=None, notes=''):
    """موافقة/رفض طلب نشر جوه schema الشركة. بيرجّع (tenant, اسم الصنف)."""
    from django_tenants.utils import schema_context
    from clients.models import Client
    from inventory.models import B2BListingRequest
    from inventory.services.inventory_service import InventoryService

    if action not in ('approve', 'reject'):
        raise ReviewError('إجراء غير معروف')
    tenant = Client.objects.filter(schema_name=schema_name).exclude(schema_name='public').first()
    if tenant is None:
        raise ReviewError('الشركة مش موجودة')
    sign = f"✔ {reviewer_label} — Mouss Tec" if reviewer_label else '✔ Mouss Tec'
    with schema_context(tenant.schema_name):
        req = B2BListingRequest.objects.select_related('product').filter(pk=request_id).first()
        if req is None:
            raise ReviewError('الطلب مش موجود')
        if req.status != 'pending':
            raise ReviewError('الطلب اتراجع قبل كده')
        name = req.product.name
        if action == 'reject':
            req.status = 'rejected'
            req.reviewed_at = timezone.now()
            req.review_notes = '\n'.join(x for x in (notes.strip(), sign) if x)
            req.save(update_fields=['status', 'reviewed_at', 'review_notes'])
            return tenant, name
        try:
            approved = Decimal(str(price)) if price not in (None, '') else req.requested_price
        except (InvalidOperation, ValueError):
            raise ReviewError('السعر غلط')
        if approved <= 0:
            raise ReviewError('السعر لازم يكون أكبر من صفر')
        InventoryService.approve_b2b_listing(req, approved, None)
        req.review_notes = '\n'.join(x for x in (notes.strip(), sign) if x)
        req.save(update_fields=['review_notes'])
        return tenant, name


def approve_many(items, *, reviewer_label=''):
    """✅ موافقة جماعية بسعر الجملة المطلوب لكل طلب.

    items: [(schema_name, request_id), ...] — أو None = كل الطلبات المعلّقة في كل الشركات.
    بيرجّع dict: approved / skipped (اتراجع قبل كده أو سعره صفر) / failed + الشركات.
    طلب بايظ مايوقّفش الباقي.
    """
    if items is None:
        items = [(r['schema'], r['id']) for r in collect_requests(status='pending', limit=5000)]
    result = {'approved': 0, 'skipped': 0, 'failed': 0, 'tenants': set()}
    seen = set()
    for schema_name, request_id in items:
        key = (schema_name, int(request_id))
        if key in seen:
            continue
        seen.add(key)
        try:
            tenant, _name = decide(schema_name, key[1], 'approve', reviewer_label=reviewer_label)
        except ReviewError:
            result['skipped'] += 1
            continue
        except Exception:  # noqa: BLE001
            logger.exception("[B2B REVIEW] bulk approve failed %s#%s", schema_name, request_id)
            result['failed'] += 1
            continue
        result['approved'] += 1
        result['tenants'].add(tenant.name)
    return result
