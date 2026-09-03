"""
📱 Mobile API — Views

كل الـ endpoints مُجمّعة تحت /api/mobile/v1/. تعمل داخل سياق المستأجر الحالي
(django-tenants) لذا البيانات مُفلترة تلقائياً حسب ورشة المستخدم.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum, Count, Q, F
from django.utils import timezone
from rest_framework import viewsets, mixins, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView

from inventory.models import Product, StockAlert, Customer
from inventory.models.invoices import SaleInvoice

from .serializers import (
    UserSerializer,
    ProductListSerializer,
    ProductDetailSerializer,
    StockAlertSerializer,
    CustomerListSerializer,
    CustomerDetailSerializer,
    WorkOrderListSerializer,
    WorkOrderDetailSerializer,
    WorkOrderStatusUpdateSerializer,
)


# ---------------------------------------------------------------------------
# 🔐 المصادقة (JWT)
# ---------------------------------------------------------------------------
class MobileTokenObtainPairSerializer(TokenObtainPairSerializer):
    """يُرفق بيانات المستخدم مع التوكن حتى لا يحتاج التطبيق لطلب إضافي."""

    def validate(self, attrs):
        data = super().validate(attrs)
        data['user'] = UserSerializer(self.user).data
        return data


class MobileTokenObtainPairView(TokenObtainPairView):
    serializer_class = MobileTokenObtainPairSerializer
    # نسمح للطلب بمعدّل مناسب لتسجيل الدخول عبر throttle الافتراضي (anon).


class MeView(APIView):
    """بيانات المستخدم الحالي — تُستخدم لاستعادة الجلسة عند فتح التطبيق."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


# ---------------------------------------------------------------------------
# 📊 لوحة المعلومات
# ---------------------------------------------------------------------------
class DashboardView(APIView):
    """مؤشرات مُلخّصة للشاشة الرئيسية في التطبيق."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = timezone.localdate()

        # أوامر الشغل المفتوحة (كل ما هو ليس مُسلّماً بعد).
        open_statuses = ['quotation', 'in_progress', 'quality_check', 'ready']
        work_orders = SaleInvoice.objects.filter(invoice_type='maintenance')
        open_orders = work_orders.filter(status__in=open_statuses)

        # توزيع أوامر الشغل حسب الحالة.
        status_breakdown = {
            row['status']: row['c']
            for row in open_orders.values('status').annotate(c=Count('id'))
        }

        # إيراد اليوم (الفواتير المُسلّمة/المعتمدة اليوم).
        revenue_today = (
            SaleInvoice.objects.filter(status='posted', date_created__date=today)
            .aggregate(total=Sum('total_amount'))['total']
            or Decimal('0.00')
        )

        # تنبيهات نقص المخزون غير المُعالَجة.
        open_alerts = StockAlert.objects.filter(is_resolved=False).count()

        return Response({
            'open_work_orders': open_orders.count(),
            'ready_for_delivery': status_breakdown.get('ready', 0),
            'in_progress': status_breakdown.get('in_progress', 0),
            'quality_check': status_breakdown.get('quality_check', 0),
            'quotations': status_breakdown.get('quotation', 0),
            'revenue_today': revenue_today,
            'low_stock_alerts': open_alerts,
            'total_customers': Customer.objects.count(),
            'total_products': Product.objects.filter(is_active=True).count(),
            'status_breakdown': status_breakdown,
        })


# ---------------------------------------------------------------------------
# 🔧 أوامر شغل الورشة / الصيانة
# ---------------------------------------------------------------------------
class WorkOrderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """قائمة وتفاصيل أوامر شغل الصيانة + تحديث الحالة."""

    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = (
            SaleInvoice.objects.filter(invoice_type='maintenance')
            .select_related('customer', 'vehicle', 'branch')
            .order_by('-date_created')
        )
        params = self.request.query_params
        status_param = params.get('status')
        if status_param == 'open':
            qs = qs.exclude(status='posted')
        elif status_param:
            qs = qs.filter(status=status_param)

        branch = params.get('branch')
        if branch:
            qs = qs.filter(branch_id=branch)

        search = params.get('search')
        if search:
            qs = qs.filter(
                Q(customer__name__icontains=search)
                | Q(customer__phone__icontains=search)
                | Q(vehicle__car_plate__icontains=search)
                | Q(id__icontains=search)
            )
        return qs

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return WorkOrderDetailSerializer
        return WorkOrderListSerializer

    @action(detail=True, methods=['post'], url_path='status')
    def update_status(self, request, pk=None):
        order = self.get_object()
        serializer = WorkOrderStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_status = serializer.validated_data['status']
        order.status = new_status
        order.save(update_fields=['status'])
        return Response(WorkOrderDetailSerializer(order).data, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# 📦 المخزون / قطع الغيار
# ---------------------------------------------------------------------------
class ProductViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """قائمة وبحث وتفاصيل قطع الغيار."""

    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Product.objects.annotate(
            annotated_qty=Sum('inventory__quantity')
        ).order_by('name')

        params = self.request.query_params
        if params.get('active_only', 'true').lower() != 'false':
            qs = qs.filter(is_active=True)

        search = params.get('search')
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(part_number__icontains=search)
                | Q(barcode__icontains=search)
                | Q(car_model__icontains=search)
                | Q(brand__icontains=search)
            )
        return qs

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ProductDetailSerializer
        return ProductListSerializer

    @action(detail=False, methods=['get'], url_path='low-stock')
    def low_stock(self, request):
        """القطع التي وصلت أو انخفضت عن حد الأمان."""
        qs = (
            Product.objects.filter(is_active=True)
            .annotate(annotated_qty=Sum('inventory__quantity'))
            .filter(Q(annotated_qty__lte=F('min_stock_level')) | Q(annotated_qty__isnull=True))
            .order_by('name')
        )
        page = self.paginate_queryset(qs)
        serializer = ProductListSerializer(page if page is not None else qs, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


class StockAlertViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = StockAlertSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = StockAlert.objects.select_related('product', 'branch').order_by('-created_at')
        if self.request.query_params.get('resolved', 'false').lower() != 'true':
            qs = qs.filter(is_resolved=False)
        return qs


# ---------------------------------------------------------------------------
# 👥 العملاء
# ---------------------------------------------------------------------------
class CustomerViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Customer.objects.all().order_by('name')
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(phone__icontains=search))
        return qs

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return CustomerDetailSerializer
        return CustomerListSerializer
