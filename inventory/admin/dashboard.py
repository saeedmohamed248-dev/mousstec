from django.contrib import admin
from decimal import Decimal
from django.urls import reverse
from django.shortcuts import redirect
from django.utils.safestring import mark_safe
from django.utils.html import format_html
from django.db.models import Sum, F, Max, Avg, Count
from django.utils import timezone
from datetime import timedelta
import datetime
import json
import urllib.parse
from django.contrib import messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from import_export.admin import ImportExportModelAdmin 
from django.utils.translation import gettext_lazy as _ 
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django_tenants.utils import schema_context 

# 🟢 استدعاء الجداول الأساسية للمنظومة التشغيلية
from ..models import (Branch, Product, Inventory, PurchaseInvoice, SaleInvoice,
                     PurchaseInvoiceItem, SaleInvoiceItem, StockTransfer,
                     Treasury, ExpenseCategory, FinancialTransaction, EmployeeProfile,
                     Customer, Vendor, Vehicle,
                     ServiceCatalog, SaleInvoiceServiceItem, VehicleInspection,
                     MaintenanceContract,
                     AuditLog, ChartOfAccount, AccountingEntry,
                     InventoryMovement, StockAlert, ImportSession,
                     ScrapDismantlingJob, ScrapDismantlingYield,
                     B2BListingRequest)

# استدعاء جداول الإمبراطورية لربط سوق التجار المركزي (B2B)
try:
    from clients.models import GlobalB2BMarketplace, Client
except ImportError:
    GlobalB2BMarketplace = None

import logging
logger = logging.getLogger('mouss_tec_core')


from .mixins import *  # noqa: F401, F403
# Chameleon dashboard engine + admin reports/analytics surface.

# =====================================================================
# 📊 10. محرك الداش بورد المركزي الحربائي المتغير (Chameleon Dashboard)
# =====================================================================
original_index = admin.site.index

def MoussTec_dashboard_index(request, extra_context=None):
    extra_context = extra_context or {}

    # 🐛 [Issue #2 FIX]: Branch employees logging in via /secure-portal/login/
    # used to land here (Django admin index). They're not superusers, so the
    # admin view shows them an empty page or a "no permissions" notice — which
    # looks like a logout.
    # ─────────────────────────────────────────────────────────────────────────
    # Rule: any authenticated non-superuser on a tenant schema gets routed to
    # the branch dashboard (or the role-specific workspace via
    # smart_post_login_redirect). Superusers and public-schema requests fall
    # through to the original admin index.
    if (
        request.user.is_authenticated
        and not request.user.is_superuser
        and getattr(connection, 'schema_name', 'public') != 'public'
    ):
        from django.shortcuts import redirect
        # Use the smart router so HR/tech/etc. land on their proper workspace
        # rather than always /system/dashboard/.
        return redirect('/auth/redirect/')

    if connection.schema_name == 'public':
        extra_context['branch_name'] = "غرفة عمليات Mouss Tec المركزية السحابية"
        return original_index(request, extra_context)

    tenant_industry = getattr(connection.tenant, 'industry', 'automotive')
    extra_context['industry'] = tenant_industry

    if tenant_industry == 'printing':
        return _printing_dashboard(request, extra_context)
    else:
        return _automotive_dashboard(request, extra_context)


def _printing_dashboard(request, extra_context):
    """داشبورد المطابع والتصميم"""
    from django.apps import apps
    PrintOrder = apps.get_model('printing', 'PrintOrder')
    PrintJob = apps.get_model('printing', 'PrintJob')
    PrintTreasury = apps.get_model('printing', 'PrintTreasury')
    PrintTransaction = apps.get_model('printing', 'PrintTransaction')
    PrintMaterial = apps.get_model('printing', 'PrintMaterial')
    PrintCustomer = apps.get_model('printing', 'PrintCustomer')
    DesignerModel = apps.get_model('printing', 'Designer')
    DesignerWorkLog = apps.get_model('printing', 'DesignerWorkLog')

    today = timezone.now().date()
    first_day_of_month = today.replace(day=1)

    extra_context['business_type'] = 'printing'
    extra_context['branch_name'] = "مركز إدارة المطبعة"

    month_orders = PrintOrder.objects.filter(date_created__gte=first_day_of_month)
    total_revenue = month_orders.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    total_paid = month_orders.aggregate(Sum('paid_amount'))['paid_amount__sum'] or 0
    total_debt = float(total_revenue) - float(total_paid)

    today_orders = PrintOrder.objects.filter(date_created__date=today)
    today_count = today_orders.count()
    pending_orders = PrintOrder.objects.filter(status__in=['draft', 'confirmed', 'in_progress']).count()
    delivered_month = month_orders.filter(status='delivered').count()

    designers_count = DesignerModel.objects.count()
    today_design_hours = DesignerWorkLog.objects.filter(date=today).aggregate(
        total=Sum('duration_hours'))['total'] or 0
    month_design_logs = DesignerWorkLog.objects.filter(date__gte=first_day_of_month).count()

    treasuries_data = []
    total_treasury_balance = Decimal('0')
    for t in PrintTreasury.objects.filter(is_active=True):
        treasuries_data.append({
            'name': t.name,
            'type': 'خزينة مطبعة',
            'balance': float(t.balance),
            'is_negative': t.balance < 0,
        })
        total_treasury_balance += t.balance

    month_income = PrintTransaction.objects.filter(
        transaction_type='in', date__gte=first_day_of_month
    ).aggregate(Sum('amount'))['amount__sum'] or 0
    month_expenses = PrintTransaction.objects.filter(
        transaction_type='out', date__gte=first_day_of_month
    ).aggregate(Sum('amount'))['amount__sum'] or 0

    low_stock_materials = list(
        PrintMaterial.objects.filter(quantity__lte=F('min_stock')).values_list('name', 'quantity')[:5]
    )

    chart_labels = []
    chart_revenue = []
    chart_profit = []
    for i in range(5, -1, -1):
        target_month = today.replace(day=1) - timedelta(days=30*i)
        month_name = target_month.strftime("%B")
        m_orders = PrintOrder.objects.filter(
            date_created__year=target_month.year,
            date_created__month=target_month.month
        )
        rev = m_orders.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
        paid = m_orders.aggregate(Sum('paid_amount'))['paid_amount__sum'] or 0
        chart_labels.append(month_name)
        chart_revenue.append(float(rev))
        chart_profit.append(float(paid))

    recent_orders = list(
        PrintOrder.objects.order_by('-date_created')[:5].values(
            'id', 'customer__name', 'total_amount', 'status', 'date_created'
        )
    )
    for o in recent_orders:
        o['date_created'] = o['date_created'].strftime('%Y-%m-%d') if o['date_created'] else ''
        o['total_amount'] = float(o['total_amount'])
        status_map = {
            'draft': 'مسودة', 'confirmed': 'مؤكد', 'in_progress': 'قيد التنفيذ',
            'ready': 'جاهز للتسليم', 'delivered': 'تم التسليم', 'cancelled': 'ملغي'
        }
        o['status_display'] = status_map.get(o['status'], o['status'])

    extra_context.update({
        'stats': {
            'total_sales_today': f"{float(total_revenue):,.0f}",
            'net_profit_today': f"{float(total_paid):,.0f}",
            'total_debt': f"{total_debt:,.0f}",
            'total_treasury': f"{float(total_treasury_balance):,.0f}",
            'invoices_count': f"{pending_orders} طلبات قيد التنفيذ",
            'low_stock_count': len(low_stock_materials),
        },
        'treasuries_data': treasuries_data,
        'delayed_orders_count': 0,
        'chart_labels': json.dumps(chart_labels),
        'chart_revenue': json.dumps(chart_revenue),
        'chart_profit': json.dumps(chart_profit),
        'print_today_count': today_count,
        'print_pending': pending_orders,
        'print_delivered_month': delivered_month,
        'print_designers_count': designers_count,
        'print_today_design_hours': float(today_design_hours),
        'print_month_design_logs': month_design_logs,
        'print_month_income': float(month_income),
        'print_month_expenses': float(month_expenses),
        'print_low_stock_materials': low_stock_materials,
        'print_recent_orders': json.dumps(recent_orders, ensure_ascii=False),
        'print_customers_count': PrintCustomer.objects.count(),
    })

    return original_index(request, extra_context)


