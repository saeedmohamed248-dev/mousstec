"""
🔐 صلاحيات تطبيق الموبايل — نفس أدوار البوابة.

🐛 [FIX]: كل المسارات كانت محميّة بـ IsAuthenticated بس — أي موظف (فني/كاشير)
داخل من التطبيق كان يقدر يقرا أرصدة الخزائن ومرتبات الموظفين، ويضيف حركات
فلوس ويعدّل رصيد خزنة، ويمسح موردين ويغيّر أسعار، ويوافق على سلفته بنفسه.

كل ViewSet بيحدّد:
    read_roles  — مين يقرا (None = أي موظف)
    write_roles — مين يكتب (الافتراضي: الأدمن والمدير)
ودور «مراجع / عرض فقط» (viewer) عمره ما يكتب.
"""
from rest_framework.permissions import SAFE_METHODS, BasePermission

ALIASES = {'owner': 'admin', 'supervisor': 'manager'}

ADMIN = ('admin',)
MANAGEMENT = ('admin', 'manager')
FINANCE = ('admin', 'manager', 'accountant')
FINANCE_DESK = ('admin', 'manager', 'accountant', 'cashier')
HR_STAFF = ('admin', 'manager', 'hr')
PAYROLL = ('admin', 'manager', 'hr', 'accountant')
PURCHASING = ('admin', 'manager', 'accountant', 'purchasing')
STOCK = ('admin', 'manager', 'stock', 'purchasing')
STOCK_MOVES = ('admin', 'manager', 'accountant', 'stock', 'purchasing')
COST_VIEWERS = ('admin', 'manager', 'accountant', 'purchasing', 'stock')
FRONT_DESK = ('admin', 'manager', 'accountant', 'sales', 'cashier', 'engineer', 'tech',
              'stock', 'purchasing')


def role_of(user):
    """الدور الأساسي للمستخدم (owner→admin، supervisor→manager، superuser→admin)."""
    if getattr(user, 'is_superuser', False):
        return 'admin'
    role = getattr(getattr(user, 'employee_profile', None), 'role', None)
    return ALIASES.get(role, role)


def has_role(user, roles):
    return roles is None or role_of(user) in roles


def sees_all_branches(user):
    if getattr(user, 'is_superuser', False):
        return True
    prof = getattr(user, 'employee_profile', None)
    return bool(prof and prof.sees_all_branches())


def branch_ids(user):
    """الفروع اللي الموظف يشوفها (None = كل الفروع)."""
    if sees_all_branches(user):
        return None
    prof = getattr(user, 'employee_profile', None)
    if prof is None:
        return []
    ids = set(prof.branch_access.values_list('branch_id', flat=True))
    if prof.branch_id:
        ids.add(prof.branch_id)
    return ids


def can_edit_branch(user, branch_id):
    if role_of(user) == 'viewer':
        return False
    if sees_all_branches(user):
        return True
    prof = getattr(user, 'employee_profile', None)
    return bool(prof and prof.can_edit_branch(branch_id))


def can_see_costs(user):
    if has_role(user, COST_VIEWERS):
        return True
    prof = getattr(user, 'employee_profile', None)
    return bool(prof and prof.can_see_costs)


class RoleScoped(BasePermission):
    """read_roles / write_roles على الـ view (ولو action ليه حدوده، action_roles)."""
    message = 'صلاحيتك مش كفاية للعملية دي.'

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        action_roles = getattr(view, 'action_roles', {}) or {}
        action = getattr(view, 'action', None)
        if action in action_roles:
            roles = action_roles[action]
            if request.method not in SAFE_METHODS and role_of(user) == 'viewer':
                return False
            return has_role(user, roles)
        if request.method in SAFE_METHODS:
            return has_role(user, getattr(view, 'read_roles', None))
        if role_of(user) == 'viewer':
            return False
        return has_role(user, getattr(view, 'write_roles', MANAGEMENT))
