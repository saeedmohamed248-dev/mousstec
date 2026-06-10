"""
🔐 RBAC helpers للـ Super Admin Control Center.

الاستخدام:
    @role_required('support', 'tech_admin')
    def support_inbox(request): ...

    {% if user|can_view:"revenue" %}<div class="card">...</div>{% endif %}
"""
from functools import wraps
from django.core.exceptions import PermissionDenied
from django.db import connection


# نفس الـ ROLE_WIDGETS من StaffRole — مكرر هنا للسرعة بدون استيراد دائري
_GOD_WIDGETS = {'revenue', 'tenants', 'tickets', 'chat', 'errors', 'plans', 'escrow', 'b2b', 'visitors'}


def get_user_widgets(user) -> set:
    """يرجّع set الـ widgets اللي المستخدم يقدر يشوفها."""
    if not user or not user.is_authenticated:
        return set()
    if user.is_superuser:
        return _GOD_WIDGETS
    role_obj = getattr(user, 'staff_role', None)
    if not role_obj:
        return set()
    return role_obj.visible_widgets


def role_required(*allowed_roles):
    """
    @role_required('support', 'tech_admin')
    Decorator: يسمح للـ is_superuser دائماً، وللأدوار المحددة في staff_role.
    """
    def deco(view_fn):
        @wraps(view_fn)
        def _wrap(request, *args, **kwargs):
            u = request.user
            if not (u.is_authenticated and connection.schema_name == 'public'):
                raise PermissionDenied("Public schema + authenticated required.")
            if u.is_superuser:
                return view_fn(request, *args, **kwargs)
            role_obj = getattr(u, 'staff_role', None)
            if not role_obj or role_obj.role not in allowed_roles:
                raise PermissionDenied(f"Role {allowed_roles} required.")
            return view_fn(request, *args, **kwargs)
        return _wrap
    return deco


def widget_required(widget_name):
    """يسمح للـ user اللي عنده widget معين في صلاحياته."""
    def deco(view_fn):
        @wraps(view_fn)
        def _wrap(request, *args, **kwargs):
            if widget_name not in get_user_widgets(request.user):
                raise PermissionDenied(f"Widget '{widget_name}' not allowed for this role.")
            return view_fn(request, *args, **kwargs)
        return _wrap
    return deco
