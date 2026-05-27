"""
📊 Reporting Service — Owns all dashboard & copilot data aggregation.

Responsibilities:
- Automotive dashboard stats (revenue, profit, treasury, stock alerts)
- Printing dashboard stats
- Copilot live context for AI agents
- Business data query engine (customer, invoice, sales, expenses lookup)
"""

import json
import re
import logging
from decimal import Decimal
from datetime import timedelta

from django.db import connection
from django.db.models import Sum, F, Q
from django.utils import timezone

logger = logging.getLogger('mouss_tec_core')


class ReportingService:
    """Centralized data aggregation for dashboards and AI copilot."""

    # ------------------------------------------------------------------
    # Copilot Live Context (used by AI agents)
    # ------------------------------------------------------------------
    @staticmethod
    def get_live_context():
        """Build a snapshot of live business data for the AI copilot."""
        try:
            from inventory.models import (
                SaleInvoice, Customer, Treasury, FinancialTransaction,
                Product, Inventory,
            )
            now = timezone.now()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            sales_today = SaleInvoice.objects.filter(date_created__gte=today_start, status='posted')
            sales_month = SaleInvoice.objects.filter(date_created__gte=month_start, status='posted')
            revenue_today = sales_today.aggregate(t=Sum('total_amount'))['t'] or 0
            revenue_month = sales_month.aggregate(t=Sum('total_amount'))['t'] or 0
            profit_month = sales_month.aggregate(t=Sum('net_profit'))['t'] or 0

            treasuries = Treasury.objects.filter(is_active=True)
            treasury_info = ", ".join(f"{t.name}: {t.balance:,.2f}" for t in treasuries)
            total_balance = sum(t.balance for t in treasuries)

            expenses_month = FinancialTransaction.objects.filter(
                transaction_type='out',
                date__gte=month_start,
                sale_invoice__isnull=True,
                purchase_invoice__isnull=True,
            ).aggregate(t=Sum('amount'))['t'] or 0

            total_customers = Customer.objects.count()
            recent = Customer.objects.order_by('-date_added')[:5]
            customers_list = ", ".join(f"{c.name}" for c in recent)

            low_stock = Inventory.objects.filter(quantity__lte=F('product__min_stock_level'))[:5]
            low_items = ", ".join(f"{i.product.name} ({i.quantity})" for i in low_stock)

            open_invoices = SaleInvoice.objects.filter(
                status__in=['quotation', 'in_progress', 'quality_check', 'ready']
            ).count()

            return (
                f"## البيانات الحية:\n"
                f"{now.strftime('%Y-%m-%d %H:%M')}\n"
                f"إيرادات اليوم: {revenue_today:,.2f} ج.م | الشهر: {revenue_month:,.2f} ج.م\n"
                f"صافي ربح الشهر: {profit_month:,.2f} ج.م\n"
                f"مصروفات الشهر: {expenses_month:,.2f} ج.م\n"
                f"الخزائن: {treasury_info} | الإجمالي: {total_balance:,.2f} ج.م\n"
                f"فواتير مفتوحة: {open_invoices}\n"
                f"إجمالي العملاء: {total_customers} | آخرهم: {customers_list}\n"
                f"تنبيهات مخزون: {low_items or 'لا يوجد نقص'}\n"
            )
        except Exception as e:
            logger.warning("[REPORTING] Live context error: %s", e)
            return ""

    # ------------------------------------------------------------------
    # Copilot Business Data Query Engine
    # ------------------------------------------------------------------
    @staticmethod
    def query_business_data(query):
        """
        Parse a user question and fetch relevant data from the database.
        Returns a formatted string or None if no match.
        """
        try:
            from inventory.models import (
                SaleInvoice, Customer, Treasury, FinancialTransaction,
                Product, Inventory, Vehicle,
            )
            now = timezone.now()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            q = query.lower()

            # --- Customer search ---
            result = ReportingService._search_customer(q, Customer, Vehicle, SaleInvoice)
            if result:
                return result

            # --- Invoice lookup ---
            result = ReportingService._search_invoice(q, SaleInvoice)
            if result:
                return result

            # --- Sales ---
            if any(k in q for k in ['بيع', 'مبيعات', 'ايراد', 'إيراد', 'بعنا', 'بيعنا', 'revenue', 'sales']):
                if any(k in q for k in ['الشهر', 'شهر']):
                    sales = SaleInvoice.objects.filter(date_created__gte=month_start, status='posted')
                else:
                    sales = SaleInvoice.objects.filter(date_created__gte=today_start, status='posted')
                total = sales.aggregate(t=Sum('total_amount'))['t'] or 0
                profit = sales.aggregate(t=Sum('net_profit'))['t'] or 0
                return f"المبيعات: {total:,.2f} ج.م | صافي الربح: {profit:,.2f} ج.م | عدد الفواتير: {sales.count()}"

            # --- Expenses ---
            if any(k in q for k in ['مصاريف', 'مصروف', 'expense']):
                expenses = FinancialTransaction.objects.filter(
                    transaction_type='out',
                    date__gte=month_start,
                    sale_invoice__isnull=True,
                    purchase_invoice__isnull=True,
                )
                total = expenses.aggregate(t=Sum('amount'))['t'] or 0
                return f"إجمالي المصروفات هذا الشهر: {total:,.2f} ج.م"

            # --- Treasury ---
            if any(k in q for k in ['خزينة', 'خزنة', 'رصيد', 'كاش', 'balance', 'فلوس']):
                treasuries = Treasury.objects.filter(is_active=True)
                total = sum(t.balance for t in treasuries)
                details = "\n".join(f"  • {t.name}: {t.balance:,.2f} ج.م" for t in treasuries)
                return f"رصيد الخزائن:\n{details}\nالإجمالي: {total:,.2f} ج.م"

            # --- Profits ---
            if any(k in q for k in ['ربح', 'أرباح', 'ارباح', 'كسب', 'profit']):
                sales = SaleInvoice.objects.filter(date_created__gte=month_start, status='posted')
                profit = sales.aggregate(t=Sum('net_profit'))['t'] or 0
                revenue = sales.aggregate(t=Sum('total_amount'))['t'] or 0
                return f"إيرادات الشهر: {revenue:,.2f} ج.م | صافي الربح: {profit:,.2f} ج.م | عدد الفواتير: {sales.count()}"

            # --- Inventory ---
            if any(k in q for k in ['مخزون', 'stock', 'قطعة', 'قطع']):
                total_products = Product.objects.count()
                low_stock = Inventory.objects.filter(quantity__lte=F('product__min_stock_level'))[:10]
                alerts = "\n".join(
                    f"  {i.product.name}: {i.quantity} (الحد: {i.product.min_stock_level})"
                    for i in low_stock
                )
                stock_status = "تنبيهات نقص:\n" + alerts if alerts else "لا يوجد نقص"
                return f"إجمالي القطع: {total_products}\n{stock_status}"

            return None
        except Exception as e:
            logger.warning("[REPORTING] Query error: %s", e)
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _search_customer(q, Customer, Vehicle, SaleInvoice):
        """Search for customer by name or keyword."""
        customer_match = re.search(r'(?:عميل|زبون|client)\s+(.+)', q)
        if not customer_match:
            for kw in ['بيانات', 'معلومات', 'ملف']:
                if kw in q:
                    rest = q.split(kw)[-1].strip()
                    if rest:
                        customer_match = type('M', (), {'group': lambda self, n: rest})()
                        break

        if customer_match:
            name = customer_match.group(1).strip()
            customers = Customer.objects.filter(Q(name__icontains=name) | Q(phone__icontains=name))[:5]
            if customers:
                details = []
                for c in customers:
                    vehicles = Vehicle.objects.filter(customer=c)
                    cars = ", ".join(f"{v.brand} {v.model_name}" for v in vehicles[:3]) or "لا توجد"
                    invoices_count = SaleInvoice.objects.filter(customer=c).count()
                    details.append(
                        f"• {c.name} | {c.phone} | رصيد: {c.balance:,.2f} | نقاط: {c.loyalty_points} | "
                        f"{c.vip_tier} | فواتير: {invoices_count} | سيارات: {cars}"
                    )
                return "نتائج البحث:\n" + "\n".join(details)
        return None

    @staticmethod
    def _search_invoice(q, SaleInvoice):
        """Search for a specific invoice by ID."""
        inv_match = re.search(r'(?:فاتور[ةه]|inv|invoice)\s*(?:رقم|#|no)?\s*#?(\d+)', q, re.IGNORECASE)
        if not inv_match:
            inv_match = re.search(r'(?:رقم|#)\s*(\d+)', q)
        if inv_match:
            inv_id = int(inv_match.group(1))
            try:
                inv = SaleInvoice.objects.get(pk=inv_id)
                profit_status = "ربح" if inv.net_profit > 0 else ("خسارة" if inv.net_profit < 0 else "تعادل")
                return (
                    f"فاتورة #{inv.id} — {inv.customer.name}\n"
                    f"النوع: {inv.get_invoice_type_display()} | الحالة: {inv.get_status_display()}\n"
                    f"الإجمالي: {inv.total_amount:,.2f} ج.م | المدفوع: {inv.paid_amount:,.2f} | المتبقي: {inv.due_amount:,.2f}\n"
                    f"التكلفة: {inv.total_cost:,.2f} ج.م | الربح: {inv.net_profit:,.2f} ج.م ({profit_status})"
                )
            except SaleInvoice.DoesNotExist:
                return f"لم أجد فاتورة برقم {inv_id}"
        return None
