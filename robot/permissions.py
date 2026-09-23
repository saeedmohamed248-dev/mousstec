"""
robot/permissions.py — what each employee is allowed to command the robot to do.

Every privileged robot action is gated by the RECOGNIZED employee's role (from
`EmployeeProfile.role` on their linked user), not just by "is a face known".
A cashier can sell; only a warehouse/stock role can intake goods; only a
manager/owner can approve a stock-take adjustment; etc. When an employee asks
for something outside their role, the robot refuses politely (and says why).

Roles (from inventory EmployeeProfile.ROLE_CHOICES):
  owner, admin, manager, supervisor, accountant, sales, purchasing, engineer,
  tech, cashier, stock, hr, viewer
"""

from __future__ import annotations

# Action → roles allowed to perform it. `owner`/`admin` implicitly allowed on
# everything (added below).
_MATRIX = {
    "sale":              {"manager", "supervisor", "sales", "cashier"},
    "intake":            {"manager", "supervisor", "stock", "purchasing"},
    "stock_take":        {"manager", "supervisor", "stock"},
    "stock_take_apply":  {"manager", "supervisor"},          # approval = senior
    "motor":             {"manager", "supervisor", "tech", "engineer", "stock"},
    "customer_enroll":   {"manager", "supervisor", "sales", "cashier"},
    # Teaching the robot (correct a scan, "X يعني Y"): anyone who handles parts.
    "teach":             {"manager", "supervisor", "stock", "purchasing",
                          "sales", "cashier", "tech", "engineer"},
}

_SUPERUSER_ROLES = {"owner", "admin"}

# Friendly Arabic label per action for the spoken refusal.
_ACTION_LABEL = {
    "sale": "إنشاء فاتورة بيع",
    "intake": "إدخال بضاعة",
    "stock_take": "بدء جرد",
    "stock_take_apply": "اعتماد تسوية الجرد",
    "motor": "تحريك الروبوت",
    "customer_enroll": "تسجيل بيانات عميل",
    "teach": "تعليم الروبوت",
}


def employee_role(employee) -> str:
    """Resolve an hr.Employee's business role, or '' if none.

    Reads EmployeeProfile.role off the linked user. Superusers are treated as
    'owner' so a system admin is never locked out.
    """
    if employee is None:
        return ""
    user = getattr(employee, "user", None)
    if user is not None and getattr(user, "is_superuser", False):
        return "owner"
    profile = getattr(user, "employee_profile", None) if user is not None else None
    return getattr(profile, "role", "") or ""


def employee_can(employee, action: str) -> bool:
    """True if `employee`'s role may perform `action`."""
    role = employee_role(employee)
    if not role:
        return False
    if role in _SUPERUSER_ROLES:
        return True
    return role in _MATRIX.get(action, set())


def denial_message(action: str) -> str:
    """Polite spoken refusal when an employee lacks permission for an action."""
    label = _ACTION_LABEL.get(action, "العملية دي")
    return f"معلش، «{label}» محتاج صلاحية أعلى. اطلب من المدير أو المشرف."
