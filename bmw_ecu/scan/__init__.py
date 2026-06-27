"""Full-System Auto-Scan + Live Data.

Two workshop-grade diagnostic features built hardware-free on the same
orchestrator / abstract-provider / mock pattern as the rest of bmw_ecu:

  • Full-System Scan — walk every networked ECU, pull + decode fault
    memory, roll it into one traffic-light health report.
  • Live Data       — stream sensor PIDs with rolling min/max/avg and
    out-of-range anomaly flags for the moving chart.

Both are gated by the granular billing layer
(feature 'full_system_scan' / 'live_data_stream').
"""
from __future__ import annotations

from .dtc_decoder import DecodedDtc, DtcSeverity, decode_dtc
from .full_scan_orchestrator import (
    FullScanOrchestrator,
    ScanData,
    ScanEvent,
    ScanPrompt,
    ScanState,
)
from .health_report import (
    HealthReport,
    ModuleScanResult,
    OverallStatus,
    build_report,
)
from .live_data import (
    PID_CATALOG,
    AbstractLiveDataProvider,
    LiveDataMonitor,
    LiveDataSample,
    LiveDataSession,
    MockLiveDataProvider,
    Pid,
    PidStats,
    UnknownPid,
    get_pid,
)
from .module_map import (
    MODULE_CATALOG,
    ChassisFamily,
    EcuModule,
    ModuleCategory,
    describe_module,
    expected_module_codes,
    expected_modules,
    get_module,
)
from .scan_provider import (
    AbstractScanProvider,
    MockScanProvider,
    ScanTransportError,
)

__all__ = [
    # dtc
    "DecodedDtc", "DtcSeverity", "decode_dtc",
    # module map
    "MODULE_CATALOG", "ChassisFamily", "EcuModule", "ModuleCategory",
    "describe_module", "expected_module_codes", "expected_modules", "get_module",
    # provider
    "AbstractScanProvider", "MockScanProvider", "ScanTransportError",
    # report
    "HealthReport", "ModuleScanResult", "OverallStatus", "build_report",
    # orchestrator
    "FullScanOrchestrator", "ScanData", "ScanEvent", "ScanPrompt", "ScanState",
    # live data
    "PID_CATALOG", "AbstractLiveDataProvider", "LiveDataMonitor",
    "LiveDataSample", "LiveDataSession", "MockLiveDataProvider", "Pid",
    "PidStats", "UnknownPid", "get_pid",
]
