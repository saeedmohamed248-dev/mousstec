"""
clients.views — package facade.

Historically a single 4,936-line module (``_legacy.py``). Now fully split
into focused submodules. URLs reference ``client_views.<name>`` — this
facade preserves that surface by explicitly re-exporting every public
endpoint from its new home.

Explicit re-exports (not wildcard) because Daphne boot was observed to
drop names unreliably from ``from ._submodule import *``, causing
AttributeError in ``erp_core/urls.py`` and a 502 at startup.

Submodule layout
----------------
- :mod:`._shared` — auth / OTP / notification helpers (internal use)
- :mod:`._ai_pipeline` — Brand + Smart Router + Composite + Quality Gate
- :mod:`.auth_views` — tenant signup, landing pages, login finders
- :mod:`.subscription_views` — pricing, Paymob checkout, add-ons
- :mod:`.admin_views` — super-admin dashboard, tenant grants, impersonation
- :mod:`.b2b_views` — B2B marketplace, blind bidding, escrow
- :mod:`.webhook_views` — universal webhook multiplexer
- :mod:`.ai_assistant_views` — landing-page sales bot
- :mod:`.marketplace_core_views` — customer marketplace + service requests
- :mod:`.design_store_views` — AI design store (generate/regenerate/refine)
- :mod:`.brand_profile_views` — Customer Brand Profile CRUD
- :mod:`.design_chat_views` — Conversational design builder (Phase N)
"""
# ───────────────────────────────────────────────────────────────────────────
# 🤖 Landing-page AI assistant
# ───────────────────────────────────────────────────────────────────────────
from .ai_assistant_views import (  # noqa: F401
    ai_assistant_api,
)

# ───────────────────────────────────────────────────────────────────────────
# 💎 Customer-tier diagnostics (separate from workshop diag room)
# ───────────────────────────────────────────────────────────────────────────
from .customer_diagnostics_views import (  # noqa: F401
    diagnostics_landing,
    diagnostics_pricing,
    diagnostics_upgrade,
    diagnostics_scan,
    diagnostics_chat,
    diagnostics_chat_reset,
    diagnostics_paymob_callback,
)

# 🧠 Customer-side sector-aware advisor
from .customer_advisor_views import (  # noqa: F401
    advisor_chat as customer_advisor_chat,
    advisor_reset as customer_advisor_reset,
    advisor_page as customer_advisor_page,
)

# ───────────────────────────────────────────────────────────────────────────
# 🛍️ Marketplace core — customer flows + service requests + merchant feed
# ───────────────────────────────────────────────────────────────────────────
from .marketplace_core_views import (  # noqa: F401
    marketplace_home,
    marketplace_automotive,
    marketplace_printing,
    marketplace_register,
    marketplace_verify_otp,
    marketplace_login,
    marketplace_dashboard,
    marketplace_create_request,
    marketplace_request_detail,
    marketplace_accept_offer,
    marketplace_rate_offer,
    marketplace_merchant_feed,
    marketplace_submit_offer,
    marketplace_logout,
    marketplace_merchant_feed_count,
    marketplace_merchant_create_request,
    marketplace_admin_approve,
    marketplace_admin_reject,
    marketplace_edit_request,
)

# ───────────────────────────────────────────────────────────────────────────
# 🎨 Design Store — AI generation pipeline (C1/C2/C3 unified)
# ───────────────────────────────────────────────────────────────────────────
from .design_store_views import (  # noqa: F401
    design_store_home,
    design_store_buy,
    design_store_payment,
    design_store_confirm_payment,
    design_store_my_designs,
    design_store_my_print_orders,
    design_store_generate,
    design_store_send_whatsapp,
    design_store_download,
    design_store_regenerate,
    design_store_print_request,
    design_store_send_to_marketplace,
    design_store_watermark,
    design_store_chat_history,
    design_store_refine,
)