def _automotive_dashboard(request, extra_context):
    """داشبورد السيارات (الكود الأصلي)"""
    today = timezone.now().date()
    first_day_of_month = today.replace(day=1)

    tenant_business_type = getattr(connection.tenant, 'business_type', 'service_center')
    extra_context['business_type'] = tenant_business_type

    branch_name = "نظام إدارة الميدان"
    try:
        if hasattr(request.user, 'employee_profile') and request.user.employee_profile.branch:
            branch_name = request.user.employee_profile.branch.name
    except Exception: pass

    can_see_finance = request.user.is_superuser
    if not can_see_finance:
        try: can_see_finance = request.user.employee_profile.role in ['admin', 'manager']
        except Exception: pass

    # Resolve user branch once — used for both shared stats and treasury filter
    user_branch = None
    if not request.user.is_superuser:
        try:
            user_branch = request.user.employee_profile.branch
        except Exception:
            user_branch = None

    # 🔁 Unified KPI source — same logic as /system/dashboard/ branch_dashboard.
    # Headline cards (sales / profit / expenses / treasury) must match the
    # branch dashboard EXACTLY, so we pull them from ReportingService.
    from inventory.services.reporting_service import ReportingService
    today_stats = ReportingService.get_today_dashboard_stats(request.user, user_branch)
    total_revenue = today_stats['total_sales_today']
    net_profit = today_stats['net_profit_today']
    total_expenses_today = today_stats['total_expenses_today']
    low_stock_count = today_stats['low_stock_count']
    total_debt = Customer.objects.aggregate(Sum('balance'))['balance__sum'] or 0

    # 🐛 [Issue #3 FIX]: نفس ReportingService بيدّي الـ treasury_summary
    # عشان الـ admin والـ branch_dashboard ما يختلفوش في الإجمالي.
    treasury_summary = ReportingService.get_treasury_summary(request.user, user_branch)
    total_treasury_balance = treasury_summary['total_treasury_balance']
    treasuries_data = treasury_summary['treasuries_data'] if can_see_finance else []

    open_orders = SaleInvoice.objects.exclude(status='posted')
    if user_branch:
        open_orders = open_orders.filter(branch=user_branch)
    open_orders_count = open_orders.count()

    yesterday = timezone.now() - timedelta(days=1)
    delayed_orders_count = open_orders.filter(date_created__lte=yesterday).count()

    chart_labels = []
    chart_revenue = []
    chart_profit = []

    for i in range(5, -1, -1):
        target_month = today.replace(day=1) - timedelta(days=30*i)
        month_name = target_month.strftime("%B")

        month_invoices = SaleInvoice.objects.filter(
            status='posted',
            date_created__year=target_month.year,
            date_created__month=target_month.month
        )
        if user_branch:
            month_invoices = month_invoices.filter(branch=user_branch)

        rev = month_invoices.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
        prof = month_invoices.aggregate(Sum('net_profit'))['net_profit__sum'] or 0

        chart_labels.append(month_name)
        chart_revenue.append(float(rev))
        chart_profit.append(float(prof))

    if not can_see_finance:
        display_revenue = "🔒 مخفي"
        display_profit = "🔒 مخفي"
        display_expenses = "🔒 مخفي"
        display_debt = "🔒 مخفي"
        safe_chart_revenue = [0] * 6
        safe_chart_profit = [0] * 6
    else:
        display_revenue = f"{float(total_revenue):,.0f}"
        display_profit = f"{float(net_profit):,.0f}"
        display_expenses = f"{float(total_expenses_today):,.0f}"
        display_debt = f"{float(total_debt):,.0f}"
        safe_chart_revenue = chart_revenue
        safe_chart_profit = chart_profit

    if tenant_business_type == 'parts_dealer':
        invoices_label = f"{open_orders_count} طلبية جملة"
    elif tenant_business_type == 'scrap_importer':
        invoices_label = f"{open_orders_count} أنصاف تقطيع"
        display_debt = "حاسبة التقطيع السحابية"
    else:
        invoices_label = f"{open_orders_count} أوامر شغل مفتوحة"

    if can_see_finance:
        display_treasury = f"{float(total_treasury_balance):,.0f}"
    else:
        display_treasury = "🔒 مخفي"
        treasuries_data = []

    extra_context.update({
        'branch_name': branch_name,
        'business_type': tenant_business_type,
        'stats': {
            'total_sales_today': display_revenue,
            'net_profit_today': display_profit,
            'total_expenses_today': display_expenses,
            'total_debt': display_debt,
            'total_treasury': display_treasury,
            'invoices_count': invoices_label,
            'low_stock_count': low_stock_count,
        },
        'treasuries_data': treasuries_data,
        'delayed_orders_count': delayed_orders_count,
        'chart_labels': json.dumps(chart_labels),
        'chart_revenue': json.dumps(safe_chart_revenue),
        'chart_profit': json.dumps(safe_chart_profit),
    })

    return original_index(request, extra_context)

