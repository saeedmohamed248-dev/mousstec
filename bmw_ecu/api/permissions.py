"""Permissions for the Mousstec super-admin BMW ECU endpoints."""
from rest_framework.permissions import BasePermission


class IsPlatformOwner(BasePermission):
    """
    🔐 Mousstec super-admin only: a superuser on the PUBLIC schema.

    🐛 [FIX]: كان IsAdminUser (أي is_staff) — وصاحب أي شركة is_staff على
    الدومين بتاعه، فكان يقدر يدّي نفسه رصيد تكويد مجاني (فلوس حقيقية).
    """
    def has_permission(self, request, view):
        from django.db import connection
        user = getattr(request, 'user', None)
        return bool(user and user.is_active and user.is_superuser
                    and getattr(connection, 'schema_name', 'public') == 'public')
