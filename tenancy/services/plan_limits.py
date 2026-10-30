"""
📏 حدود الباقة العددية — كروت الصيانة الشهرية وعدد الأصناف
============================================================
باقة Silver بتتباع بـ «150 كرت صيانة/شهر» و«مخزون حتى 500 صنف» — الأرقام دي في
entitlements الباقة (workshop_repair_cards / inventory_items) بس ماكانش فيه أي كود
بيطبّقها، فعميل Silver كان واخد Gold ببلاش.

الفحص هنا في نقط الإنشاء اللي المستخدم بيعملها بإيده (أمر الشغل، إضافة صنف،
استيراد إكسيل، فورم الأدمن) — مش في pre_save: الاستيراد الآلي (Wix / FixIt /
الروبوت / إنشاء صنف من فاتورة شراء) مايتقطعش في النص وتضيع بيانات.

``monthly_limit`` = 0 أو مش موجود ⇒ غير محدود (نفس اتفاقية الباقات).
الاشتراكات القديمة محتفظة بالمزايا لحد التجديد (clients.0087 — grandfathering).
"""
from __future__ import annotations

from django.db import connection
from django.utils import timezone

UPGRADE_HINT = 'رقّي باقتك من «إدارة الاشتراك» عشان تكمل.'


def _tenant():
    tenant = getattr(connection, 'tenant', None)
    if tenant is None or getattr(tenant, 'schema_name', 'public') == 'public':
        return None
    return tenant


def _limit(tenant, code):
    from clients.services.entitlements import EntitlementService
    if EntitlementService._get_subscription(tenant) is None:
        return None   # شركة قديمة من غير اشتراك — مانقفلش عليها حاجة من غير ما المالك يراجع
    limit = EntitlementService.limit(tenant, code)
    return limit if limit and limit > 0 else None


def repair_cards_block(tenant=None):
    """رسالة رفض لو كروت الصيانة الشهر ده وصلت حد الباقة، وإلا None."""
    tenant = tenant or _tenant()
    if tenant is None:
        return None
    limit = _limit(tenant, 'workshop_repair_cards')
    if limit is None:
        return None
    from inventory.models import SaleInvoice
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    used = SaleInvoice.objects.filter(
        invoice_type='maintenance', is_return=False, date_created__gte=month_start).count()
    if used < limit:
        return None
    return (f'🚫 وصلت لحد باقتك من كروت الصيانة الشهر ده ({limit} كرت). '
            f'تقدر تعمل فاتورة بيع عادية، أو {UPGRADE_HINT}')


def inventory_items_block(tenant=None, adding=1):
    """رسالة رفض لو إضافة ``adding`` صنف جديد هتعدّي حد الأصناف في الباقة، وإلا None."""
    tenant = tenant or _tenant()
    if tenant is None:
        return None
    limit = _limit(tenant, 'inventory_items')
    if limit is None:
        return None
    from inventory.models import Product
    current = Product.objects.filter(is_active=True).count()
    if current + max(int(adding or 1), 1) <= limit:
        return None
    left = max(limit - current, 0)
    return (f'🚫 باقتك فيها لحد {limit} صنف (عندك {current}، فاضل {left}). {UPGRADE_HINT}')