admin.site.index = MoussTec_dashboard_index


# =====================================================================
# 📊 التقارير والتحليلات (Admin Reports Dashboard)
# =====================================================================
from django.template.response import TemplateResponse
from django.http import HttpResponseForbidden
from django.db.models import Count, DecimalField, ExpressionWrapper

def admin_reports_view(request):
    """Custom admin reports page with comprehensive sales analytics."""
    if connection.schema_name == 'public':
        from django.shortcuts import redirect
        return redirect('/secure-portal/')

    # Permission check
    if not request.user.is_superuser:
        try:
            if request.user.employee_profile.role not in ('admin', 'manager'):
                return HttpResponseForbidden("غير مصرح")
        except Exception:
            return HttpResponseForbidden("غير مصرح")

    try:
        # Detect tenant industry for printing vs automotive
        try:
            from clients.models import Client
            tenant = Client.objects.filter(schema_name=connection.schema_name).first()
            if tenant and tenant.industry == 'printing':
                return _build_reports_response_printing(request)
        except Exception:
            pass
        return _build_reports_response(request)
    except Exception:
        logger.error(
            "Reports view crash [schema=%s user=%s]: %s",
            connection.schema_name, request.user,
            __import__('traceback').format_exc(),
        )
        from django.http import HttpResponse
        return HttpResponse(
            '<h2 style="font-family:Cairo,sans-serif;text-align:center;margin-top:60px;">'
            '⚠️ حدث خطأ أثناء تحميل التقارير.<br>'
            'تم تسجيل الخطأ وسيتم إصلاحه قريباً.<br><br>'
            '<a href="/secure-portal/">← العودة للوحة التحكم</a></h2>',
            status=500,
        )


