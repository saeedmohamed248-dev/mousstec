"""Core views — المصادقة، المستخدم، لوحة المعلومات، التحليلات، الفروع."""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum, Count
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from erp_core.jwt_tenant import TenantTokenObtainPairSerializer
from inventory.models import Product, StockAlert, Customer, Vehicle
from inventory.models.organization import Branch
from inventory.models.invoices import SaleInvoice, PurchaseInvoice

from ..permissions import ADMIN, AUTOMOTIVE, FINANCE, IndustryGate, RoleScoped
from ..serializers import UserSerializer, BranchSerializer
from .base import FullCrudViewSet


def _net_sales(qs):
    """صافي المبيعات (شامل الضريبة) بعد طرح المرتجعات — نفس لوحة الويب."""
    from inventory.services.reporting_service import ReportingService
    return ReportingService.net_sales_profit(qs)[0].quantize(Decimal('0.01'))


def tenant_info(request, user=None):
    """بيانات الشركة اللي التطبيق بيبني عليها نفسه: القطاع (سيارات/طباعة)، العملة، اللوجو.

    🐛 [FIX]: التطبيق كان بيفتح «الورشة» لأي شركة — مكانش يعرف نشاطها أصلاً."""
    from django.db import connection
    from ..permissions import tenant_industry
    tenant = getattr(request, 'tenant', None) or getattr(connection, 'tenant', None)
    if tenant is None or getattr(tenant, 'schema_name', 'public') == 'public':
        return None
    try:
        loc = tenant.localization
    except Exception:
        loc = {}
    logo = None
    if getattr(tenant, 'logo', None):
        try:
            logo = request.build_absolute_uri(tenant.logo.url)
        except Exception:
            logo = None
    data = {
        'name': tenant.name,
        'industry': tenant_industry(request),
        'business_type': getattr(tenant, 'business_type', ''),
        'plan': getattr(tenant, 'plan', ''),
        'status': getattr(tenant, 'status', ''),
        'currency': loc.get('currency') or 'EGP',
        'language': loc.get('language') or 'ar',
        'theme_color': getattr(tenant, 'theme_color', '') or '',
        'logo': logo,
    }
    if user is not None and data['industry'] == 'printing':
        # 🎨 صلاحيات المطابع (جروبات «فريق العمل») — التطبيق بيخفي اللي الموظف مايقدرش يفتحه
        data['permissions'] = sorted(
            p.split('.', 1)[1] for p in user.get_all_permissions() if p.startswith('printing.'))
    return data


