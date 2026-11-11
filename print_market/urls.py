from django.urls import path

from . import views

app_name = 'print_market'

urlpatterns = [
    path('', views.home, name='home'),
    path('go/', views.home_for_account, name='home_for_account'),
    path('shop/<int:pk>/', views.shop_page, name='shop'),
    path('product/<int:pk>/', views.product_detail, name='product'),
    path('signup/', views.signup, name='signup'),
    path('login/', views.login_view, name='login'),
    path('password/forgot/', views.password_forgot, name='password_forgot'),
    path('password/reset/<str:token>/', views.password_reset, name='password_reset'),
    path('logout/', views.logout_view, name='logout'),
    path('settings/', views.account_settings, name='settings'),
    # المطبعة
    path('dashboard/', views.shop_dashboard, name='shop_dashboard'),
    path('products/new/', views.product_form, name='product_new'),
    path('products/<int:pk>/edit/', views.product_form, name='product_edit'),
    path('products/<int:pk>/delete/', views.product_delete, name='product_delete'),
    # العميل
    path('my-orders/', views.my_orders, name='my_orders'),
    path('order/new/<int:shop_id>/', views.order_new, name='order_new'),
    # الطلب
    path('order/<uuid:code>/', views.order_detail, name='order_detail'),
    path('order/<uuid:code>/offer/', views.offer_submit, name='offer_submit'),
    path('order/<uuid:code>/offer/<int:offer_id>/respond/', views.offer_respond, name='offer_respond'),
    path('order/<uuid:code>/pay/', views.payment_submit, name='payment_submit'),
    path('order/<uuid:code>/proof/', views.proof_submit, name='proof_submit'),
    path('order/<uuid:code>/proof/<int:proof_id>/respond/', views.proof_respond, name='proof_respond'),
    path('order/<uuid:code>/message/', views.message_send, name='message_send'),
    path('order/<uuid:code>/cancel/', views.order_cancel, name='order_cancel'),
    path('order/<uuid:code>/delivered/', views.order_delivered, name='order_delivered'),
    path('order/<uuid:code>/received/', views.order_received, name='order_received'),
    path('file/<str:kind>/<int:pk>/', views.private_file, name='file'),
]