def _build_reports_response(request):
    """Internal: builds the reports TemplateResponse (may raise)."""
    from_date_str = request.GET.get('from', '')
    to_date_str = request.GET.get('to', '')
    customer_id = request.GET.get('customer', '')

    today = timezone.now().date()
    first_of_month = today.replace(day=1)
    first_of_year = today.replace(month=1, day=1)

    try:
        from_date = timezone.datetime.strptime(from_date_str, '%Y-%m-%d').date() if from_date_str else first_of_month
        to_date = timezone.datetime.strptime(to_date_str, '%Y-%m-%d').date() if to_date_str else today
    except ValueError:
        from_date, to_date = first_of_month, today

    # Base queryset with branch isolation
    sales_base = SaleInvoice.objects.filter(status='posted', is_return=False)
    branch = None
    if not request.user.is_superuser:
        try:
            branch = request.user.employee_profile.branch
            if branch:
                sales_base = sales_base.filter(branch=branch)
        except Exception:
            pass

    # Period filter
    period_sales = sales_base.filter(
        date_created__date__gte=from_date, date_created__date__lte=to_date,
    )
    if customer_id:
        try:
            period_sales = period_sales.filter(customer_id=int(customer_id))
        except (ValueError, TypeError):
            pass

    def summarize(qs):
        agg = qs.aggregate(
            revenue=Sum('total_amount'),
            cost=Sum('total_cost'),
            profit=Sum('net_profit'),
            count=Count('id'),
        )
        return {k: float(v or 0) for k, v in agg.items()}

    today_summary = summarize(sales_base.filter(date_created__date=today))
    month_summary = summarize(sales_base.filter(date_created__date__gte=first_of_month))
    year_summary = summarize(sales_base.filter(date_created__date__gte=first_of_year))
    period_summary = summarize(period_sales)

    # Top 10 products by revenue
    top_products = list(
        SaleInvoiceItem.objects.filter(invoice__in=period_sales)
        .values('product__name', 'product__part_number')
        .annotate(
            total_revenue=Sum(
                ExpressionWrapper(F('quantity') * F('unit_price'), output_field=DecimalField())
            ),
            total_qty=Sum('quantity'),
            total_profit=Sum(
                ExpressionWrapper(
                    F('quantity') * (F('unit_price') - F('cost_at_sale')),
                    output_field=DecimalField(),
                )
            ),
        )
        .order_by('-total_revenue')[:10]
    )
    for p in top_products:
        p['total_revenue'] = float(p['total_revenue'] or 0)
        p['total_profit'] = float(p['total_profit'] or 0)

    # Top 10 customers by spend
    top_customers = list(
        period_sales.values('customer__name', 'customer__phone')
        .annotate(total_spent=Sum('total_amount'), invoice_count=Count('id'))
        .order_by('-total_spent')[:10]
    )
    for c in top_customers:
        c['total_spent'] = float(c['total_spent'] or 0)

    # Monthly trend (6 months)
    chart_labels, chart_revenue, chart_profit, chart_count = [], [], [], []
    for i in range(5, -1, -1):
        target = today.replace(day=1) - timedelta(days=30 * i)
        month_qs = sales_base.filter(
            date_created__year=target.year, date_created__month=target.month,
        )
        agg = month_qs.aggregate(r=Sum('total_amount'), p=Sum('net_profit'), c=Count('id'))
        chart_labels.append(target.strftime('%B %Y'))
        chart_revenue.append(float(agg['r'] or 0))
        chart_profit.append(float(agg['p'] or 0))
        chart_count.append(agg['c'] or 0)

    # Operating expenses (not linked to invoices)
    try:
        expenses_qs = FinancialTransaction.objects.filter(
            transaction_type='out',
            date__date__gte=from_date, date__date__lte=to_date,
            sale_invoice__isnull=True, purchase_invoice__isnull=True,
        )
        if branch:
            expenses_qs = expenses_qs.filter(treasury__branch=branch)
        total_expenses = float(expenses_qs.aggregate(t=Sum('amount'))['t'] or 0)
    except Exception:
        total_expenses = 0.0

    # Debt aging
    from inventory.services.reporting_service import ReportingService
    try:
        debt_aging = ReportingService.customer_debt_aging(branch=branch)
    except Exception:
        debt_aging = []

    # Slow-moving inventory
    try:
        slow_moving = ReportingService.slow_moving_inventory(days_threshold=60, branch=branch)
    except Exception:
        slow_moving = []

    # Pending B2B listings count
    try:
        pending_b2b = B2BListingRequest.objects.filter(status='pending').count()
    except Exception:
        pending_b2b = 0

    # ── Customer Detail Report (when a customer is selected) ──
    customer_detail = None
    if customer_id:
        try:
            cust = Customer.objects.get(id=int(customer_id))
            cust_invoices = list(
                SaleInvoice.objects.filter(customer=cust, status='posted')
                .order_by('-date_created')[:50]
                .values('id', 'date_created', 'total_amount', 'paid_amount',
                        'is_return', 'invoice_type')
            )
            for inv in cust_invoices:
                inv['total_amount'] = float(inv['total_amount'] or 0)
                inv['paid_amount'] = float(inv['paid_amount'] or 0)
                inv['due'] = inv['total_amount'] - inv['paid_amount']
                inv['date_created'] = inv['date_created'].strftime('%Y-%m-%d') if inv['date_created'] else ''
            cust_payments = list(
                FinancialTransaction.objects.filter(
                    customer=cust, transaction_type='in',
                ).order_by('-date')[:30]
                .values('date', 'amount', 'description', 'treasury__name')
            )
            for pay in cust_payments:
                pay['amount'] = float(pay['amount'] or 0)
                pay['date'] = pay['date'].strftime('%Y-%m-%d') if pay['date'] else ''
            customer_detail = {
                'name': cust.name,
                'phone': cust.phone or '',
                'balance': float(cust.balance),
                'invoices': cust_invoices,
                'payments': cust_payments,
                'total_purchases': float(
                    SaleInvoice.objects.filter(customer=cust, status='posted', is_return=False)
                    .aggregate(t=Sum('total_amount'))['t'] or 0
                ),
                'total_returns': float(
                    SaleInvoice.objects.filter(customer=cust, status='posted', is_return=True)
                    .aggregate(t=Sum('total_amount'))['t'] or 0
                ),
                'invoice_count': SaleInvoice.objects.filter(customer=cust, status='posted').count(),
            }
        except (Customer.DoesNotExist, ValueError, TypeError):
            customer_detail = None

    # ── Expense Breakdown by Category ──
    try:
        expense_breakdown = list(
            FinancialTransaction.objects.filter(
                transaction_type='out',
                date__date__gte=from_date, date__date__lte=to_date,
                sale_invoice__isnull=True, purchase_invoice__isnull=True,
            ).values('category__name')
            .annotate(total=Sum('amount'), count=Count('id'))
            .order_by('-total')
        )
        for e in expense_breakdown:
            e['total'] = float(e['total'] or 0)
            e['category__name'] = e['category__name'] or 'بدون تصنيف'
    except Exception:
        expense_breakdown = []

    # ── Payroll Reports ──
    try:
        from hr.models import PayrollRun, PayrollEntry, Employee
        payroll_runs = list(
            PayrollRun.objects.order_by('-period_year', '-period_month')[:12]
            .values('id', 'period_month', 'period_year', 'status',
                    'total_gross', 'total_deductions', 'total_net', 'total_employees')
        )
        for pr in payroll_runs:
            pr['total_gross'] = float(pr['total_gross'] or 0)
            pr['total_deductions'] = float(pr['total_deductions'] or 0)
            pr['total_net'] = float(pr['total_net'] or 0)
            pr['status_display'] = dict(PayrollRun.STATUS_CHOICES).get(pr['status'], pr['status'])
            pr['period_label'] = f"{pr['period_month']}/{pr['period_year']}"

        # Latest payroll detail
        latest_payroll_entries = []
        if payroll_runs:
            latest_payroll_entries = list(
                PayrollEntry.objects.filter(payroll_run_id=payroll_runs[0]['id'])
                .select_related('employee__user')
                .values(
                    'employee__user__first_name', 'employee__user__last_name',
                    'employee__department', 'base_salary',
                    'late_deduction', 'absence_deduction', 'advance_deduction',
                    'other_deductions', 'bonuses', 'overtime_pay',
                    'total_deductions', 'total_additions', 'net_salary',
                    'days_present', 'days_absent', 'days_late',
                )
            )
            dept_display = dict(Employee.DEPARTMENT_CHOICES)
            for entry in latest_payroll_entries:
                for k in ('base_salary', 'late_deduction', 'absence_deduction',
                           'advance_deduction', 'other_deductions', 'bonuses',
                           'overtime_pay', 'total_deductions', 'total_additions', 'net_salary'):
                    entry[k] = float(entry[k] or 0)
                entry['employee_name'] = f"{entry['employee__user__first_name'] or ''} {entry['employee__user__last_name'] or ''}".strip() or 'موظف'
                entry['dept'] = dept_display.get(entry['employee__department'], entry['employee__department'] or '-')

        # Payroll cost trend (6 months)
        payroll_trend_labels, payroll_trend_data = [], []
        for i in range(5, -1, -1):
            target = today.replace(day=1) - timedelta(days=30 * i)
            pr_agg = PayrollRun.objects.filter(
                period_year=target.year, period_month=target.month,
            ).aggregate(net=Sum('total_net'))
            payroll_trend_labels.append(f"{target.month}/{target.year}")
            payroll_trend_data.append(float(pr_agg['net'] or 0))
    except Exception:
        payroll_runs = []
        latest_payroll_entries = []
        payroll_trend_labels = []
        payroll_trend_data = []

    # ── Employee Reports ──
    try:
        from hr.models import AttendanceRecord, LeaveRequest, Advance
        # Employee attendance summary for current month
        employees_summary = []
        all_employees = Employee.objects.filter(
            user__is_active=True,
        ).select_related('user').order_by('department', 'user__first_name')

        for emp in all_employees:
            att = AttendanceRecord.objects.filter(
                employee=emp, date__gte=first_of_month, date__lte=today,
            )
            att_agg = att.aggregate(
                present=Count('id', filter=models.Q(status__in=('present', 'late'))),
                absent=Count('id', filter=models.Q(status='absent')),
                late=Count('id', filter=models.Q(status='late')),
                late_mins=Sum('late_minutes'),
                hours=Sum('worked_hours'),
            )
            dept_display = dict(Employee.DEPARTMENT_CHOICES)
            employees_summary.append({
                'name': f"{emp.user.first_name or ''} {emp.user.last_name or ''}".strip() or emp.user.username,
                'department': dept_display.get(emp.department, emp.department or '-'),
                'job_title': emp.job_title or '-',
                'present': att_agg['present'] or 0,
                'absent': att_agg['absent'] or 0,
                'late': att_agg['late'] or 0,
                'late_mins': att_agg['late_mins'] or 0,
                'hours': float(att_agg['hours'] or 0),
                'salary': float(emp.base_salary),
            })

        # Leave requests for the period
        leave_requests = list(
            LeaveRequest.objects.filter(
                from_date__lte=to_date, to_date__gte=from_date,
            ).select_related('employee__user')
            .order_by('-created_at')[:50]
            .values(
                'employee__user__first_name', 'employee__user__last_name',
                'leave_type', 'from_date', 'to_date', 'status',
            )
        )
        leave_type_display = dict(LeaveRequest.TYPE_CHOICES)
        leave_status_display = dict(LeaveRequest.STATUS_CHOICES)
        for lr in leave_requests:
            lr['employee_name'] = f"{lr['employee__user__first_name'] or ''} {lr['employee__user__last_name'] or ''}".strip()
            lr['leave_type_display'] = leave_type_display.get(lr['leave_type'], lr['leave_type'])
            lr['status_display'] = leave_status_display.get(lr['status'], lr['status'])
            lr['from_date'] = lr['from_date'].isoformat() if lr['from_date'] else ''
            lr['to_date'] = lr['to_date'].isoformat() if lr['to_date'] else ''
            lr['days'] = (
                (datetime.datetime.strptime(lr['to_date'], '%Y-%m-%d').date() -
                 datetime.datetime.strptime(lr['from_date'], '%Y-%m-%d').date()).days + 1
            ) if lr['from_date'] and lr['to_date'] else 0

        # Active advances
        active_advances = list(
            Advance.objects.filter(status__in=('approved', 'active'))
            .select_related('employee__user')
            .values(
                'employee__user__first_name', 'employee__user__last_name',
                'amount', 'remaining_amount', 'installments_count', 'status',
            )
        )
        adv_status_display = dict(Advance.STATUS_CHOICES)
        for adv in active_advances:
            adv['employee_name'] = f"{adv['employee__user__first_name'] or ''} {adv['employee__user__last_name'] or ''}".strip()
            adv['amount'] = float(adv['amount'] or 0)
            adv['remaining_amount'] = float(adv['remaining_amount'] or 0)
            adv['paid'] = adv['amount'] - adv['remaining_amount']
            adv['status_display'] = adv_status_display.get(adv['status'], adv['status'])
    except Exception:
        employees_summary = []
        leave_requests = []
        active_advances = []

    context = {
        **admin.site.each_context(request),
        'title': 'التقارير والتحليلات',
        'today_summary': json.dumps(today_summary),
        'month_summary': json.dumps(month_summary),
        'year_summary': json.dumps(year_summary),
        'period_summary': json.dumps(period_summary),
        'top_products': json.dumps(top_products, ensure_ascii=False),
        'top_customers': json.dumps(top_customers, ensure_ascii=False),
        'chart_labels': json.dumps(chart_labels),
        'chart_revenue': json.dumps(chart_revenue),
        'chart_profit': json.dumps(chart_profit),
        'chart_count': json.dumps(chart_count),
        'total_expenses': total_expenses,
        'net_pl': period_summary['profit'] - total_expenses,
        'from_date': from_date.isoformat(),
        'to_date': to_date.isoformat(),
        'selected_customer': customer_id,
        'customers_list': json.dumps(
            list(Customer.objects.values('id', 'name').order_by('name')[:200]),
            ensure_ascii=False,
        ),
        'debt_aging': json.dumps(debt_aging, ensure_ascii=False),
        'slow_moving': json.dumps(slow_moving[:20], ensure_ascii=False),
        'pending_b2b': pending_b2b,
        # New report data
        'customer_detail': json.dumps(customer_detail, ensure_ascii=False) if customer_detail else 'null',
        'expense_breakdown': json.dumps(expense_breakdown, ensure_ascii=False),
        'payroll_runs': json.dumps(payroll_runs, ensure_ascii=False),
        'latest_payroll_entries': json.dumps(latest_payroll_entries, ensure_ascii=False),
        'payroll_trend_labels': json.dumps(payroll_trend_labels),
        'payroll_trend_data': json.dumps(payroll_trend_data),
        'employees_summary': json.dumps(employees_summary, ensure_ascii=False),
        'leave_requests': json.dumps(leave_requests, ensure_ascii=False),
        'active_advances': json.dumps(active_advances, ensure_ascii=False),
    }

    # Force-render to catch template errors inside try/except
    response = TemplateResponse(request, 'admin/reports.html', context)
    response.render()
    return response