class MobileTokenObtainPairSerializer(TenantTokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        data['user'] = UserSerializer(self.user).data
        request = self.context.get('request')
        data['tenant'] = tenant_info(request, self.user) if request is not None else None
        return data


class MobileTokenObtainPairView(TokenObtainPairView):
    serializer_class = MobileTokenObtainPairSerializer


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        data = dict(UserSerializer(request.user).data)
        data['tenant'] = tenant_info(request, request.user)
        return Response(data)


class DashboardView(APIView):
    """مؤشرات شاملة للشاشة الرئيسية عبر كل الموديولات."""

    permission_classes = [IsAuthenticated, IndustryGate]
    industry = AUTOMOTIVE

    def get(self, request):
        from ..permissions import branch_ids, has_role
        today = timezone.localdate()
        open_statuses = ['quotation', 'in_progress', 'quality_check', 'ready']
        ids = branch_ids(request.user)   # None = كل الفروع (الأدمن)
        invoices = SaleInvoice.objects.all() if ids is None else SaleInvoice.objects.filter(branch_id__in=ids)
        alerts = StockAlert.objects.all() if ids is None else StockAlert.objects.filter(branch_id__in=ids)
        work_orders = invoices.filter(invoice_type='maintenance')
        open_orders = work_orders.filter(status__in=open_statuses)

        status_breakdown = {
            row['status']: row['c']
            for row in open_orders.values('status').annotate(c=Count('id'))
        }

        # 💰 إيراد اليوم للإدارة والمحاسبة بس (الفني/الكاشير بيشوفوا الشغل مش الفلوس)
        revenue_today = None
        if has_role(request.user, FINANCE):
            # 🐛 [FIX]: كان بيجمع المرتجعات «إيراد» (مرتجع 1000 كان بيزوّد الإيراد
            #    بدل ما ينقّصه) — صافي بعد المرتجعات زي لوحة الويب.
            revenue_today = _net_sales(invoices.filter(status='posted', date_created__date=today))

        data = {
            'open_work_orders': open_orders.count(),
            'ready_for_delivery': status_breakdown.get('ready', 0),
            'in_progress': status_breakdown.get('in_progress', 0),
            'quality_check': status_breakdown.get('quality_check', 0),
            'quotations': status_breakdown.get('quotation', 0),
            'revenue_today': revenue_today,
            'low_stock_alerts': alerts.filter(is_resolved=False).count(),
            'total_customers': Customer.objects.count(),
            'total_vehicles': Vehicle.objects.count(),
            'total_products': Product.objects.filter(is_active=True).count(),
            'pending_purchases': PurchaseInvoice.objects.exclude(status='posted').count(),
            'status_breakdown': status_breakdown,
        }
        # مؤشرات اختيارية من موديولات قد لا تكون مهيّأة — نتجاهل أي خطأ بهدوء.
        try:
            from smart_diagnostics.models import FaultLog
            data['unresolved_faults'] = FaultLog.objects.filter(resolved_at__isnull=True).count()
        except Exception:
            data['unresolved_faults'] = 0
        try:
            from hr.models import Employee, LeaveRequest
            data['active_employees'] = Employee.objects.filter(is_active=True).count()
            data['pending_leaves'] = LeaveRequest.objects.filter(status='pending').count()
        except Exception:
            data['active_employees'] = 0
            data['pending_leaves'] = 0

        return Response(data)


class AnalyticsView(APIView):
    """بيانات الرسوم البيانية للتطبيق: إيراد الأسبوع، توزيع الحالات، الأكثر مبيعاً."""

    # إيرادات الأيام = أرقام مالية — للإدارة والمحاسبة بس
    permission_classes = [IsAuthenticated, IndustryGate, RoleScoped]
    industry = AUTOMOTIVE
    read_roles = FINANCE

    def get(self, request):
        from ..permissions import branch_ids
        today = timezone.localdate()
        # 🐛 [FIX]: الرسوم كانت على كل الفروع حتى لمحاسب فرع واحد (لوحة التطبيق
        #    نفسها محصورة بفروعه) — نفس نطاق الفروع.
        ids = branch_ids(request.user)   # None = كل الفروع (الأدمن)
        invoices = SaleInvoice.objects.all() if ids is None else SaleInvoice.objects.filter(branch_id__in=ids)

        # إيراد آخر 7 أيام (الفواتير المُسلّمة/المعتمدة) — صافي بعد المرتجعات.
        revenue_series = []
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            total = _net_sales(invoices.filter(status='posted', date_created__date=day))
            revenue_series.append({'date': day.isoformat(), 'total': total})

        # توزيع حالات أوامر الشغل (صيانة).
        status_dist = {
            row['status']: row['c']
            for row in invoices.filter(invoice_type='maintenance')
            .values('status').annotate(c=Count('id'))
        }

        # الأكثر مبيعاً خلال 30 يوماً (بالكمية).
        top_products = []
        try:
            from inventory.models.invoices import SaleInvoiceItem
            since = today - timedelta(days=30)
            items = (SaleInvoiceItem.objects
                     .filter(invoice__date_created__date__gte=since, invoice__is_return=False)
                     .exclude(invoice__status='quotation'))
            if ids is not None:
                items = items.filter(invoice__branch_id__in=ids)
            rows = (
                items
                .values('product__name')
                .annotate(qty=Sum('quantity'))
                .order_by('-qty')[:5]
            )
            top_products = [
                {'name': r['product__name'], 'quantity': r['qty'] or 0} for r in rows
            ]
        except Exception:
            top_products = []

        return Response({
            'revenue_last_7_days': revenue_series,
            'work_order_status': status_dist,
            'top_products': top_products,
        })


class BranchViewSet(FullCrudViewSet):
    queryset = Branch.objects.all().order_by('name')
    serializer_class = BranchSerializer
    write_roles = ADMIN   # إنشاء/حذف الفروع للأدمن بس (حدود الباقة كمان)
