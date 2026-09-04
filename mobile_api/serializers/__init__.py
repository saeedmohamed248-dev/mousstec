"""Mobile API serializers — re-export موحّد لكل الموديولات."""
from .core import UserSerializer, BranchSerializer  # noqa: F401
from .crm import (  # noqa: F401
    VehicleSerializer,
    VehicleWriteSerializer,
    MaintenanceContractSerializer,
    MaintenanceContractWriteSerializer,
    CustomerListSerializer,
    CustomerDetailSerializer,
    CustomerWriteSerializer,
    ServiceNudgeSerializer,
    CustomerFeedbackSerializer,
    VehicleTelemetrySerializer,
)
from .inventory import (  # noqa: F401
    ProductListSerializer,
    ProductDetailSerializer,
    ProductWriteSerializer,
    InventoryLocationSerializer,
    StockAlertSerializer,
    StockTransferSerializer,
    InventoryMovementSerializer,
    VendorSerializer,
    ServiceCatalogSerializer,
    ScrapJobSerializer,
)
from .purchasing import (  # noqa: F401
    PurchaseInvoiceListSerializer,
    PurchaseInvoiceDetailSerializer,
    PurchaseInvoiceItemSerializer,
)
from .workshop import (  # noqa: F401
    WorkOrderListSerializer,
    WorkOrderDetailSerializer,
    WorkOrderCreateSerializer,
    WorkOrderStatusUpdateSerializer,
    WorkOrderItemSerializer,
    WorkOrderServiceSerializer,
    RepairLogSerializer,
    DiagnosticReportSerializer,
)
from .finance import (  # noqa: F401
    TreasurySerializer,
    TreasuryWriteSerializer,
    ExpenseCategorySerializer,
    FinancialTransactionSerializer,
    FinancialTransactionCreateSerializer,
)
from .hr import (  # noqa: F401
    EmployeeSerializer,
    WorkShiftSerializer,
    AttendanceRecordSerializer,
    LeaveRequestSerializer,
    AdvanceSerializer,
    PayrollRunSerializer,
    PayrollEntrySerializer,
)
from .diagnostics import (  # noqa: F401
    DiagnosticDeviceSerializer,
    DiagnosticScanSerializer,
    FaultLogSerializer,
    LiveTelemetrySerializer,
)
