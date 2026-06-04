"""RBAC decorators for the unified DMS workspace flow.

`role_required` enforces strict cross-role lockout. Superusers always pass.
"""
from functools import wraps
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect


def role_required(*allowed_roles, json_response=False, login_url='/secure-portal/login/'):
    """Restrict a view to specific EmployeeProfile.role values.

    Usage:
        @role_required('tech', 'engineer')
        def tech_workspace(request): ...

        @role_required('tech', 'engineer', json_response=True)
        def repair_log_start(request): ...   # APIs return JSON 403
    """
    def decorator(view):
        @wraps(view)
        def _wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                if json_response:
                    return JsonResponse({"error": "auth_required"}, status=401)
                return redirect(login_url)

            if user.is_superuser:
                return view(request, *args, **kwargs)

            profile = getattr(user, 'employee_profile', None)
            if profile is None or profile.role not in allowed_roles:
                msg = "صلاحية غير متاحة لهذا الدور."
                if json_response:
                    return JsonResponse({"error": "forbidden", "detail": msg}, status=403)
                return HttpResponseForbidden(msg)

            return view(request, *args, **kwargs)
        return _wrapped
    return decorator
