"""Core views — المصادقة، المستخدم، لوحة المعلومات، الفروع."""
from decimal import Decimal

from django.db.models import Sum, Count
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView

from inventory.models import Product, StockAlert, Customer, Vehicle
from inventory.models.organization import Branch
from inventory.models.invoices import SaleInvoice, PurchaseInvoice

from ..serializers import UserSerializer, BranchSerializer
from .base import FullCrudViewSet


class MobileTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        data['user'] = UserSerializer(self.user).data
        return data


class MobileTokenObtainPairView(TokenObtainPairView):
    serializer_class = MobileTokenObtainPairSerializer


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class DashboardView(APIView):
    """مؤشرات شاملة للشاشة الرئيسية عبر كل الموديولات."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = timezone.localdate()
        open_statuses = ['quotation', 'in_progress', 'quality_check', 'ready']
        work_orders = SaleInvoice.objects.filter(invoice_type='maintenance')
        open_orders = work_orders.filter(status__in=open_statuses)

        status_breakdown = {
            row['status']: row['c']
            for row in open_orders.values('status').annotate(c=Count('id'))
        }

        revenue_today = (
            SaleInvoice.objects.filter(status='posted', date_created__date=today)
            .aggregate(total=Sum('total_amount'))['total'] or Decimal('0.00')
        )

        data = {
            'open_work_orders': open_orders.count(),
            'ready_for_delivery': status_breakdown.get('ready', 0),
            'in_progress': status_breakdown.get('in_progress', 0),
            'quality_check': status_breakdown.get('quality_check', 0),
            'quotations': status_breakdown.get('quotation', 0),
            'revenue_today': revenue_today,
            'low_stock_alerts': StockAlert.objects.filter(is_resolved=False).count(),
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


class BranchViewSet(FullCrudViewSet):
    queryset = Branch.objects.all().order_by('name')
    serializer_class = BranchSerializer
