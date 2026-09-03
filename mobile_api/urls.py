"""
📱 Mobile API — URL routing

جميع المسارات تحت البادئة /api/mobile/v1/ (تُضاف في erp_core/urls.py).
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

app_name = 'mobile_api'

router = DefaultRouter()
router.register(r'work-orders', views.WorkOrderViewSet, basename='work-order')
router.register(r'products', views.ProductViewSet, basename='product')
router.register(r'stock-alerts', views.StockAlertViewSet, basename='stock-alert')
router.register(r'customers', views.CustomerViewSet, basename='customer')

urlpatterns = [
    # 🔐 المصادقة
    path('auth/login/', views.MobileTokenObtainPairView.as_view(), name='login'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/me/', views.MeView.as_view(), name='me'),

    # 📊 لوحة المعلومات
    path('dashboard/', views.DashboardView.as_view(), name='dashboard'),

    # 🔧 / 📦 / 👥 الموارد
    path('', include(router.urls)),
]