# ───────────────────────────────────────────────────────────────────────────
# 🎨 Brand Memory — Customer Brand Profile (Phase 5)
# ───────────────────────────────────────────────────────────────────────────
from .brand_profile_views import (  # noqa: F401
    brand_profile_view,
    brand_profile_delete_logo,
    brand_profile_page,
)

# ───────────────────────────────────────────────────────────────────────────
# 💬 Conversational Design Builder (Phase N)
# ───────────────────────────────────────────────────────────────────────────
from .design_chat_views import (  # noqa: F401
    design_chat_start,
    design_chat_message,
    design_chat_undo,
    design_chat_finalize,
    design_chat_state,
    design_chat_page,
)

# ───────────────────────────────────────────────────────────────────────────
# 🔐 Tenant signup / login / landing pages
# ───────────────────────────────────────────────────────────────────────────
from .auth_views import (  # noqa: F401
    register_new_tenant_saas,
    smart_post_login_redirect,
    client_login_finder,
    tenant_auto_login,
    mousstec_landing_page,
    automotive_landing_page,
    printing_landing_page,
    account_recovery,
    change_password,
    verify_email,
    resend_verification,
)

from .mfa_views import (  # noqa: F401
    mfa_setup,
    mfa_disable,
    mfa_challenge,
)

# ───────────────────────────────────────────────────────────────────────────
# 💳 Subscriptions / billing / Paymob
# ───────────────────────────────────────────────────────────────────────────
from .subscription_views import (  # noqa: F401
    saas_pricing_page,
    paymob_checkout,
    paymob_callback,
    manage_subscription,
    purchase_addon_api,
    features_page,
    payment_success,
    payment_failed,
)

# ───────────────────────────────────────────────────────────────────────────
# 💵 Manual payment (Vodafone Cash / InstaPay) — unified receipt upload
# ───────────────────────────────────────────────────────────────────────────
from .manual_payment_views import (  # noqa: F401
    manual_payment_upload,
    manual_pay_subscription_start,
    manual_pay_parts_start,
    manual_pay_design_start,
    manual_pay_diagnostics_start,
    admin_review_receipt,
)

# ───────────────────────────────────────────────────────────────────────────
# 👑 Super-admin tools
# ───────────────────────────────────────────────────────────────────────────
from .admin_views import (  # noqa: F401
    super_admin_dashboard,
    super_admin_customer_detail,
    super_admin_customer_delete,
    super_admin_customer_gift,
    super_admin_customer_notify,
    super_admin_tenant_grants,
    enter_tenant,
    impersonate_login,
    customer_notifications_list,
    customer_notification_read,
    super_admin_parts_refund_approve,
    super_admin_parts_refund_reject,
    super_admin_gift_diagnostics,
    super_admin_obd_quick_grant,
)

# ───────────────────────────────────────────────────────────────────────────
# 🚗 P2P Car-Parts Marketplace
# ───────────────────────────────────────────────────────────────────────────
from .parts_marketplace_views import (  # noqa: F401
    parts_feed,
    parts_detail,
    parts_create,
    parts_checkout,
    parts_paymob_callback,
    parts_my_orders,
    parts_my_sales,
    parts_mark_shipped,
    parts_confirm_delivery,
    parts_request_refund,
    parts_wanted_create,
    parts_wanted_seller_feed,
    parts_open_dispute,
)

# ───────────────────────────────────────────────────────────────────────────
# 🤝 B2B marketplace / blind bidding / escrow
# ───────────────────────────────────────────────────────────────────────────
from .b2b_views import (  # noqa: F401
    b2b_market_search_api,
    active_blind_bids_api,
    submit_bid_offer_api,
    my_escrow_wallet_api,
    market_demand_predictor_api,
)

# ───────────────────────────────────────────────────────────────────────────
# 🪝 Webhooks
# ───────────────────────────────────────────────────────────────────────────
from .webhook_views import universal_webhook_multiplexer  # noqa: F401
