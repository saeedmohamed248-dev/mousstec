"""🔌 Wix ↔ Mousstec sync engine.

Runs from the public schema (where WixConnection lives) but reads/writes the
tenant's operational data inside ``schema_context(tenant.schema_name)``.

Three entry points, all safe to call repeatedly:
  • test_connection(conn)  → verify credentials, update conn.last_test_ok.
  • push_products(conn)    → Mousstec Product → Wix Stores (create/update by
                             SKU + inventory quantity). Idempotent via
                             Product.wix_product_id.
  • pull_orders(conn)      → Wix eCommerce orders → Mousstec SaleInvoice.
                             Idempotent via SaleInvoice.wix_order_id.

Everything is defensive: a single bad product/order is logged and skipped so
one failure never stalls the batch. Errors land on conn.last_error.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from clients.services.wix_client import WixClient

logger = logging.getLogger('mouss_tec_core')


def _client(conn) -> WixClient:
    return WixClient(conn.api_key, conn.site_id, conn.account_id or '')


# ─────────────────────────────────────────────────────────────────────
# Connectivity
# ─────────────────────────────────────────────────────────────────────

def test_connection(conn) -> tuple[bool, str]:
    res = _client(conn).test_connection()
    conn.last_test_ok = res.ok
    conn.last_error = '' if res.ok else res.error[:2000]
    conn.save(update_fields=['last_test_ok', 'last_error', 'updated_at'])
    return res.ok, ('' if res.ok else res.error)


# ─────────────────────────────────────────────────────────────────────
# Products: Mousstec → Wix
# ─────────────────────────────────────────────────────────────────────

def push_products(conn, *, limit: int = 500) -> dict:
    """يزامن المنتجات النشطة للـ tenant إلى متجر Wix."""
    from clients.models import Client
    tenant = Client.objects.filter(pk=conn.client_id).first()
    if tenant is None:
        return {'error': 'tenant_missing'}

    client = _client(conn)
    summary = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': 0}

    with schema_context(tenant.schema_name):
        from inventory.models import Product
        qs = Product.objects.filter(is_active=True).order_by('id')[:limit]
        for p in qs:
            try:
                sku = (p.part_number or '').strip()
                if not sku:
                    summary['skipped'] += 1
                    continue
                qty = int(p.total_inventory_qty or 0)
                price = p.retail_price or Decimal('0')
                desc = _product_description(p)

                if p.wix_product_id:
                    res = client.update_product(
                        p.wix_product_id, name=p.name, price=price,
                        description=desc, visible=p.is_active)
                    if res.ok:
                        client.update_inventory(p.wix_product_id, qty)
                        summary['updated'] += 1
                    else:
                        summary['errors'] += 1
                    continue

                # مفيش mapping — نجرّب نلاقيه بالـ SKU الأول (متبعتش duplicate)
                found = client.find_product_by_sku(sku)
                wid = _first_product_id(found)
                if wid:
                    client.update_product(wid, name=p.name, price=price,
                                          description=desc, visible=p.is_active)
                    client.update_inventory(wid, qty)
                    p.wix_product_id = wid
                    p.save(update_fields=['wix_product_id'])
                    summary['updated'] += 1
                    continue

                created = client.create_product(
                    name=p.name, sku=sku, price=price, description=desc,
                    brand=p.brand or '', visible=p.is_active)
                new_id = _first_product_id(created, key='product')
                if new_id:
                    client.update_inventory(new_id, qty)
                    p.wix_product_id = new_id
                    p.save(update_fields=['wix_product_id'])
                    summary['created'] += 1
                else:
                    summary['errors'] += 1
                    if not conn.last_error:
                        conn.mark_error(created.error)
            except Exception as exc:
                summary['errors'] += 1
                logger.warning('[WIX push] product %s failed: %s', p.pk, exc)

    conn.products_pushed = summary['created'] + summary['updated']
    conn.last_product_sync_at = timezone.now()
    conn.save(update_fields=['products_pushed', 'last_product_sync_at', 'updated_at'])
    logger.info('[WIX push] tenant=%s %s', tenant.schema_name, summary)
    return summary


def _product_description(p) -> str:
    bits = []
    if p.brand:
        bits.append(f'الماركة: {p.brand}')
    if getattr(p, 'car_model', ''):
        bits.append(f'الموديلات: {p.car_model}')
    if getattr(p, 'warranty_months', 0):
        bits.append(f'ضمان: {p.warranty_months} شهر')
    return ' • '.join(bits)


def _first_product_id(res, key='product') -> str:
    if not res.ok or not res.data:
        return ''
    # create → {"product": {"id": ...}} ; query → {"products": [{"id": ...}]}
    if key in res.data and isinstance(res.data[key], dict):
        return res.data[key].get('id', '')
    products = res.data.get('products') or []
    return products[0].get('id', '') if products else ''


# ─────────────────────────────────────────────────────────────────────
# Orders: Wix → Mousstec
# ─────────────────────────────────────────────────────────────────────

def pull_orders(conn, *, limit: int = 50) -> dict:
    """يستورد طلبات Wix الجديدة كفواتير بيع في Mousstec."""
    from clients.models import Client
    tenant = Client.objects.filter(pk=conn.client_id).first()
    if tenant is None:
        return {'error': 'tenant_missing'}

    created_after = (conn.last_order_sync_at.isoformat()
                     if conn.last_order_sync_at else '')
    res = _client(conn).search_orders(limit=limit, created_after=created_after)
    if not res.ok:
        conn.mark_error(res.error)
        return {'error': res.error}

    orders = (res.data or {}).get('orders') or []
    summary = {'imported': 0, 'skipped': 0, 'errors': 0, 'unmatched_lines': 0}

    with schema_context(tenant.schema_name):
        from inventory.models import (
            Branch, Customer, Product, SaleInvoice, SaleInvoiceItem,
        )
        branch = (Branch.objects.filter(pk=conn.default_branch_id).first()
                  if conn.default_branch_id else None) or Branch.objects.first()
        if branch is None:
            return {'error': 'no_branch'}

        for order in orders:
            oid = str(order.get('id') or order.get('number') or '')
            if not oid:
                summary['skipped'] += 1
                continue
            if SaleInvoice.objects.filter(wix_order_id=oid).exists():
                summary['skipped'] += 1
                continue
            try:
                with transaction.atomic():
                    customer = _resolve_wix_customer(order, Customer)
                    inv = SaleInvoice.objects.create(
                        invoice_type='sale', status='posted', customer=customer,
                        branch=branch, wix_order_id=oid,
                        notes=f'طلب Wix #{order.get("number", oid)}',
                    )
                    matched = _add_order_lines(order, inv, Product, SaleInvoiceItem)
                    summary['unmatched_lines'] += matched['unmatched']
                    if matched['matched'] == 0:
                        # مفيش أي سطر اتطابق — نلغي الفاتورة عشان متبقاش فاضية
                        inv.delete()
                        summary['skipped'] += 1
                        continue
                summary['imported'] += 1
            except Exception as exc:
                summary['errors'] += 1
                logger.warning('[WIX pull] order %s failed: %s', oid, exc)

    conn.orders_imported = (conn.orders_imported or 0) + summary['imported']
    conn.last_order_sync_at = timezone.now()
    conn.save(update_fields=['orders_imported', 'last_order_sync_at', 'updated_at'])
    logger.info('[WIX pull] tenant=%s %s', tenant.schema_name, summary)
    return summary


def _resolve_wix_customer(order, Customer):
    """يلاقي/ينشئ عميل من buyerInfo بتاع طلب Wix."""
    buyer = order.get('buyerInfo') or {}
    billing = ((order.get('billingInfo') or {}).get('contactDetails') or {})
    phone = (buyer.get('phone') or billing.get('phone') or '').strip()
    email = (buyer.get('email') or '').strip()
    name = (f"{billing.get('firstName', '')} {billing.get('lastName', '')}".strip()
            or buyer.get('email') or 'عميل Wix')

    if phone:
        normalized = Customer.normalize_phone(phone)
        cust, _ = Customer.objects.get_or_create(
            phone=normalized, defaults={'name': name[:200]})
        return cust
    # مفيش تليفون — نستخدم عميل موحّد للطلبات الأونلاين المجهولة الرقم
    cust, _ = Customer.objects.get_or_create(
        phone=Customer.normalize_phone('0000000001'),
        defaults={'name': 'مبيعات Wix (بدون رقم)'})
    return cust


def _add_order_lines(order, invoice, Product, SaleInvoiceItem) -> dict:
    """يضيف أسطر الطلب المطابقة بالـ SKU. غير المطابق يتسجّل في الملاحظات."""
    result = {'matched': 0, 'unmatched': 0}
    unmatched_notes = []
    for line in (order.get('lineItems') or []):
        sku = ((line.get('physicalProperties') or {}).get('sku')
               or line.get('sku') or '').strip()
        qty = int(line.get('quantity') or 1)
        price = _line_price(line)
        product = Product.objects.filter(part_number=sku).first() if sku else None
        if product is None:
            result['unmatched'] += 1
            title = (line.get('productName') or {}).get('original') or sku or 'منتج'
            unmatched_notes.append(f'{title} ×{qty}')
            continue
        SaleInvoiceItem.objects.create(
            invoice=invoice, product=product, quantity=max(qty, 1),
            unit_price=price if price > 0 else (product.retail_price or Decimal('0')),
        )
        result['matched'] += 1

    if unmatched_notes:
        invoice.notes = (invoice.notes or '') + '\nأصناف Wix غير مطابقة: ' + ', '.join(unmatched_notes)
        invoice.save(update_fields=['notes'])
    return result


def _line_price(line) -> Decimal:
    try:
        raw = ((line.get('price') or {}).get('amount')
               or (line.get('priceBeforeDiscounts') or {}).get('amount') or '0')
        return Decimal(str(raw))
    except (InvalidOperation, TypeError):
        return Decimal('0')
