"""
URLs: HR Module — API routes for mobile/PWA, admin actions, and designer dashboard.
"""

from django.urls import path
from hr import views

app_name = 'hr'

urlpatterns = [
    # --- Attendance ---
    path('api/clock-in/', views.api_clock_in, name='api_clock_in'),
    path('api/clock-out/', views.api_clock_out, name='api_clock_out'),
    path('api/my-attendance/', views.api_my_attendance, name='api_my_attendance'),

    # --- Advances ---
    path('api/advance/request/', views.api_request_advance, name='api_request_advance'),
    path('api/advance/mine/', views.api_my_advances, name='api_my_advances'),

    # --- Design Workflow ---
    path('api/design/submit/', views.api_submit_design, name='api_submit_design'),
    path('api/design/mine/', views.api_my_designs, name='api_my_designs'),
    path('api/design/pending/', views.api_pending_reviews, name='api_pending_reviews'),
    path('api/design/<int:submission_id>/review/', views.api_review_design, name='api_review_design'),

    # --- Leave Requests ---
    path('api/leave/request/', views.api_request_leave, name='api_request_leave'),

    # --- Payslip ---
    path('api/payslip/', views.api_my_payslip, name='api_my_payslip'),

    # --- Designer Dashboard (HTML) ---
    path('designer/', views.designer_dashboard, name='designer_dashboard'),

    # --- AI Subscription Admin APIs ---
    path('api/ai-sub/admin-activate/', views.api_admin_ai_activate, name='api_admin_ai_activate'),
    path('api/ai-sub/admin-cancel/', views.api_admin_ai_cancel, name='api_admin_ai_cancel'),
]
