"""URL config for repair_atlas (mounted under /repair-atlas/)."""
from django.urls import path

from . import views

app_name = 'repair_atlas'

urlpatterns = [
    path('', views.repair_atlas_page, name='page'),
    path('customer/', views.repair_atlas_customer_page, name='customer-page'),
    path('api/ask/', views.repair_atlas_ask, name='api-ask'),
    path('api/ask-stream/', views.repair_atlas_ask_stream, name='api-ask-stream'),
    path('api/photo/', views.repair_atlas_photo, name='api-photo'),
    path('api/verdict/<int:answer_id>/', views.repair_atlas_verdict, name='api-verdict'),
    path('api/reset/', views.repair_atlas_reset, name='api-reset'),

    # SuperAdmin review
    path('superadmin/review/', views.review_queue, name='review-queue'),
    path('superadmin/review/<int:answer_id>/',
         views.review_act, name='review-act'),
]
