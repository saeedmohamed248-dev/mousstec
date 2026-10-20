"""⚡ شاشات التشغيل اليومي (نقطة البيع، الأصناف، الفواتير، الخزائن، العملاء، الموردين،
المشتريات، التقارير…) — كانت ملف واحد ~٧٠٠٠ سطر واتقسمت موديول لكل مجال.

الكود الجديد يستورد من الموديول نفسه (`from inventory.views_lightning.pos import ...`).
الأسماء هنا متصدّرة عشان `urls.py` وأي استيراد قديم (`views_lightning.X`) يفضلوا شغالين.
"""
from ._common import (  # noqa: F401
    WALK_IN_NAME,
    WALK_IN_PHONE,
    _AR_DIACRITICS,
    _AR_FOLD,
    _RollbackWith,
    _WALK_IN_CREDIT_ERROR,
    _apply_product_search,
    _ar_field_expr,
    _can_bulk_delete_products,
    _can_edit_invoices,
    _finite_dec,
    _is_finance_manager,
    _is_walk_in,
    _named_walk_in,
    _norm_ar,
    _normalize_tenders,
    _post_sale_to_ledger,
    _product_price_visibility,
    _record_invoice_payment,
    _record_invoice_payments,
    _report_branch,
    _report_period,
    _resolve_customer,
    _walk_in_customer,
)
from .pos import (  # noqa: F401
    lightning_pos,
    lightning_pos_checkout,
    product_quick_search,
)
from .products import (  # noqa: F401
    _NAME_PLACEHOLDERS,
    _downscale_to_jpeg_b64,
    _match_product_by_code,
    _match_product_by_filename,
    _product_needs_name,
    bulk_images_smart,
    bulk_images_smart_review_one,
    bulk_images_smart_save,
    bulk_images_smart_targets,
    bulk_product_create,
    bulk_product_entry,
    product_bulk_images,
    product_edit,
    product_gallery,
    product_gallery_upload,
    product_image_delete,
    product_image_primary,
    product_prices_update,
    quick_product_create,
    quick_product_entry,
)
from .job_card import (  # noqa: F401
    DVI_FIELDS,
    customer_search,
    job_card_create,
    job_card_save,
)
from .expenses import (  # noqa: F401
    _EXPENSE_ROLES,
    _SALARY_KEYWORDS,
    _delete_expense_ft,
    _get_or_create_expense_category,
    _is_operating_expense,
    _salary_system_key_for,
    expense_delete,
    expense_edit,
    expense_list,
    quick_expense,
    quick_expense_create,
)
from .sales import (  # noqa: F401
    _can_process_returns,
    _flash_int,
    _flash_money,
    _get_returnable_invoice,
    _return_unit_estimates,
    sale_invoice_deliver,
    sale_invoice_list,
    sale_invoice_return,
)
from .returns_audit import (  # noqa: F401
    _audit_tag,
    returns_audit,
    returns_audit_correct,
)
from .invoice_edits import (  # noqa: F401
    _get_scoped_invoice,
    _pre_tax_discount_for,
    _purge_invoice_payments,
    _resync_invoice_paid,
    sale_invoice_delete,
    sale_invoice_edit,
    sale_invoice_pay,
    sale_invoice_photo_delete,
    sale_invoice_photos,
    sale_invoice_photos_upload,
    sale_invoice_set_name,
)
from .product_list import (  # noqa: F401
    _PRODUCT_EXPORT_COLS,
    _PRODUCT_EXPORT_DEFAULT,
    _can_import_products,
    _effective_unit_cost,
    _expense_share_per_unit,
    _product_list_qs,
    _selected_products,
    _stock_purchase_expenses,
    fixit_sync_now,
    product_bulk_action,
    product_export,
    product_list,
)
from .treasuries import (  # noqa: F401
    _txn_meta,
    transactions_list,
    treasury_create,
    treasury_detail,
    treasury_list,
    treasury_movement,
    treasury_transfer,
    treasury_txn_delete,
)
from .customers import (  # noqa: F401
    _audit_payment_void,
    customer_collect,
    customer_detail,
    customer_payment_void,
    customers_receivables,
)
from .vendors import (  # noqa: F401
    purchase_pay,
    purchase_recost,
    purchase_repair,
    vendor_apply_credit,
    vendor_create,
    vendor_detail,
    vendor_fix_balance,
    vendor_pay,
    vendor_payment_void,
    vendors_payables,
)
from .purchases import (  # noqa: F401
    _get_or_create_purchase_product,
    _purchase_list_qs,
    _reverse_purchase_posting,
    purchase_create,
    purchase_delete,
    purchase_edit,
    purchase_export,
    purchase_import_template,
    purchase_list,
    purchase_return,
    purchase_save,
)
from .shipments import (  # noqa: F401
    _EXPENSE_KIND_WORDS,
    _normalize_expense_kind,
    _shipment_candidates,
    shipment_delete,
    shipment_expenses_extract,
    shipment_form,
    shipment_list,
    shipment_save,
)
from .financial_reports import (  # noqa: F401
    _advance_recoveries,
    balance_sheet,
    pnl_report,
    trial_balance,
)
from .product_import import (  # noqa: F401
    _FIELD_KEYWORDS,
    _FIELD_ORDER,
    _IMPORT_HEADERS,
    _resolve_columns,
    product_import,
)
from .purchase_import import (  # noqa: F401
    invoice_import,
    invoice_import_extract,
    invoice_import_save,
)
from .stock_from_photo import (  # noqa: F401
    _apply_extra_fields,
    _extract_products_from_upload,
    inventory_import,
    inventory_import_extract,
    inventory_import_save,
)
from .reports import (  # noqa: F401
    _sales_invoices,
    customer_statement,
    customers_report,
    products_report,
    reports_hub,
    sales_report,
    services_report,
)
from .advisor import (  # noqa: F401
    business_advisor,
    business_advisor_ask,
    reorder_report,
)
