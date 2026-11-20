"""🔐 لوحة المطابع — صلاحيات الموظفين."""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from ..models import (
    StaffPermission,
)
from ._common import PrintSecureAdmin


# =====================================================================
# 🔐 صلاحيات الموظفين
# =====================================================================

@admin.register(StaffPermission)
class StaffPermissionAdmin(PrintSecureAdmin):
    list_display = (
        'user', 'can_view_treasury', 'can_view_profits',
        'can_view_project_files', 'can_use_ai_studio',
        'can_manage_stock', 'can_view_reports',
    )
    list_filter = (
        'can_view_treasury', 'can_view_profits',
        'can_use_ai_studio', 'can_view_reports',
    )
    list_editable = (
        'can_view_treasury', 'can_view_profits',
        'can_view_project_files', 'can_use_ai_studio',
        'can_manage_stock', 'can_view_reports',
    )
    fieldsets = (
        (_('👤 الموظف'), {'fields': ('user',)}),
        (_('💰 الصلاحيات المالية'), {
            'fields': ('can_view_treasury', 'can_manage_treasury', 'can_view_profits'),
            'description': _('⚠️ صلاحيات حساسة — فقط للمديرين والمحاسبين'),
        }),
        (_('📋 الطلبات'), {
            'fields': ('can_create_orders', 'can_edit_orders', 'can_delete_orders', 'can_view_all_orders'),
        }),
        (_('📁 الملفات والعملاء'), {
            'fields': ('can_view_project_files', 'can_upload_project_files', 'can_manage_customers'),
        }),
        (_('📦 المخزون والمصممين'), {
            'fields': ('can_manage_stock', 'can_view_designers'),
        }),
        (_('🤖 AI وتقارير'), {
            'fields': ('can_use_ai_studio', 'can_view_reports'),
        }),
    )