def _build_reports_response_printing(request):
    """Internal: builds the reports TemplateResponse for PRINTING tenants."""
    from printing.models import (
        PrintOrder, PrintJob, PrintTransaction, PrintCustomer,
        PrintTreasury, PrintMaterial, Designer, DesignerWorkLog,
        MachineProfile, PrintBranch, ProductType,
    )

    from_date_str = request.GET.get('from', '')
    to_date_str = request.GET.get('to', '')
    customer_id = request.GET.get('customer', '')

    today = timezone.now().date()
    first_of_month = today.replace(day=1)
    first_of_year = today.replace(month=1, day=1)

    try:
        from_date = timezone.datetime.strptime(from_date_str, '%Y-%m-%d').date() if from_date_str else first_of_month
        to_date = timezone.datetime.strptime(to_date_str, '%Y-%m-%d').date() if to_date_str else today
    except ValueError:
        from_date, to_date = first_of_month, today

    # Base queryset — orders that are not cancelled
    orders_base = PrintOrder.objects.exclude(status='cancelled')
    branch = None
    if not request.user.is_superuser:
        try:
            branch = request.user.employee_profile.branch
            # Map employee branch to PrintBranch by name
            if branch:
                pb = PrintBranch.objects.filter(name=branch.name).first()
                if pb:
                    orders_base = orders_base.filter(branch=pb)
        except Exception:
            pass

    # Period filter
    period_orders = orders_base.filter(
        date_created__date__gte=from_date, date_created__date__lte=to_date,
    )
    if customer_id:
        try:
            period_orders = period_orders.filter(customer_id=int(customer_id))
        except (ValueError, TypeError):
            pass

    def summarize_orders(qs):
        """Summarize printing orders: revenue = net_total, cost from jobs, profit = revenue - cost."""
        agg = qs.aggregate(
            revenue=Sum(F('total_amount') - F('discount')),
            paid=Sum('paid_amount'),
            count=Count('id'),
        )
        revenue = float(agg['revenue'] or 0)
        # Calculate cost from completed jobs
        job_cost = float(
            PrintJob.objects.filter(order__in=qs, is_complete=True)
            .aggregate(c=Sum('actual_cost'))['c'] or 0
        )
        return {
            'revenue': revenue,
            'cost': job_cost,
            'profit': revenue - job_cost,
            'count': agg['count'] or 0,
        }

    today_summary = summarize_orders(orders_base.filter(date_created__date=today))
    month_summary = summarize_orders(orders_base.filter(date_created__date__gte=first_of_month))
    year_summary = summarize_orders(orders_base.filter(date_created__date__gte=first_of_year))
    period_summary = summarize_orders(period_orders)

    # Top 10 products (by ProductType)
    top_products = list(
        PrintJob.objects.filter(order__in=period_orders)
        .exclude(product_type__isnull=True)
        .values('product_type__name')
        .annotate(
            total_revenue=Sum('total_price'),
            total_qty=Sum('quantity'),
            total_profit=Sum(F('total_price') - F('actual_cost')),
        )
        .order_by('-total_revenue')[:10]
    )
    for p in top_products:
        p['product__name'] = p.pop('product_type__name', '')
        p['product__part_number'] = ''
        p['total_revenue'] = float(p['total_revenue'] or 0)
        p['total_profit'] = float(p['total_profit'] or 0)

    # Top 10 customers by spend
    top_customers = list(
        period_orders.values('customer__name', 'customer__phone')
        .annotate(
            total_spent=Sum(F('total_amount') - F('discount')),
            invoice_count=Count('id'),
        )
        .order_by('-total_spent')[:10]
    )
    for c in top_customers:
        c['total_spent'] = float(c['total_spent'] or 0)

    # Monthly trend (6 months)
    chart_labels, chart_revenue, chart_profit, chart_count = [], [], [], []
    for i in range(5, -1, -1):
        target = today.replace(day=1) - timedelta(days=30 * i)
        month_qs = orders_base.filter(
            date_created__year=target.year, date_created__month=target.month,
        )
        agg = month_qs.aggregate(
            r=Sum(F('total_amount') - F('discount')),
            c=Count('id'),
        )
        job_cost = float(
            PrintJob.objects.filter(
                order__in=month_qs, is_complete=True
            ).aggregate(cost=Sum('actual_cost'))['cost'] or 0
        )
        rev = float(agg['r'] or 0)
        chart_labels.append(target.strftime('%B %Y'))
        chart_revenue.append(rev)
        chart_profit.append(rev - job_cost)
        chart_count.append(agg['c'] or 0)

    # Operating expenses (transactions out, not linked to orders)
    try:
        expenses_qs = PrintTransaction.objects.filter(
            transaction_type='out',
            date__date__gte=from_date, date__date__lte=to_date,
            order__isnull=True,
        )
        total_expenses = float(expenses_qs.aggregate(t=Sum('amount'))['t'] or 0)
    except Exception:
        total_expenses = 0.0

    # Debt aging — customers with unpaid orders
    try:
        debt_aging = []
        customers_with_debt = (
            PrintOrder.objects.exclude(status='cancelled')
            .annotate(remaining=F('total_amount') - F('discount') - F('paid_amount'))
            .filter(remaining__gt=0)
            .values('customer__id', 'customer__name', 'customer__phone')
            .annotate(
                total_debt=Sum(F('total_amount') - F('discount') - F('paid_amount')),
                invoice_count=Count('id'),
                oldest_date=models.Min('date_created'),
            )
            .order_by('-total_debt')[:30]
        )
        for row in customers_with_debt:
            oldest = row['oldest_date']
            if oldest:
                days = (timezone.now() - oldest).days
            else:
                days = 0
            if days <= 30:
                bracket = '0-30 يوم'
            elif days <= 60:
                bracket = '31-60 يوم'
            elif days <= 90:
                bracket = '61-90 يوم'
            else:
                bracket = '+90 يوم'
            debt_aging.append({
                'customer_name': row['customer__name'] or '',
                'customer_phone': row['customer__phone'] or '',
                'total_debt': float(row['total_debt'] or 0),
                'invoice_count': row['invoice_count'] or 0,
                'oldest_days': days,
                'bracket': bracket,
            })
    except Exception:
        debt_aging = []

    # Slow-moving materials (low stock alerts)
    try:
        slow_moving = list(
            PrintMaterial.objects.filter(quantity__lte=F('min_stock'))
            .values('name', 'category', 'quantity', 'min_stock', 'cost_per_unit')
            .order_by('quantity')[:20]
        )
        cat_display = dict(PrintMaterial.CATEGORY_CHOICES)
        for m in slow_moving:
            m['product_name'] = m.pop('name', '')
            m['part_number'] = cat_display.get(m.pop('category', ''), '')
            m['current_stock'] = float(m.pop('quantity', 0))
            m['last_sale_date'] = ''
            m['days_since_last_sale'] = 0
            m['stock_value'] = float(m['current_stock'] * float(m.pop('cost_per_unit', 0)))
            m.pop('min_stock', None)
    except Exception:
        slow_moving = []

    # ── Customer Detail Report (when a customer is selected) ──
    customer_detail = None
    if customer_id:
        try:
            cust = PrintCustomer.objects.get(id=int(customer_id))
            cust_orders = list(
                PrintOrder.objects.filter(customer=cust)
                .exclude(status='cancelled')
                .order_by('-date_created')[:50]
                .values('id', 'order_number', 'date_created', 'total_amount',
                        'discount', 'paid_amount', 'status')
            )
            status_display = dict(PrintOrder.STATUS_CHOICES)
            for inv in cust_orders:
                net = float(inv['total_amount'] or 0) - float(inv['discount'] or 0)
                inv['total_amount'] = net
                inv['paid_amount'] = float(inv['paid_amount'] or 0)
                inv['due'] = net - inv['paid_amount']
                inv['date_created'] = inv['date_created'].strftime('%Y-%m-%d') if inv['date_created'] else ''
                inv['is_return'] = False
                inv['invoice_type'] = status_display.get(inv['status'], inv['status'])

            cust_payments = list(
                PrintTransaction.objects.filter(
                    order__customer=cust, transaction_type='in',
                ).order_by('-date')[:30]
                .values('date', 'amount', 'description', 'treasury__name')
            )
            for pay in cust_payments:
                pay['amount'] = float(pay['amount'] or 0)
                pay['date'] = pay['date'].strftime('%Y-%m-%d') if pay['date'] else ''

            total_revenue = float(
                PrintOrder.objects.filter(customer=cust)
                .exclude(status='cancelled')
                .aggregate(t=Sum(F('total_amount') - F('discount')))['t'] or 0
            )
            total_paid = float(
                PrintOrder.objects.filter(customer=cust)
                .exclude(status='cancelled')
                .aggregate(t=Sum('paid_amount'))['t'] or 0
            )
            customer_detail = {
                'name': cust.name,
                'phone': cust.phone or '',
                'balance': total_revenue - total_paid,
                'invoices': cust_orders,
                'payments': cust_payments,
                'total_purchases': total_revenue,
                'total_returns': 0,
                'invoice_count': PrintOrder.objects.filter(customer=cust).exclude(status='cancelled').count(),
            }
        except (PrintCustomer.DoesNotExist, ValueError, TypeError):
            customer_detail = None

    # ── Expense Breakdown (by description keywords since no category model) ──
    try:
        expense_txns = PrintTransaction.objects.filter(
            transaction_type='out',
            date__date__gte=from_date, date__date__lte=to_date,
            order__isnull=True,
        )
        # Group by first word of description as pseudo-category
        expense_breakdown = []
        expense_map = {}
        for txn in expense_txns.values('description', 'amount'):
            cat = (txn['description'] or 'أخرى').split()[0] if txn['description'] else 'أخرى'
            if cat not in expense_map:
                expense_map[cat] = {'category__name': cat, 'total': 0.0, 'count': 0}
            expense_map[cat]['total'] += float(txn['amount'] or 0)
            expense_map[cat]['count'] += 1
        expense_breakdown = sorted(expense_map.values(), key=lambda x: -x['total'])
    except Exception:
        expense_breakdown = []

    # ── Payroll Reports (shared HR module) ──
    try:
        from hr.models import PayrollRun, PayrollEntry, Employee
        payroll_runs = list(
            PayrollRun.objects.order_by('-period_year', '-period_month')[:12]
            .values('id', 'period_month', 'period_year', 'status',
                    'total_gross', 'total_deductions', 'total_net', 'total_employees')
        )
        for pr in payroll_runs:
            pr['total_gross'] = float(pr['total_gross'] or 0)
            pr['total_deductions'] = float(pr['total_deductions'] or 0)
            pr['total_net'] = float(pr['total_net'] or 0)
            pr['status_display'] = dict(PayrollRun.STATUS_CHOICES).get(pr['status'], pr['status'])
            pr['period_label'] = f"{pr['period_month']}/{pr['period_year']}"

        latest_payroll_entries = []
        if payroll_runs:
            latest_payroll_entries = list(
                PayrollEntry.objects.filter(payroll_run_id=payroll_runs[0]['id'])
                .select_related('employee__user')
                .values(
                    'employee__user__first_name', 'employee__user__last_name',
                    'employee__department', 'base_salary',
                    'late_deduction', 'absence_deduction', 'advance_deduction',
                    'other_deductions', 'bonuses', 'overtime_pay',
                    'total_deductions', 'total_additions', 'net_salary',
                    'days_present', 'days_absent', 'days_late',
                )
            )
            dept_display = dict(Employee.DEPARTMENT_CHOICES)
            for entry in latest_payroll_entries:
                for k in ('base_salary', 'late_deduction', 'absence_deduction',
                           'advance_deduction', 'other_deductions', 'bonuses',
                           'overtime_pay', 'total_deductions', 'total_additions', 'net_salary'):
                    entry[k] = float(entry[k] or 0)
                entry['employee_name'] = f"{entry['employee__user__first_name'] or ''} {entry['employee__user__last_name'] or ''}".strip() or 'موظف'
                entry['dept'] = dept_display.get(entry['employee__department'], entry['employee__department'] or '-')

        payroll_trend_labels, payroll_trend_data = [], []
        for i in range(5, -1, -1):
            target = today.replace(day=1) - timedelta(days=30 * i)
            pr_agg = PayrollRun.objects.filter(
                period_year=target.year, period_month=target.month,
            ).aggregate(net=Sum('total_net'))
            payroll_trend_labels.append(f"{target.month}/{target.year}")
            payroll_trend_data.append(float(pr_agg['net'] or 0))
    except Exception:
        payroll_runs = []
        latest_payroll_entries = []
        payroll_trend_labels = []
        payroll_trend_data = []

    # ── Employee Reports (shared HR module) ──
    try:
        from hr.models import AttendanceRecord, LeaveRequest, Advance, Employee
        employees_summary = []
        all_employees = Employee.objects.filter(
            user__is_active=True,
        ).select_related('user').order_by('department', 'user__first_name')

        for emp in all_employees:
            att = AttendanceRecord.objects.filter(
                employee=emp, date__gte=first_of_month, date__lte=today,
            )
            att_agg = att.aggregate(
                present=Count('id', filter=models.Q(status__in=('present', 'late'))),
                absent=Count('id', filter=models.Q(status='absent')),
                late=Count('id', filter=models.Q(status='late')),
                late_mins=Sum('late_minutes'),
                hours=Sum('worked_hours'),
            )
            dept_display = dict(Employee.DEPARTMENT_CHOICES)
            employees_summary.append({
                'name': f"{emp.user.first_name or ''} {emp.user.last_name or ''}".strip() or emp.user.username,
                'department': dept_display.get(emp.department, emp.department or '-'),
                'job_title': emp.job_title or '-',
                'present': att_agg['present'] or 0,
                'absent': att_agg['absent'] or 0,
                'late': att_agg['late'] or 0,
                'late_mins': att_agg['late_mins'] or 0,
                'hours': float(att_agg['hours'] or 0),
                'salary': float(emp.base_salary),
            })

        leave_requests = list(
            LeaveRequest.objects.filter(
                from_date__lte=to_date, to_date__gte=from_date,
            ).select_related('employee__user')
            .order_by('-created_at')[:50]
            .values(
                'employee__user__first_name', 'employee__user__last_name',
                'leave_type', 'from_date', 'to_date', 'status',
            )
        )
        leave_type_display = dict(LeaveRequest.TYPE_CHOICES)
        leave_status_display = dict(LeaveRequest.STATUS_CHOICES)
        for lr in leave_requests:
            lr['employee_name'] = f"{lr['employee__user__first_name'] or ''} {lr['employee__user__last_name'] or ''}".strip()
            lr['leave_type_display'] = leave_type_display.get(lr['leave_type'], lr['leave_type'])
            lr['status_display'] = leave_status_display.get(lr['status'], lr['status'])
            lr['from_date'] = lr['from_date'].isoformat() if lr['from_date'] else ''
            lr['to_date'] = lr['to_date'].isoformat() if lr['to_date'] else ''
            lr['days'] = (
                (datetime.datetime.strptime(lr['to_date'], '%Y-%m-%d').date() -
                 datetime.datetime.strptime(lr['from_date'], '%Y-%m-%d').date()).days + 1
            ) if lr['from_date'] and lr['to_date'] else 0

        active_advances = list(
            Advance.objects.filter(status__in=('approved', 'active'))
            .select_related('employee__user')
            .values(
                'employee__user__first_name', 'employee__user__last_name',
                'amount', 'remaining_amount', 'installments_count', 'status',
            )
        )
        adv_status_display = dict(Advance.STATUS_CHOICES)
        for adv in active_advances:
            adv['employee_name'] = f"{adv['employee__user__first_name'] or ''} {adv['employee__user__last_name'] or ''}".strip()
            adv['amount'] = float(adv['amount'] or 0)
            adv['remaining_amount'] = float(adv['remaining_amount'] or 0)
            adv['paid'] = adv['amount'] - adv['remaining_amount']
            adv['status_display'] = adv_status_display.get(adv['status'], adv['status'])
    except Exception:
        employees_summary = []
        leave_requests = []
        active_advances = []

    context = {
        **admin.site.each_context(request),
        'title': 'التقارير والتحليلات',
        'today_summary': json.dumps(today_summary),
        'month_summary': json.dumps(month_summary),
        'year_summary': json.dumps(year_summary),
        'period_summary': json.dumps(period_summary),
        'top_products': json.dumps(top_products, ensure_ascii=False),
        'top_customers': json.dumps(top_customers, ensure_ascii=False),
        'chart_labels': json.dumps(chart_labels),
        'chart_revenue': json.dumps(chart_revenue),
        'chart_profit': json.dumps(chart_profit),
        'chart_count': json.dumps(chart_count),
        'total_expenses': total_expenses,
        'net_pl': period_summary['profit'] - total_expenses,
        'from_date': from_date.isoformat(),
        'to_date': to_date.isoformat(),
        'selected_customer': customer_id,
        'customers_list': json.dumps(
            list(PrintCustomer.objects.values('id', 'name').order_by('name')[:200]),
            ensure_ascii=False,
        ),
        'debt_aging': json.dumps(debt_aging, ensure_ascii=False),
        'slow_moving': json.dumps(slow_moving, ensure_ascii=False),
        'pending_b2b': 0,
        # New report data
        'customer_detail': json.dumps(customer_detail, ensure_ascii=False) if customer_detail else 'null',
        'expense_breakdown': json.dumps(expense_breakdown, ensure_ascii=False),
        'payroll_runs': json.dumps(payroll_runs, ensure_ascii=False),
        'latest_payroll_entries': json.dumps(latest_payroll_entries, ensure_ascii=False),
        'payroll_trend_labels': json.dumps(payroll_trend_labels),
        'payroll_trend_data': json.dumps(payroll_trend_data),
        'employees_summary': json.dumps(employees_summary, ensure_ascii=False),
        'leave_requests': json.dumps(leave_requests, ensure_ascii=False),
        'active_advances': json.dumps(active_advances, ensure_ascii=False),
    }

    response = TemplateResponse(request, 'admin/reports.html', context)
    response.render()
    return response


# Inject reports URL into admin
from django.urls import path as _admin_path
_original_get_urls = admin.AdminSite.get_urls

def _custom_get_urls(self):
    custom = [
        _admin_path('reports/', self.admin_view(admin_reports_view), name='admin_reports'),
    ]
    return custom + _original_get_urls(self)

admin.AdminSite.get_urls = _custom_get_urls


