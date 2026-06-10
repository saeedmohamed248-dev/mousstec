from django import template
from clients.permissions import get_user_widgets

register = template.Library()


@register.filter(name='can_view')
def can_view(user, widget_name):
    """{% if user|can_view:"revenue" %}...{% endif %}"""
    return widget_name in get_user_widgets(user)


@register.simple_tag
def user_role_label(user):
    if not user or not user.is_authenticated:
        return ''
    if user.is_superuser:
        return '👑 المالك الأعلى'
    role_obj = getattr(user, 'staff_role', None)
    return role_obj.get_role_display() if role_obj else '—'
