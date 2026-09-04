"""Mobile API views — re-export موحّد."""
from .core import (  # noqa: F401
    MobileTokenObtainPairView,
    MeView,
    DashboardView,
    AnalyticsView,
    BranchViewSet,
)
from .crm import (  # noqa: F401
    CustomerViewSet,
    VehicleViewSet,
    MaintenanceContractViewSet,
    ServiceNudgeViewSet,
    CustomerFeedbackViewSet,
)
from .inventory import (  # noqa: F401
    ProductViewSet,
    StockAlertViewSet,
    StockTransferViewSet,
    InventoryMovementViewSet,
    VendorViewSet,
    ServiceCatalogViewSet,
    ScrapJobViewSet,
)
from .purchasing import PurchaseInvoiceViewSet  # noqa: F401
from .workshop import (  # noqa: F401
    WorkOrderViewSet,
    RepairLogViewSet,
    DiagnosticReportViewSet,
)
from .finance import (  # noqa: F401
    TreasuryViewSet,
    FinancialTransactionViewSet,
    ExpenseCategoryViewSet,
)
from .hr import (  # noqa: F401
    EmployeeViewSet,
    AttendanceViewSet,
    LeaveRequestViewSet,
    AdvanceViewSet,
    PayrollRunViewSet,
    PayrollEntryViewSet,
)
from .diagnostics import (  # noqa: F401
    DiagnosticDeviceViewSet,
    DiagnosticScanViewSet,
    FaultLogViewSet,
)
