"""
🚗 سوق قطع الغيار P2P (أفراد ↔ أفراد) — الواجهات.

Flow:
  Seller flow:    /marketplace/parts/sell/   → upload photos → publish listing
  Buyer flow:     /marketplace/parts/       → filter by make → detail → checkout (Paymob) → escrow
  Post-purchase:  /marketplace/parts/orders/ → confirm delivery → warranty window → auto-release

  Seller tools:   /marketplace/parts/my-listings/ → track moderation / withdraw a listing
  Wanted flow:    buyer posts /parts/wanted/new/ → sellers answer from
                  /parts/wanted/sellers/ → buyer accepts on /parts/wanted/mine/
                  → a private listing reserved for that buyer → normal checkout

Commission: from settings (MARKETPLACE_PARTS_COMMISSION_*_PCT) — 0% for now.
Return shipping: never paid by the platform — buyer pays on change-of-mind,
seller pays when the part is defective / wrong / not as described
(see marketplace_b2b.services.escrow.RETURN_REASON_TO_PAYER).

🔀 كانت ملف واحد (١٥٠٠+ سطر) واتقسمت موديول لكل مجال — والكل بيتصدّر من هنا،
فأي `from clients.views.parts_marketplace_views import X` شغال زي ما هو.
الشاشة الجديدة تتحط في موديول مجالها.

| الموديول | فيه إيه |
|---|---|
| listings | صفحة القطعة، رفع إعلان (فحص الصور + تنظيف الفورم)، تقييم البايع |
| checkout | Paymob، بيانات الشحن، حجز القطعة، الـ callback (escrow) |
| orders   | مشترياتي/مبيعاتي، الشحن، الاستلام، المرتجع، الإلغاء، التقييم، المحفظة |
| seller   | دليل الأسعار، إعلاناتي، سحب/تعديل إعلان، المتابعة ❤ والمحفوظات |
| disputes | النزاعات وفك الضمان التلقائي |
| (parts_wanted_views) | «مطلوب قطعة» — ملف منفصل وبنعيد تصديره هنا |

⚠️ في الاختبارات: الـ mock.patch يتعمل على الموديول اللي فيه الدالة فعلاً
(مثلاً `clients.views.parts_marketplace_views.checkout._paymob_create_payment`) مش على الحزمة.
"""
from .listings import (  # noqa: F401
    MAX_PHOTO_BYTES, _clean_listing_post, _company_contact, _validate_photo,
    create_listing_from_post, parts_create, parts_detail, seller_rating_summary,
)
from .checkout import (  # noqa: F401
    _checkout_shipping, _paymob_create_payment, parts_checkout, parts_paymob_callback,
    reserve_listing_for_checkout,
)
from .orders import (  # noqa: F401
    _decorate_orders, _orders_page_choices, parts_cancel_order, parts_confirm_delivery,
    parts_mark_shipped, parts_my_orders, parts_my_sales, parts_rate_order,
    parts_request_refund, parts_wallet,
)
from .seller import (  # noqa: F401
    _CONTENT_FIELDS, _notify_price_drop, parts_listing_edit, parts_listing_watch,
    parts_listing_withdraw, parts_my_listings, parts_price_guide, parts_saved,
)
from .disputes import (  # noqa: F401
    auto_release_expired_warranties, parts_open_dispute,
)
from clients.views._parts_helpers import (  # noqa: F401
    _ACTIVE_ORDER_STATUSES, _parse_int, _parse_money,
)
# «مطلوب قطعة» في parts_wanted_views.py — بنعيد تصديرها عشان الـ imports القديمة
from clients.views.parts_wanted_views import (  # noqa: F401
    parts_wanted_cancel, parts_wanted_create, parts_wanted_my_requests,
    parts_wanted_offer_accept, parts_wanted_offer_submit, parts_wanted_offer_withdraw,
    parts_wanted_seller_feed,
)
