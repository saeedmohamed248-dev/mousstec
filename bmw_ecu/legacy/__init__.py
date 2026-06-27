"""FRM3 Footwell Module recovery for E-series + MINI R56.

The FRM3 is the BMW E-chassis (and MINI R5x) lighting + footwell
controller built on a Freescale MC9S12XEP100 16-bit microcontroller.
Its D-Flash region is famous for corruption: an undervoltage event
during a sleep/wake cycle can flip cells mid-write, bricking the
exterior + interior lights. The car still starts but every lamp throws
a fault; the dealer's fix is a 600 EUR module swap.

Mousstec's workshop tool recovers the module without replacement:

    1. Connect the BDM (Background Debug Mode) harness to the FRM3's
       internal pads.
    2. Read the corrupted 8 KB D-Flash region.
    3. Ship the dump (+ VIN) to the Mousstec cloud rebuilder, which
       diffs against the known-good template for this VIN's chassis +
       FA and emits a clean blob.
    4. Flash the clean blob back via BDM.
    5. Inject the VO/FA (SALAPA codes) over UDS so the FRM3 re-reads
       its options from the freshly-restored D-Flash.

Modules
-------
frm_profiles        — E90 + R56 chassis specifics (D-Flash size, BDM
                      timings, VO base codes).
bdm_transport       — ABC + Mock for the BDM physical interface.
dflash_corruption   — analyzer that classifies a raw dump as healthy,
                      partially corrupted, or fully bricked.
cloud_rebuild       — VIN-keyed template + per-vehicle patch synthesis.
vo_fa_injector      — SALAPA / FA writeback after restore.
frm_recovery        — orchestrator state machine driving the full flow.
"""
from __future__ import annotations

from .bdm_transport import (
    AbstractBdmTransport,
    BdmConnectionError,
    BdmReadError,
    BdmWriteError,
    MockBdmTransport,
)
from .cloud_rebuild import (
    CloudRebuildError,
    CloudRebuildResult,
    rebuild_dflash,
)
from .dflash_corruption import (
    CorruptionLevel,
    CorruptionReport,
    analyze_dflash,
)
from .frm_profiles import (
    FRM_PROFILES,
    FrmProfile,
    FrmVariant,
    get_frm_profile,
)
from .frm_recovery import (
    FrmRecoveryEvent,
    FrmRecoveryOrchestrator,
    FrmRecoveryPrompt,
    FrmRecoveryState,
    FrmRecoveryData,
    IllegalFrmTransition,
)
from .vo_fa_injector import (
    FaPayload,
    SalapaCode,
    SalapaInjectionError,
    build_fa_payload,
    parse_salapa,
)

__all__ = [
    "AbstractBdmTransport",
    "BdmConnectionError",
    "BdmReadError",
    "BdmWriteError",
    "MockBdmTransport",
    "CloudRebuildError",
    "CloudRebuildResult",
    "rebuild_dflash",
    "CorruptionLevel",
    "CorruptionReport",
    "analyze_dflash",
    "FRM_PROFILES",
    "FrmProfile",
    "FrmVariant",
    "get_frm_profile",
    "FrmRecoveryEvent",
    "FrmRecoveryOrchestrator",
    "FrmRecoveryPrompt",
    "FrmRecoveryState",
    "FrmRecoveryData",
    "IllegalFrmTransition",
    "FaPayload",
    "SalapaCode",
    "SalapaInjectionError",
    "build_fa_payload",
    "parse_salapa",
]
