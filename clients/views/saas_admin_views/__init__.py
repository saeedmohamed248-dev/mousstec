"""
🎛️ SaaS Super-Admin Control Center — لوحة مالك المنصة (/superadmin/)
======================================================================
كل الشاشات محمية بـ `saas_admin_required` (superuser + public schema) و/أو
`platform_owner_required` / `widget_required`.

🔀 مقسومة موديول لكل مجال — والكل بيتصدّر من هنا، فـ `erp_core/urls.py`
(`saas_admin_views.<view>`) وأي `from clients.views.saas_admin_views import X` شغالين زي ما هم.
الشاشة الجديدة تتحط في موديول مجالها.

| الموديول | فيه إيه |
|---|---|
| _common     | البوابة الأمنية `saas_admin_required` و `_log_event` |
| plans       | الباقات، النسخ، عروض الخصم، أكواد الخصم والإحالة |
| team        | قفل/فتح الموديولات وفريق المنصة |
| analytics   | الإيرادات، تكلفة الـ AI، مصادر التسويق، إنفاق التشخيص، ذكاء الإيرادات |
| tenants     | الشركات: حذف/استرجاع، OBD، خطر الإلغاء، التجارب، صفحة 360، التجديد، التحكم، البحث السريع |
| monitoring  | الأخطاء، مركز الأمان، سجل الأحداث، حالة النظام، الزوار اللايف |
| marketplace | مراجعة قطع السوق، طلبات نشر B2B، النزاعات والمدفوعات وطلبات السوق |
| broadcast   | حملات الإيميل والبانرات |

⚠️ في الاختبارات: الـ mock.patch يتعمل على الموديول اللي فيه الدالة فعلاً
(مثلاً `clients.views.saas_admin_views.monitoring._check_database`) مش على الحزمة.
"""
from ._common import (  # noqa: F401
    saas_admin_required,
)
from .plans import (  # noqa: F401
    coupons_list, pb_referral_percent, plan_management_clone, plan_management_edit,
    plan_management_list, promotions_list,
)
from .team import (  # noqa: F401
    modules_list, team_list,
)
from .analytics import (  # noqa: F401
    ai_usage, diagnostics_spend_dashboard, marketing_sources, revenue_dashboard,
    revenue_intel,
)
from .tenants import (  # noqa: F401
    churn_risk, obd_access_grant, obd_access_list, obd_access_revoke, quick_search,
    tenant_360, tenant_control, tenant_force_delete, tenant_renew_subscription,
    tenant_restore, tenant_soft_delete, tenants_list, trials_watch,
)
from .monitoring import (  # noqa: F401
    audit_log_list, live_market, live_visitor, security_action, security_center,
    system_error_autofix, system_error_resolve, system_errors_bulk, system_errors_list,
    system_status,
)
from .marketplace import (  # noqa: F401
    b2b_listing_request_decide, b2b_listing_requests, b2b_listing_requests_approve_bulk,
    dispute_resolve, disputes_queue, marketplace_requests_list, parts_active_listings,
    parts_listing_edit, parts_listing_soft_delete, parts_listing_suspend,
    parts_moderation_approve, parts_moderation_queue, parts_moderation_reject,
    payout_mark_paid, payouts_queue,
)
from .broadcast import (  # noqa: F401
    broadcast_banner_dismiss, broadcast_enable_banner, broadcast_list, broadcast_retry,
    broadcast_send,
)

# أسماء بشرطة تحتية (مش في القوايم اللي فوق) — كانت متاحة من الملف القديم
from ._common import _log_event, _saas_admin_required  # noqa: F401
from .plans import _PLAN_INT_FIELDS, _PLAN_LIMIT_FIELDS, _PROMO_DURATIONS, _create_promotion, _handle_plan_edit_post, _parse_local_datetime, _parse_plan_details, _plain_number, _push_limits_to_subscribers  # noqa: F401
from .team import _temp_password  # noqa: F401
from .analytics import _parse_day  # noqa: F401
from .tenants import _OBD_DURATIONS  # noqa: F401
from .monitoring import _ERR_WINDOWS, _check_cache, _check_celery, _check_database, _check_resources, _errors_csv, _errors_filters, _errors_overview, _errors_qs_string, _errors_queryset  # noqa: F401
from .marketplace import _notify_dispute_outcome  # noqa: F401
