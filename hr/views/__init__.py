"""
👥 شاشات الموارد البشرية (/hr/)
===============================
🔀 مقسومة موديول لكل مجال — والأسماء كلها بتتصدّر من هنا، فـ `hr/urls.py`
(`views.<name>`) شغال زي ما هو. الشاشة الجديدة تتحط في موديول مجالها.

| الموديول | فيه إيه |
|---|---|
| _common      | `_json_response` و `_err_text` |
| attendance   | الحضور والانصراف وبصمة الوجه وإعدادات الحضور |
| self_service | طلبات الموظف: السلف، الإجازات، قسيمة المرتب |
| designs      | أعمال المصممين ومراجعتها ولوحة المصمم |
| manager      | لوحة مدير الموارد البشرية ومراجعة السلف والإجازات |
| admin_tools  | تفعيل/إلغاء اشتراك الذكاء الاصطناعي والحركة السريعة على الخزنة |
"""

from .attendance import (  # noqa: F401
    api_attendance_settings, api_clock_in, api_clock_out, api_get_face_descriptor,
    api_my_attendance, api_register_face, attendance_page,
)
from .self_service import (  # noqa: F401
    api_my_advances, api_my_payslip, api_request_advance, api_request_leave,
)
from .designs import (  # noqa: F401
    api_my_designs, api_pending_reviews, api_review_design, api_submit_design,
    designer_dashboard,
)
from .manager import (  # noqa: F401
    hr_manager_dashboard, hr_review_advance, hr_review_leave,
)
from .admin_tools import (  # noqa: F401
    api_admin_ai_activate, api_admin_ai_cancel, api_quick_treasury_transaction,
)

# أسماء بشرطة تحتية (مش في القوايم اللي فوق) — كانت متاحة من الملف القديم
from ._common import _err_text, _json_response  # noqa: F401
from .manager import _hr_reviewer, _payout_treasuries  # noqa: F401
