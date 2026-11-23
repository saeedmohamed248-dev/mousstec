"""🏬 «من مخازن التجار» — أصناف الشركات المعتمدة في سوق قطع الغيار العام.

🐛 [FIX]: الشركة (زي FixIt) تطلب نشر أصنافها والإدارة توافق، والأصناف كانت بتتحط في
``GlobalB2BMarketplace`` اللي مالوش أي صفحة عامة — بيظهر بس لتاجر تاني بيدوّر برقم القطعة.
دلوقتي:
  * ``public_stock_items()`` = الأصناف المتاحة اللي بتتعرض في سوق قطع الغيار.
  * ``place_order()`` = العميل يدوس «اطلب القطعة» ⇒ فاتورة «عرض سعر (مسودة)» بقناة
    «سوق Mouss Tec» في سيستم الشركة نفسها + إشعار للشركة (إيميل/واتساب) وللعميل.
    الشركة بتراجع وتعتمد الفاتورة، وساعتها بس المخزون بيتخصم (زي طلبات موقع FixIt).
  * الدفع بيتفق عليه مع المحل — مش داخل الـ Escrow في المرحلة دي.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q

logger = logging.getLogger('mouss_tec_core')

MAX_ORDERS_PER_HOUR = 10
MAX_NOTE_LEN = 500

# حالة القطعة في سوق الأفراد ⇒ حالة الصنف عند الشركات
_CONDITION_MAP = {
    'new': 'new',
    'used_excellent': 'used', 'used_good': 'used', 'used_fair': 'used',
    'refurbished': 'core',
}


class StockOrderError(Exception):
    pass


def public_stock_items():
    from clients.models import GlobalB2BMarketplace
    return (GlobalB2BMarketplace.objects
            .filter(available_qty__gt=0, wholesale_price__gt=0,
                    tenant__is_active=True, tenant__is_marketplace_active=True,
                    tenant__is_fraud_flagged=False, tenant__industry='automotive')
            .exclude(tenant__schema_name='public')
            .select_related('tenant'))


def filter_stock_items(qs, *, q='', make=None, condition='', sort='new'):
    if q:
        # 🐛 [FIX]: بحث متسامح — كل كلمة لوحدها (فلتر زيت تويوتا)، والهمزات/ى/ة، والشرط والمسافات
        #    في رقم القطعة (04152-YZZA1 = 04152YZZA1) والأرقام العربي.
        from clients.services.search_text import text_filter
        qs = qs.filter(text_filter(q, ('product_name', 'part_number', 'brand', 'car_model')))
    if make is not None:
        qs = qs.filter(Q(brand__iexact=make.name) | Q(brand__iexact=make.slug)
                       | Q(product_name__icontains=make.name))
    if condition:
        mapped = _CONDITION_MAP.get(condition)
        qs = qs.filter(condition=mapped) if mapped else qs.none()
    if sort == 'price_low':
        return qs.order_by('wholesale_price', '-updated_at')
    if sort == 'price_high':
        return qs.order_by('-wholesale_price', '-updated_at')
    return qs.order_by('-tenant__is_verified_merchant', '-updated_at')


def _tenant_link(tenant, path):
    from clients.models import Domain
    dom = (Domain.objects.filter(tenant=tenant, is_primary=True).first()
           or Domain.objects.filter(tenant=tenant).first())
    return f"https://{dom.domain}{path}" if dom else ''


def _notify_tenant(tenant, subject, body):
    """إيميل + واتساب لصاحب الشركة — best-effort: مايوقّفش الطلب لو الإرسال فشل."""
    from django.core.mail import send_mail
    try:
        if tenant.email:
            send_mail(subject, body, None, [tenant.email], fail_silently=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[PARTS STOCK] email to %s failed: %s", tenant.schema_name, exc)
    try:
        from clients.services.whatsapp_notify import send_platform_whatsapp
        if tenant.phone:
            send_platform_whatsapp(tenant.phone, text=f"Mouss Tec — {subject}\n{body}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[PARTS STOCK] whatsapp to %s failed: %s", tenant.schema_name, exc)


def place_order(item, customer, quantity, note=''):
    """طلب العميل ⇒ فاتورة عرض سعر في سيستم الشركة. بيرجّع dict فيه رقم الفاتورة.

    الكمية بتتقارن بالرصيد الحقيقي في سيستم الشركة دلوقتي (مش الرقم المتخزّن في السوق
    اللي ممكن يكون متأخر لو الـ Celery واقف).
    """
    from django.urls import reverse
    from django_tenants.utils import schema_context

    from clients.models import PlatformEvent
    from erp_core.localization import current_tenant_symbol as _sym

    if customer is None:
        raise StockOrderError('سجّل دخول الأول عشان تطلب القطعة.')
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise StockOrderError('الكمية مش مظبوطة.')
    if quantity < 1:
        raise StockOrderError('الكمية لازم تكون ١ على الأقل.')
    note = (note or '').strip()[:MAX_NOTE_LEN]

    rate_key = f'pstock_orders:{customer.pk}'
    if (cache.get(rate_key) or 0) >= MAX_ORDERS_PER_HOUR:
        raise StockOrderError('طلبات كتير في وقت قصير — استنى شوية وجرّب تاني.')

    tenant = item.tenant
    marker = f"[سوق Mouss Tec #{item.pk}]"
    with schema_context(tenant.schema_name):
        from inventory.models import Branch, Customer, Inventory, Product, SaleInvoice, SaleInvoiceItem
        product = Product.objects.filter(part_number=item.part_number).first()
        if product is None or not product.is_active or not product.is_b2b_published:
            raise StockOrderError('الصنف ده مبقاش متاح عند المحل.')
        stock = product.total_inventory_qty
        if stock <= 0:
            raise StockOrderError('الصنف ده خلص من عند المحل.')
        if quantity > stock:
            raise StockOrderError(f'المتاح عند المحل {stock} بس.')
        top = (Inventory.objects.filter(product=product, quantity__gt=0)
               .select_related('branch').order_by('-quantity').first())
        branch = top.branch if top else Branch.objects.order_by('pk').first()
        if branch is None:
            raise StockOrderError('المحل لسه ماجهّزش فرع لاستلام الطلبات.')

        with transaction.atomic():
            buyer, _ = Customer.get_or_create_by_phone(
                customer.phone, defaults={'name': customer.full_name or 'عميل سوق Mouss Tec'})
            if buyer.is_walk_in:
                raise StockOrderError('رقم الموبايل ده مش صالح للطلب.')
            lines = [
                f"{marker} طلب من سوق قطع الغيار على Mouss Tec 🛒",
                f"العميل: {customer.full_name} — {customer.phone}"
                + (f" — {customer.city}" if getattr(customer, 'city', '') else ''),
                f"الكمية: {quantity} × {item.wholesale_price} {_sym()} (سعر السوق المعتمد)",
                "الدفع والتسليم بالاتفاق مع العميل.",
            ]
            if note:
                lines.append(f"ملاحظات العميل: {note}")
            invoice = SaleInvoice.objects.create(
                invoice_type='sale', sales_channel='marketplace', status='quotation',
                customer=buyer, branch=branch, notes='\n'.join(lines),
            )
            SaleInvoiceItem.objects.create(
                invoice=invoice, product=product, quantity=quantity, unit_price=item.wholesale_price,
            )
            invoice.update_total()
        invoice_id = invoice.pk
        try:
            admin_path = reverse('admin:inventory_saleinvoice_change', args=[invoice_id])
        except Exception:  # noqa: BLE001
            admin_path = ''

    cache.set(rate_key, (cache.get(rate_key) or 0) + 1, 3600)
    link = _tenant_link(tenant, admin_path) if admin_path else ''
    _notify_tenant(
        tenant, f"🛒 طلب جديد من سوق Mouss Tec — {item.product_name[:60]}",
        '\n'.join(x for x in (
            f"العميل {customer.full_name} ({customer.phone}) طلب {quantity} × «{item.product_name}» "
            f"({item.part_number}) بسعر {item.wholesale_price} {_sym()}.",
            f"اتسجّلت فاتورة عرض سعر #{invoice_id} — راجعها واتواصل مع العميل.",
            note and f"ملاحظات العميل: {note}",
            link,
        ) if x),
    )
    try:
        from clients.services.customer_notify import notify_customer
        notify_customer(
            customer, title='✅ طلبك وصل للمحل',
            body=f'«{item.product_name[:80]}» ×{quantity} — {tenant.name} هيتواصل معاك على رقمك.',
            level='success', icon='fa-store', action_url='/marketplace/parts/', action_label='سوق قطع الغيار')
    except Exception:  # noqa: BLE001
        logger.exception("[PARTS STOCK] customer notify failed")
    try:
        PlatformEvent.objects.create(
            event_type='other', tenant_schema='public', tenant_name='parts_market',
            user_name=customer.full_name[:100],
            description=f"🏬 طلب من مخازن التجار: «{item.product_name[:60]}» ×{quantity} — {tenant.name} "
                        f"(فاتورة #{invoice_id})",
        )
    except Exception:  # noqa: BLE001
        logger.exception("[PARTS STOCK] platform event failed")
    return {'invoice_id': invoice_id, 'tenant_name': tenant.name}
