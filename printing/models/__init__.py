"""
🎨 Mousstec Printing & Design Module
=====================================
نظام إدارة المطابع واستوديوهات التصميم — معزول تماماً عن قطاع السيارات.
كل tenant في قطاع الطباعة يحصل على هذه الجداول في الـ schema الخاص به.

🔀 الموديلات مقسومة موديول لكل مجال — والكل بيتصدّر من هنا، فأي
`from printing.models import X` شغال زي ما هو. الموديل الجديد يتحط في موديول
مجاله (أو موديول جديد يتضاف هنا بالترتيب: الموديول لازم ييجي بعد اللي بيستورد منهم فوق).

| الموديول | فيه إيه |
|---|---|
| customers   | الفروع والعملاء |
| production  | الماكينات، المصممين، أنواع البنود، أوامر وشغلانات الطباعة |
| materials   | الخامات واستهلاكها وتسويات المخزون |
| treasury    | الخزائن والحركات والتحويلات ومطابقة البنك وإقرار الضريبة |
| staff       | صلاحيات الموظفين |
| sales       | عروض الأسعار، البروفات، الرصيد المقدّم، الإشعار الدائن |
| purchasing  | الموردين والمشتريات ومرتجعاتها وسداد المورد من الرصيد المقدّم |
| cheques     | الشيكات (أوراق القبض والدفع) |
| accounting  | الدفتر المحاسبي، الأصول الثابتة، فواتير المصروفات، فرع كل قيد |
| pricing     | قايمة الأسعار بشرايح الكمية (حاسبة التسعير والمتجر الأونلاين) |
"""
from .customers import (  # noqa: F401
    PrintBranch, PrintCustomer,
)
from .production import (  # noqa: F401
    Designer, DesignerWorkLog, MachineProfile, PrintJob, PrintOrder, ProductType,
)
from .pricing import (  # noqa: F401
    PrintPriceRule, PrintPriceTier,
)
from .materials import (  # noqa: F401
    PrintJobMaterial, PrintMaterial, PrintStockAdjustment,
)
from .treasury import (  # noqa: F401
    PrintBankReconciliation, PrintTransaction, PrintTreasury, PrintTreasuryTransfer,
    PrintVatReturn,
)
from .staff import (  # noqa: F401
    StaffPermission,
)
from .sales import (  # noqa: F401
    PriceQuotation, PrintCreditAllocation, PrintCreditNote, PrintProof, QuotationLine,
)
from .purchasing import (  # noqa: F401
    MaterialPurchase, MaterialPurchaseLine, MaterialPurchaseReturn,
    MaterialPurchaseReturnLine, PrintSupplier, PrintSupplierAllocation,
)
from .cheques import (  # noqa: F401
    PrintCheque,
)
from .accounting import (  # noqa: F401
    PrintExpenseBill, PrintFixedAsset, PrintJournalBranch, PrintLedgerState,
)

# مساعدات بشرطة تحتية (مش في القوايم اللي فوق) ومستخدمة برا الحزمة
from .materials import _shift_job_snapshot  # noqa: F401
from ._common import _save_with_number  # noqa: F401
