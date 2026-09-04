"""
📱 Mobile API — URL routing (شامل لكل الموديولات)

كل المسارات تحت البادئة /api/mobile/v1/.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

app_name = 'mobile_api'

router = DefaultRouter()

# 🔧 الورشة
router.register(r'work-orders', views.WorkOrderViewSet, basename='work-order')
router.register(r'repair-logs', views.RepairLogViewSet, basename='repair-log')
router.register(r'diagnostic-reports', views.DiagnosticReportViewSet, basename='diag-report')

# 📦 المخزون والمشتريات
router.register(r'products', views.ProductViewSet, basename='product')
router.register(r'stock-alerts', views.StockAlertViewSet, basename='stock-alert')
router.register(r'stock-transfers', views.StockTransferViewSet, basename='stock-transfer')
router.register(r'inventory-movements', views.InventoryMovementViewSet, basename='inventory-movement')
router.register(r'vendors', views.VendorViewSet, basename='vendor')
router.register(r'purchase-invoices', views.PurchaseInvoiceViewSet, basename='purchase-invoice')
router.register(r'services', views.ServiceCatalogViewSet, basename='service')
router.register(r'scrap-jobs', views.ScrapJobViewSet, basename='scrap-job')

# 👥 العملاء والمركبات
router.register(r'customers', views.CustomerViewSet, basename='customer')
router.register(r'vehicles', views.VehicleViewSet, basename='vehicle')
router.register(r'maintenance-contracts', views.MaintenanceContractViewSet, basename='contract')
router.register(r'service-nudges', views.ServiceNudgeViewSet, basename='service-nudge')
router.register(r'customer-feedback', views.CustomerFeedbackViewSet, basename='feedback')

# 💰 الحسابات
router.register(r'treasuries', views.TreasuryViewSet, basename='treasury')
router.register(r'transactions', views.FinancialTransactionViewSet, basename='transaction')
router.register(r'expense-categories', views.ExpenseCategoryViewSet, basename='expense-category')

# 🏢 الفروع
router.register(r'branches', views.BranchViewSet, basename='branch')

# 👷 الموارد البشرية
router.register(r'employees', views.EmployeeViewSet, basename='employee')
router.register(r'attendance', views.AttendanceViewSet, basename='attendance')
router.register(r'leave-requests', views.LeaveRequestViewSet, basename='leave-request')
router.register(r'advances', views.AdvanceViewSet, basename='advance')
router.register(r'payroll-runs', views.PayrollRunViewSet, basename='payroll-run')
router.register(r'payroll-entries', views.PayrollEntryViewSet, basename='payroll-entry')

# 🚗 التشخيص الذكي
router.register(r'diag-devices', views.DiagnosticDeviceViewSet, basename='diag-device')
router.register(r'diag-scans', views.DiagnosticScanViewSet, basename='diag-scan')
router.register(r'fault-logs', views.FaultLogViewSet, basename='fault-log')

urlpatterns = [
    # 🔐 المصادقة
    path('auth/login/', views.MobileTokenObtainPairView.as_view(), name='login'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/me/', views.MeView.as_view(), name='me'),

    # 📊 لوحة المعلومات والتحليلات
    path('dashboard/', views.DashboardView.as_view(), name='dashboard'),
    path('analytics/', views.AnalyticsView.as_view(), name='analytics'),

    # الموارد
    path('', include(router.urls)),
]
