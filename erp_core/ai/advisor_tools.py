"""
🧠 Cognitive Advisor — Tool Layer
=====================================================================
الـ Tools الأربعة اللي بيستدعيها موديل Gemini عبر Function Calling.

⚠️  قاعدة أمنية صارمة:
   كل الـ Tools دي بتشتغل على schema المستأجر الحالي فقط (Tenant Isolation).
   مفيش أي query بتعدي حدود الـ tenant. لو الـ request جاي من خارج tenant
   صحيح، الـ wrapper view بيرفض قبل ما يوصل هنا.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db import connection
from django.db.models import F, Sum
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _current_schema() -> str:
    """يجيب اسم schema الـ tenant الحالي عشان نأكد إن الـ tool شغال في السياق الصح."""
    return getattr(connection, 'schema_name', 'public')


def _money(value) -> float:
    """يحول Decimal لـ float بدقة 2 منازل عشان الـ JSON serialization."""
    if value is None:
        return 0.0
    try:
        return float(Decimal(str(value)).quantize(Decimal('0.01')))
    except Exception:
        return 0.0


# =============================================================================
# 1️⃣  Cash Flow Projection
# =============================================================================
def calculate_cash_flow_projections(days_ahead: int = 30) -> dict[str, Any]:
    """
    لو لميت المستحقات اللي على العملاء في الفواتير الآجلة، الكاش هيبقى كام؟

    Returns:
        dict مفصّل: الكاش الحالي + المستحقات + التوقع الإجمالي.
    """
    try:
        from inventory.models import FinancialTransaction, SaleInvoice, Treasury

        # 💰 الكاش الموجود حالياً في كل الخزن
        treasuries = Treasury.objects.all()
        current_cash_by_treasury = []
        total_current_cash = Decimal('0.00')

        for t in treasuries:
            inflow = FinancialTransaction.objects.filter(
                treasury=t, transaction_type='in'
            ).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
            outflow = FinancialTransaction.objects.filter(
                treasury=t, transaction_type='out'
            ).aggregate(s=Sum('amount'))['s'] or Decimal('0.00')
            balance = inflow - outflow
            total_current_cash += balance
            current_cash_by_treasury.append({
                'treasury': t.name,
                'balance': _money(balance),
            })

        # 📋 الفواتير اللي عليها مستحقات (مش متسددة بالكامل)
        outstanding_invoices = SaleInvoice.objects.filter(
            status='posted',
        ).exclude(maintenance_contract__isnull=False)

        total_receivables = Decimal('0.00')
        invoices_breakdown = []
        cutoff = timezone.now() - timedelta(days=days_ahead)

        for inv in outstanding_invoices.select_related('customer'):
            due = Decimal(str(inv.total_amount or 0)) - Decimal(str(inv.paid_amount or 0))
            if due <= 0:
                continue
            total_receivables += due
            invoices_breakdown.append({
                'invoice_id': inv.id,
                'customer': inv.customer.name if inv.customer else 'غير محدد',
                'due': _money(due),
                'is_overdue': inv.date_created < cutoff,
            })

        # ترتيب أعلى مستحق أول
        invoices_breakdown.sort(key=lambda r: r['due'], reverse=True)

        projection = total_current_cash + total_receivables

        return {
            'success': True,
            'schema': _current_schema(),
            'current_cash': _money(total_current_cash),
            'cash_by_treasury': current_cash_by_treasury,
            'total_receivables': _money(total_receivables),
            'projected_cash_if_all_collected': _money(projection),
            'outstanding_invoices_count': len(invoices_breakdown),
            'top_outstanding': invoices_breakdown[:10],
            'notes': (
                f'لو اتحصّلت كل مستحقات العملاء ({len(invoices_breakdown)} فاتورة)، '
                f'الكاش هيوصل لـ {_money(projection):,.2f} ج.م.'
            ),
        }
    except Exception as e:
        logger.exception('[ADVISOR TOOL] calculate_cash_flow_projections failed')
        return {'success': False, 'error': f'تعذر حساب توقعات الكاش: {e}'}


# =============================================================================
# 2️⃣  Inventory Sale Simulation
# =============================================================================
def simulate_inventory_sale(percentage: float = 20.0) -> dict[str, Any]:
    """
    لو بعت X% من المخزون الراكد بسعر السوق، الأرباح المتوقعة كام؟

    Args:
        percentage: النسبة المئوية اللي عاوز تبيعها من كل قطعة راكدة (1-100).
    """
    try:
        from inventory.models import Inventory, Product

        pct = max(1.0, min(100.0, float(percentage)))
        ratio = Decimal(str(pct / 100.0))

        # Dead stock: قطع موجودة بكميات ومحدش اشتراها آخر 90 يوم
        dead = _query_dead_stock_products()

        total_units_to_sell = 0
        total_revenue = Decimal('0.00')
        total_cost = Decimal('0.00')
        sample_products = []

        for row in dead:
            qty_to_sell = int(Decimal(str(row['qty'])) * ratio)
            if qty_to_sell < 1:
                continue
            unit_price = Decimal(str(row['retail_price']))
            unit_cost = Decimal(str(row['avg_cost']))
            revenue = unit_price * qty_to_sell
            cost = unit_cost * qty_to_sell

            total_units_to_sell += qty_to_sell
            total_revenue += revenue
            total_cost += cost

            if len(sample_products) < 8:
                sample_products.append({
                    'product': row['name'],
                    'part_number': row['part_number'],
                    'qty_to_sell': qty_to_sell,
                    'expected_revenue': _money(revenue),
                    'expected_profit': _money(revenue - cost),
                })

        net_profit = total_revenue - total_cost
        margin = (float(net_profit) / float(total_revenue) * 100.0) if total_revenue > 0 else 0.0

        return {
            'success': True,
            'schema': _current_schema(),
            'percentage_simulated': pct,
            'dead_stock_skus': len(dead),
            'total_units_to_sell': total_units_to_sell,
            'expected_revenue': _money(total_revenue),
            'expected_cost': _money(total_cost),
            'expected_net_profit': _money(net_profit),
            'profit_margin_percent': round(margin, 2),
            'sample_breakdown': sample_products,
            'notes': (
                f'لو بعت {pct:.0f}% من المخزون الراكد ({len(dead)} صنف)، '
                f'متوقع تدخل {_money(total_revenue):,.2f} ج.م '
                f'وتحقق ربح صافي {_money(net_profit):,.2f} ج.م.'
            ),
        }
    except Exception as e:
        logger.exception('[ADVISOR TOOL] simulate_inventory_sale failed')
        return {'success': False, 'error': f'تعذرت محاكاة بيع المخزون: {e}'}


# =============================================================================
# 3️⃣  Dead Stock Report
# =============================================================================
def get_dead_stock_report(days_no_sale: int = 90, limit: int = 25) -> dict[str, Any]:
    """
    تقرير بالأصناف الراكدة (موجودة في المخزن ومحدش اشتراها آخر N يوم).
    """
    try:
        dead = _query_dead_stock_products(days_no_sale=days_no_sale)

        # ترتيب: الأكثر تكلفة محبوسة الأول
        for r in dead:
            r['locked_capital'] = _money(Decimal(str(r['avg_cost'])) * Decimal(str(r['qty'])))
        dead.sort(key=lambda r: r['locked_capital'], reverse=True)

        total_locked = sum(r['locked_capital'] for r in dead)

        return {
            'success': True,
            'schema': _current_schema(),
            'days_threshold': days_no_sale,
            'total_dead_skus': len(dead),
            'total_capital_locked': _money(total_locked),
            'items': dead[:limit],
            'notes': (
                f'فيه {len(dead)} صنف راكد آخر {days_no_sale} يوم، '
                f'وحابس عندك كاش بقيمة {_money(total_locked):,.2f} ج.م.'
            ),
        }
    except Exception as e:
        logger.exception('[ADVISOR TOOL] get_dead_stock_report failed')
        return {'success': False, 'error': f'تعذر توليد تقرير الراكد: {e}'}


def _query_dead_stock_products(days_no_sale: int = 90) -> list[dict]:
    """
    Helper مشترك بين dead_stock + simulate_sale.
    بيرجع قائمة dicts (مش querysets) عشان serialization آمن.
    """
    from inventory.models import Inventory, Product, SaleInvoiceItem

    cutoff = timezone.now() - timedelta(days=days_no_sale)

    # IDs اللي اتباعت آخر N يوم
    sold_recently_ids = set(
        SaleInvoiceItem.objects.filter(
            invoice__date_created__gte=cutoff,
            invoice__status='posted',
        ).values_list('product_id', flat=True).distinct()
    )

    # الأصناف اللي مش في القائمة دي + عندها مخزون
    qs = (
        Product.objects.filter(is_active=True)
        .exclude(id__in=sold_recently_ids)
        .annotate(total_qty=Sum('inventory__quantity'))
        .filter(total_qty__gt=0)
        .values('id', 'name', 'part_number', 'retail_price', 'average_cost', 'total_qty')
    )

    return [
        {
            'id': r['id'],
            'name': r['name'],
            'part_number': r['part_number'],
            'qty': int(r['total_qty'] or 0),
            'retail_price': _money(r['retail_price']),
            'avg_cost': _money(r['average_cost']),
        }
        for r in qs
    ]


# =============================================================================
# 4️⃣  Report Link Generator
# =============================================================================
# Whitelist للـ report types — مفيش any user input بيتمرر مباشرة لـ reverse()
_REPORT_ROUTES = {
    'customer_detail': ('super_admin_customer_detail', ['customer_id']),
    'super_admin_dashboard': ('super_admin_dashboard', []),
    'inventory_dashboard': ('inventory:dashboard', []),
    'b2b_marketplace': ('inventory:b2b_marketplace', []),
    'pos': ('inventory:pos_interface', []),
    'bank_reconciliation': ('inventory:bank_reconciliation', []),
}


def generate_report_link(report_type: str, customer_id: int | None = None) -> dict[str, Any]:
    """
    يولّد رابط حقيقي من URL routing للسيستم — مش string ثابت.
    الـ AI بيستدعيها لما عاوز يدي اللينك للمستخدم في الرد.
    """
    try:
        if report_type not in _REPORT_ROUTES:
            return {
                'success': False,
                'error': f'نوع التقرير "{report_type}" غير مدعوم.',
                'available_types': list(_REPORT_ROUTES.keys()),
            }

        url_name, required_args = _REPORT_ROUTES[report_type]
        kwargs = {}

        if 'customer_id' in required_args:
            if not customer_id:
                return {
                    'success': False,
                    'error': f'التقرير "{report_type}" محتاج customer_id.',
                }
            kwargs['customer_id'] = int(customer_id)

        url = reverse(url_name, kwargs=kwargs) if kwargs else reverse(url_name)

        # عناوين عربية للأنواع المختلفة عشان نظهرها في الـ HTML
        labels = {
            'customer_detail': 'صفحة العميل',
            'super_admin_dashboard': 'لوحة السوبر آدمن',
            'inventory_dashboard': 'لوحة المخزون',
            'b2b_marketplace': 'سوق B2B',
            'pos': 'نقطة البيع (POS)',
            'bank_reconciliation': 'مطابقة كشف البنك',
        }
        label = labels.get(report_type, report_type)

        return {
            'success': True,
            'url': url,
            'label': label,
            'html': f'<a href="{url}" class="advisor-link">📊 {label}</a>',
        }
    except NoReverseMatch as e:
        return {'success': False, 'error': f'مسار التقرير غير موجود: {e}'}
    except Exception as e:
        logger.exception('[ADVISOR TOOL] generate_report_link failed')
        return {'success': False, 'error': f'تعذر توليد الرابط: {e}'}


# =============================================================================
# Tool Registry (يستخدم في Function Calling)
# =============================================================================
TOOL_REGISTRY = {
    'calculate_cash_flow_projections': calculate_cash_flow_projections,
    'simulate_inventory_sale': simulate_inventory_sale,
    'get_dead_stock_report': get_dead_stock_report,
    'generate_report_link': generate_report_link,
}

# Schema بصيغة Gemini Function Declarations (لـ Stage 2)
GEMINI_FUNCTION_DECLARATIONS = [
    {
        'name': 'calculate_cash_flow_projections',
        'description': (
            'يحسب توقعات الكاش لو اتحصلت كل المستحقات على العملاء من الفواتير الآجلة. '
            'يرد بالكاش الحالي في الخزن، إجمالي المستحقات، والكاش المتوقع.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'days_ahead': {
                    'type': 'integer',
                    'description': 'فترة التوقع بالأيام (default: 30).',
                },
            },
        },
    },
    {
        'name': 'simulate_inventory_sale',
        'description': (
            'محاكاة بيع نسبة محددة من المخزون الراكد بسعر السوق الحالي، '
            'وحساب الإيراد والربح المتوقع.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'percentage': {
                    'type': 'number',
                    'description': 'النسبة المئوية للبيع من 1 لـ 100 (مثال: 20).',
                },
            },
            'required': ['percentage'],
        },
    },
    {
        'name': 'get_dead_stock_report',
        'description': (
            'تقرير الأصناف الراكدة اللي عندها مخزون ومحدش اشتراها في فترة معينة. '
            'يرجع الكاش المحبوس في كل صنف.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'days_no_sale': {
                    'type': 'integer',
                    'description': 'عدد الأيام اللي تعتبر الصنف راكد بعدها (default: 90).',
                },
                'limit': {
                    'type': 'integer',
                    'description': 'أقصى عدد أصناف ترجع في النتيجة (default: 25).',
                },
            },
        },
    },
    {
        'name': 'generate_report_link',
        'description': (
            'يولّد رابط حقيقي لصفحة داخل السيستم (تقرير، عميل، لوحة...). '
            'استدعيها لما تحب تدي للمستخدم لينك مباشر في رد المساعد.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'report_type': {
                    'type': 'string',
                    'enum': list(_REPORT_ROUTES.keys()),
                    'description': 'نوع الصفحة المطلوبة.',
                },
                'customer_id': {
                    'type': 'integer',
                    'description': 'مطلوب فقط لما report_type = customer_detail.',
                },
            },
            'required': ['report_type'],
        },
    },
]
