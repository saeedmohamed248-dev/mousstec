# 🔗 مزامنة المخزون مع موقع FixIt الإلكتروني
# Mouss Tec هو مصدر الحقيقة: أي تغيير في المخزون هنا يتبعت للموقع تلقائياً.
#
# التفعيل من متغيرات البيئة (أو settings.py):
#   FIXIT_SYNC_URL    = https://your-site.vercel.app/api/sync
#   FIXIT_SYNC_SECRET = نفس قيمة SYNC_SECRET المضبوطة في Vercel
import logging
import os
import threading

import requests
from django.conf import settings

logger = logging.getLogger('mouss_tec_core')

CONDITION_MAP = {'new': 'new', 'used': 'used', 'core': 'used'}


def _config():
    url = getattr(settings, 'FIXIT_SYNC_URL', None) or os.environ.get('FIXIT_SYNC_URL')
    secret = getattr(settings, 'FIXIT_SYNC_SECRET', None) or os.environ.get('FIXIT_SYNC_SECRET')
    return (url, secret) if url and secret else (None, None)


def is_enabled():
    return _config()[0] is not None


def _post(payload):
    url, secret = _config()
    if not url:
        return
    try:
        response = requests.post(
            url,
            json=payload,
            headers={'X-Sync-Secret': secret},
            timeout=8,
        )
        if response.status_code >= 400:
            logger.warning("FixIt sync rejected (%s): %s", response.status_code, response.text[:200])
    except requests.RequestException as exc:
        logger.warning("FixIt sync failed (will not block operation): %s", exc)


def product_payload(product):
    """تحويل منتج Mouss Tec لصيغة موقع FixIt (المطابقة بالـ part_number = SKU)."""
    models_list = product.chassis_compatibility if isinstance(product.chassis_compatibility, list) else []
    if not models_list and product.car_model:
        models_list = [m.strip() for m in str(product.car_model).replace('،', ',').split(',') if m.strip()]
    oem_refs = product.oem_cross_reference if isinstance(product.oem_cross_reference, list) else []
    image_url = ''
    if product.image:
        try:
            image_url = product.image.url
            site_base = getattr(settings, 'SITE_BASE_URL', '') or os.environ.get('SITE_BASE_URL', '')
            if image_url.startswith('/') and site_base:
                image_url = site_base.rstrip('/') + image_url
        except Exception:
            image_url = ''
    return {
        'sku': product.part_number,
        'name': product.name,
        'brand': product.brand or 'BMW',
        'condition': CONDITION_MAP.get(product.condition, 'new'),
        'price': float(product.retail_price or 0),
        'stock': int(product.total_inventory_qty or 0),
        'models': models_list,
        'oem': oem_refs[0] if oem_refs else '',
        'image': image_url,
        'description': f"{product.name} — {product.car_model or ''} {product.car_year or ''}".strip(' —'),
    }


def push_stock(product):
    """تحديث كمية منتج واحد على الموقع (يتنادى تلقائياً مع أي حركة مخزون)."""
    if not is_enabled():
        return
    payload = {
        'action': 'set',
        'items': [{
            'sku': product.part_number,
            'stock': int(product.total_inventory_qty or 0),
            'price': float(product.retail_price or 0),
        }],
    }
    # في خيط منفصل عشان مننتظرش الشبكة جوه عملية الحفظ
    threading.Thread(target=_post, args=(payload,), daemon=True).start()


def push_all_products(stdout=None):
    """مزامنة كاملة: رفع/تحديث كل المنتجات النشطة على الموقع دفعة واحدة."""
    from inventory.models import Product

    url, _ = _config()
    if not url:
        raise RuntimeError("اضبط FIXIT_SYNC_URL و FIXIT_SYNC_SECRET الأول")

    products = Product.objects.filter(is_active=True)
    items = [product_payload(p) for p in products]
    # دفعات من 50 عشان حجم الطلب
    for start in range(0, len(items), 50):
        batch = items[start:start + 50]
        _post({'action': 'upsert', 'items': batch})
        if stdout:
            stdout.write(f"  ✓ اتبعت دفعة {start // 50 + 1} ({len(batch)} منتج)")
    return len(items)
