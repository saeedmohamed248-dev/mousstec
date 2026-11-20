"""
🎨 Mousstec Printing Admin — لوحة تحكم المطابع والتصميم
=========================================================
🔀 مقسومة موديول لكل مجال (نفس تقسيمة `printing/models/`). الشاشة الجديدة
تتسجّل في موديول مجالها، والمساعد المشترك يتحط في `_common.py`.

| الموديول | فيه إيه |
|---|---|
| _common     | PrintSecureAdmin والـ mixins والفورمات المشتركة |
| customers   | الفروع والعملاء |
| production  | الماكينات، المصممين، الطلبات والمهام، أنواع البنود |
| materials   | الخامات وتسويات المخزن |
| treasury    | الخزائن والحركات والتحويلات ومطابقة البنك وإقرار الضريبة |
| staff       | صلاحيات الموظفين |
| sales       | عروض الأسعار، البروفات، الإشعارات الدائنة |
| purchasing  | الموردين والمشتريات ومرتجعاتها والتسديد من الرصيد المقدّم |
| accounting  | الأصول الثابتة وفواتير المصروفات |
| cheques     | الشيكات |
"""
from ._common import (  # noqa: F401
    PrintSecureAdmin, PrintTransactionForm,
)
from .customers import (  # noqa: F401
    OnAccountPaymentInline, PrintBranchAdmin, PrintCustomerAdmin,
)
from .production import (  # noqa: F401
    DesignerAdmin, DesignerWorkLogAdmin, DesignerWorkLogInline, MachineProfileAdmin,
    PrintChequeInline, PrintCreditAllocationInline, PrintCreditNoteInline, PrintJobAdmin,
    PrintJobInline, PrintJobInlineForm, PrintJobMaterialForm, PrintJobMaterialInline,
    PrintOrderAdmin, PrintOrderForm, PrintOrderPaymentInline, PrintProofInline,
    ProductTypeAdmin, STAGE_COLORS, cancel_order_and_refund, order_whatsapp_text,
)
from .materials import (  # noqa: F401
    PrintMaterialAdmin, PrintStockAdjustmentAdmin, PrintStockAdjustmentForm,
)
from .treasury import (  # noqa: F401
    PrintBankReconciliationAdmin, PrintTransactionAdmin, PrintTransactionInline,
    PrintTreasuryAdmin, PrintTreasuryTransferAdmin, PrintTreasuryTransferForm,
    PrintVatReturnAdmin, TXN_MODES,
)
from .staff import (  # noqa: F401
    StaffPermissionAdmin,
)
from .sales import (  # noqa: F401
    PROOF_STATUS_COLORS, PriceQuotationAdmin, PrintCreditNoteAdmin, PrintCreditNoteForm,
    PrintProofAdmin, QuotationLineInline,
)
from .purchasing import (  # noqa: F401
    MaterialPurchaseAdmin, MaterialPurchaseForm, MaterialPurchaseLineInline,
    MaterialPurchaseReturnAdmin, MaterialPurchaseReturnForm,
    MaterialPurchaseReturnLineInline, PURCHASE_STATUS_COLORS, PrintSupplierAdmin,
    PrintSupplierAllocationAdmin, PurchasePaymentInline, SupplierPaymentInline,
)
from .accounting import (  # noqa: F401
    BillPaymentInline, PrintExpenseBillAdmin, PrintExpenseBillForm, PrintFixedAssetAdmin,
    PrintFixedAssetForm,
)
from .cheques import (  # noqa: F401
    CHEQUE_STATUS_COLORS, PrintChequeAdmin, PrintChequeForm,
)

# مساعدات بشرطة تحتية (مش في القوايم اللي فوق) ومستخدمة برا الحزمة
from ._common import _money, _amount_html, _CreatedByFormsetMixin, _SafeSaveDeleteMixin  # noqa: F401
from .customers import _apply_supplier_credit, _money_txns  # noqa: F401
from .production import _order_paid, _wa_number, _proof_links  # noqa: F401
