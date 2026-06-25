"""Initialize a newly installed module by applying the car's active FA.

Production flow for replacing an EPS rack / EGS gearbox / FEM body
controller:
    1. Read FA from the VCM (vo_parser.read_vo_from_vcm).
    2. Dispatch to the per-module coder (dme/fem/eps already exist) with
       a synthesised coding blob derived from the relevant FA options.
    3. PreflightGate runs first (battery + backup of the new module's
       current EEPROM), RollbackGuard wraps the write.
    4. Verify by read-back of the coding index / variant DID.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from ..exceptions import CodingError
from ..logging_setup import get_logger
from ..safety.preflight import PreflightGate
from ..uds.client import UdsClient
from ..uds.security_access import SecurityAccess
from .dme import code_dme
from .eps import code_eps
from .fa_vo import VehicleOrder
from .fem import code_fem

log = get_logger(__name__)

ModuleId = str  # "EPS" | "EGS" | "DME" | "FEM" | "FRM" | "EKPS" | …


@dataclass
class ModuleInitResult:
    module_id: ModuleId
    coded_options_count: int
    verified: bool
    notes: str = ""


# Mapping of module → relevant FA option subset (the codes that actually
# affect that module's coding blob). Conservative defaults; per-platform
# overrides land in a separate table in production.
_RELEVANT_OPTIONS: dict[ModuleId, set[str]] = {
    "EPS": {"S216", "S217", "S2VB", "S2VC"},        # variable steering / Servotronic
    "EGS": {"S205", "S2TB"},                         # 8HP variants
    "DME": {"S205", "S322", "S488", "S4HA"},         # transmission, sports kit
    "FEM": {"S521", "S522", "S5AC", "S5AS", "S5DM"}, # lights, comfort access
    "FRM": {"S521", "S524", "S563"},                 # footwell / lighting
    "EKPS": {"S2VB"},                                # fuel pump
}


async def initialize_replaced_module(
    *,
    client: UdsClient,
    security: SecurityAccess,
    preflight: PreflightGate,
    vin: str,
    module_id: ModuleId,
    vo: VehicleOrder,
    backup_dump: Callable[[], Awaitable[bytes]],
) -> ModuleInitResult:
    """End-to-end init: pre-flight → dispatch → verify.

    `backup_dump` is an async callable returning the new module's current
    EEPROM/coding region (RequestUpload). Provided by caller so each
    module type can supply its specific address range.
    """
    if module_id not in _RELEVANT_OPTIONS:
        raise CodingError(f"No init template for module {module_id!r}")

    relevant = _RELEVANT_OPTIONS[module_id] & vo.options
    log.info("Module init begin", extra={
        "module": module_id, "matched_options": sorted(relevant),
    })

    # Pre-flight: battery check + backup the current state.
    await preflight.check(
        vin=vin, ecu_name=module_id, memory_region="EEPROM",
        dump_callable=backup_dump, write_kind="coding",
    )

    coding_blob = _synthesise_coding_blob(module_id, relevant)

    if module_id == "EPS":
        await code_eps(client, security, vin=vin, vo=vo,
                       variant_code=coding_blob)
    elif module_id == "DME":
        await code_dme(client, security, vin=vin, vo=vo,
                       coding_index=coding_blob)
    elif module_id == "FEM":
        await code_fem(client, security, vin=vin, vo=vo,
                       coding_blob=coding_blob)
    else:
        # EGS / FRM / EKPS share the FEM-shape DID-write pattern; specific
        # DIDs live in the per-module pin-and-DID profile (not in this MVP).
        raise CodingError(
            f"Module {module_id} init path not yet wired — "
            f"add a per-module coder in bmw_ecu/coding/",
        )

    verified = await _verify_module(client, module_id, coding_blob)
    return ModuleInitResult(
        module_id=module_id,
        coded_options_count=len(relevant),
        verified=verified,
        notes=f"applied options: {sorted(relevant)}" if relevant else
              "no FA options matched this module — applied defaults",
    )


def _synthesise_coding_blob(module_id: ModuleId,
                            options: set[str]) -> bytes:
    """Deterministic coding blob from the matched options.

    MVP encoding: 4-byte header (module fingerprint) + bitmap of which
    relevant options are present, sorted. Real platforms have per-DID
    CAFD trees — this layer is the seam where a vendor's CAFD library
    plugs in (E-Sys SGBD, AutoCode, etc.).
    """
    header = module_id.encode("ascii").ljust(4, b"\x00")[:4]
    sorted_opts = sorted(options)
    bitmap = 0
    for i, _ in enumerate(sorted_opts[:32]):  # cap to 32-bit bitmap for MVP
        bitmap |= (1 << i)
    return header + bitmap.to_bytes(4, "big")


async def _verify_module(client: UdsClient, module_id: ModuleId,
                         expected: bytes) -> bool:
    """Read back the coding region and compare prefix.

    Tolerant compare — many ECUs pad the coding region; we only check the
    first len(expected) bytes match.
    """
    verify_did = {"EPS": 0xF110, "DME": 0xF015, "FEM": 0xF050}.get(module_id)
    if verify_did is None:
        return True  # no DID mapped; trust the per-module coder's own verify
    try:
        readback = await client.read_data_by_identifier(verify_did)
    except Exception as e:
        log.warning("Verify read failed", extra={"err": str(e)})
        return False
    return readback[: len(expected)] == expected
