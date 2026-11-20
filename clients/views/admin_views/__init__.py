"""
👑 أدوات السوبر أدمن (مالك المنصة) — لوحة كل الشركات، الهدايا، الدخول على شركة، عملاء السوق
=================================================================================================
🔀 مقسومة موديول لكل مجال — والكل بيتصدّر من هنا، فـ `from clients.views.admin_views import X`
و `clients/views/__init__.py` شغالين زي ما هم. الشاشة الجديدة تتحط في موديول مجالها.

| الموديول | فيه إيه |
|---|---|
| _common               | `_require_superadmin`، تحويل slug الباقة، `ADMIN_URL`، حالات شراء التصاميم المدفوعة |
| dashboard             | لوحة إدارة كل الشركات |
| marketplace_customers | عملاء السوق: التفاصيل، الحذف/الهدية/الإشعار، إشعارات العميل، الحظر |
| gifts                 | هدايا الشركات، اشتراكات التشخيص، منح OBD وشحن التشخيص |
| impersonation         | الدخول كمالك المنصة على أي شركة |
| parts_refunds         | نزاعات واسترداد سوق قطع الغيار |
| print_requests        | طلبات طباعة التصاميم |
"""
from ._common import (  # noqa: F401
    ADMIN_URL, PAID_PURCHASE_STATUSES,
)
from .dashboard import (  # noqa: F401
    super_admin_dashboard,
)
from .marketplace_customers import (  # noqa: F401
    customer_notification_read, customer_notifications_list, super_admin_customer_block,
    super_admin_customer_delete, super_admin_customer_detail, super_admin_customer_gift,
    super_admin_customer_notify,
)
from .gifts import (  # noqa: F401
    super_admin_diag_topup_grant, super_admin_gift_diagnostics, super_admin_obd_quick_grant,
    super_admin_tenant_grants,
)
from .impersonation import (  # noqa: F401
    enter_tenant, impersonate_login,
)
from .parts_refunds import (  # noqa: F401
    super_admin_parts_refund_approve, super_admin_parts_refund_reject,
)
from .print_requests import (  # noqa: F401
    PRINT_TRANSITIONS, super_admin_print_request_update,
)

from ._common import User, logger  # noqa: F401

# أسماء بشرطة تحتية (مش في القوايم اللي فوق) — كانت متاحة من الملف القديم
from ._common import _plan_slug_candidates, _require_superadmin, _resolve_active_plan  # noqa: F401
from .print_requests import _PRINT_NOTICE  # noqa: F401
