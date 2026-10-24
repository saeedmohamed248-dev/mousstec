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
    # Starting a staff face-enrollment round by voice (the dashboard is the
    # usual way; at first install nobody can be recognized yet).
    "enroll_staff":      {"manager"},
    # Teaching the robot (correct a scan, "X يعني Y"): anyone who handles parts.
    "teach":             {"manager", "supervisor", "stock", "purchasing",
                          "sales", "cashier", "tech", "engineer"},
    # Money out of a branch treasury / onto a vendor's account, by voice.
    "expense":           {"manager", "supervisor", "accountant", "cashier"},
    "purchase":          {"manager", "supervisor", "purchasing", "accountant"},
    # Quran / music in the shop, by voice (robot/assistant.py).
    "media":             {"manager", "supervisor"},
    # The staff's questions: today's sales, what's running low.
    "sales_report":      {"manager", "supervisor", "accountant"},
    "stock_report":      {"manager", "supervisor", "stock", "purchasing"},
    # What the ERP doesn't know, looked up on the internet (Google Search).
    "web_search":        {"manager", "supervisor"},
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
    "enroll_staff": "تسجيل بصمات الموظفين",
    "expense": "تسجيل مصروف",
    "purchase": "تسجيل فاتورة شراء",
    "media": "تشغيل القرآن والموسيقى",
    "sales_report": "مبيعات النهارده",
    "stock_report": "النواقص",
    "web_search": "البحث على النت",
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


def action_label(action: str) -> str:
    return _ACTION_LABEL.get(action, "العملية دي")


def denial_message(action: str) -> str:
    """Polite spoken refusal when an employee lacks permission for an action."""
    label = _ACTION_LABEL.get(action, "العملية دي")
    return f"معلش، «{label}» محتاج صلاحية أعلى. اطلب من المدير أو المشرف."
