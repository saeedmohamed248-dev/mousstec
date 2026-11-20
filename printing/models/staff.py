"""🔐 المطابع — صلاحيات الموظفين."""
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _


# =====================================================================
# 🔐 7. صلاحيات الموظفين (Staff Permissions)
# =====================================================================

class StaffPermission(models.Model):
    """
    صلاحيات مخصصة لكل موظف — الأدمن يتحكم مين يشوف إيه.
    بدل ما نعتمد على Django Groups/Permissions المعقدة.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='print_permissions', verbose_name=_("الموظف")
    )

    # 📊 مالي
    can_view_treasury = models.BooleanField(default=False, verbose_name=_("مشاهدة الخزينة"))
    can_manage_treasury = models.BooleanField(default=False, verbose_name=_("إدارة الخزينة (إيداع/سحب)"))
    can_view_profits = models.BooleanField(default=False, verbose_name=_("مشاهدة الأرباح والتكاليف"))

    # 📋 الطلبات
    can_create_orders = models.BooleanField(default=True, verbose_name=_("إنشاء طلبات"))
    can_edit_orders = models.BooleanField(default=True, verbose_name=_("تعديل طلبات"))
    can_delete_orders = models.BooleanField(default=False, verbose_name=_("حذف طلبات"))
    can_view_all_orders = models.BooleanField(default=True, verbose_name=_("مشاهدة كل الطلبات"))

    # 👥 العملاء
    can_manage_customers = models.BooleanField(default=True, verbose_name=_("إدارة العملاء"))

    # 📁 ملفات المشاريع
    can_view_project_files = models.BooleanField(default=False, verbose_name=_("مشاهدة ملفات المشاريع"))
    can_upload_project_files = models.BooleanField(default=True, verbose_name=_("رفع ملفات المشاريع"))

    # 📦 المخزون
    can_manage_stock = models.BooleanField(default=False, verbose_name=_("إدارة المخزون"))

    # 🎨 المصممين
    can_view_designers = models.BooleanField(default=False, verbose_name=_("مشاهدة أداء المصممين"))

    # 🤖 AI Studio
    can_use_ai_studio = models.BooleanField(default=False, verbose_name=_("استخدام AI Studio"))

    # 📊 التقارير
    can_view_reports = models.BooleanField(default=False, verbose_name=_("مشاهدة التقارير"))

    class Meta:
        verbose_name = _("صلاحية موظف")
        verbose_name_plural = _("صلاحيات الموظفين")

    def __str__(self):
        return f"صلاحيات: {self.user.get_full_name() or self.user.username}"
