"""
URLs: HR Module — API routes for mobile/PWA, admin actions, and designer dashboard.
"""

from django.urls import path
from hr import views

app_name = 'hr'

urlpatterns = [
    # --- Attendance Page + APIs ---
    path('attendance/', views.attendance_page, name='attendance_page'),
    path('api/clock-in/', views.api_clock_in, name='api_clock_in'),
    path('api/clock-out/', views.api_clock_out, name='api_clock_out'),
    path('api/my-attendance/', views.api_my_attendance, name='api_my_attendance'),
    path('api/attendance/settings/', views.api_attendance_settings, name='api_attendance_settings'),

    # --- Face Registration ---
    path('api/face/register/', views.api_register_face, name='api_register_face'),
    path('api/face/descriptor/', views.api_get_face_descriptor, name='api_get_face_descriptor'),

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

    # --- HR Manager Dashboard (HTML) ---
    path('manager/', views.hr_manager_dashboard, name='hr_manager_dashboard'),

    # --- AI Subscription Admin APIs ---
    path('api/ai-sub/admin-activate/', views.api_admin_ai_activate, name='api_admin_ai_activate'),
    path('api/ai-sub/admin-cancel/', views.api_admin_ai_cancel, name='api_admin_ai_cancel'),

    # --- Quick Treasury Transaction (Designer dashboard quick actions) ---
    path('api/treasury/quick-transaction/', views.api_quick_treasury_transaction, name='api_quick_treasury_transaction'),
]
