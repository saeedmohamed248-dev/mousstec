"""Smart Harness abstraction.

Production wires this to the Mousstec Breakout Box (USB-CAN + 4-channel
power switch + I²C bus). Tests use MockSmartHarness so the orchestrator
can be driven end-to-end without a real bench.

The abstraction is intentionally THIN: a handful of awaitable methods
that map 1:1 to physical operations. Anything richer (multi-step
sequences, retry policy) belongs in the orchestrator, not here, so that
swapping hardware vendors is a single-file change.
"""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from typing import Optional


class HarnessFailure(Exception):
    """Raised when the harness reports a wiring / electrical fault."""


class HarnessConnection(str, enum.Enum):
    """Snapshot of the wiring status reported by detect_wiring()."""
    OK = "ok"
    POWER_MISSING = "power_missing"
    GROUND_MISSING = "ground_missing"
    SHORT_TO_GROUND = "short_to_ground"
    SHORT_TO_POWER = "short_to_power"
    CAN_OPEN = "can_open"
    EEPROM_SDA_NC = "eeprom_sda_nc"     # data line not connected
    EEPROM_SCL_NC = "eeprom_scl_nc"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WiringReport:
    status: HarnessConnection
    voltage_v: float
    detail: str = ""


# ─────────────────────────────────────────────────────────────────────
class AbstractSmartHarness(abc.ABC):
    """Lifecycle: detect_wiring → power_on → enter_bench_mode → read/write
    → power_off. Every method is awaitable so production can run BLE or
    USB-CDC waits without blocking the asyncio loop."""

    @abc.abstractmethod
    async def detect_wiring(self, *, expected_pins: dict[str, int]
                            ) -> WiringReport:
        """Probe each expected pin and report continuity + voltage."""

    @abc.abstractmethod
    async def power_on(self, *, voltage_v: float,
                       tolerance_v: float = 0.5) -> float:
        """Ramp the 12 V rail. Returns the measured voltage. Raises
        HarnessFailure if the rail can't be brought into tolerance."""

    @abc.abstractmethod
    async def power_off(self) -> None: ...

    @abc.abstractmethod
    async def enter_bench_mode(self, *, hold_boot_pin: bool,
                               can_speed_kbps: int = 500) -> None:
        """Drive the BOOT pin (or short the K-line) so the target ECU
        comes up in its bootloader / bench mode instead of normal run mode."""

    # ── EEPROM (I²C) path ───────────────────────────────────────────
    @abc.abstractmethod
    async def i2c_read_eeprom(self, *, chip: str, size: int) -> bytes:
        """Read the entire EEPROM. The Smart Harness handles paging /
        device-address quirks for the chip family (M35080, M35128, ...)."""

    # ── CAN / UDS path ──────────────────────────────────────────────
    @abc.abstractmethod
    async def can_send_raw(self, *, arbitration_id: int, data: bytes) -> None:
        """Inject a raw CAN frame — used by the UDS layer to ride the bus."""

    @abc.abstractmethod
    async def can_recv_raw(self, *, timeout_s: float = 1.0) -> bytes: ...


# ─────────────────────────────────────────────────────────────────────
# Mock — pure-Python, deterministic. Tests configure it with an EEPROM
# dump (or a UDS reply queue) and drive the orchestrator without any
# real hardware in the loop.
# ─────────────────────────────────────────────────────────────────────
class MockSmartHarness(AbstractSmartHarness):
    """In-memory harness for unit tests + dry-run demos.

    Usage:
        h = MockSmartHarness(eeprom_payload=open("dump.bin","rb").read())
        h.simulated_voltage = 12.1
        # … pass into orchestrator …
    """

    def __init__(
        self,
        *,
        eeprom_payload: Optional[bytes] = None,
        simulated_voltage: float = 12.1,
        wiring_status: HarnessConnection = HarnessConnection.OK,
        power_on_voltage: Optional[float] = None,
    ) -> None:
        self.eeprom_payload = eeprom_payload or b""
        self.simulated_voltage = simulated_voltage
        self.wiring_status = wiring_status
        # When None the rail tracks `simulated_voltage` exactly; tests
        # use this to force a brown-out / over-voltage case.
        self._power_on_voltage = power_on_voltage

        # ── Audit trail tests can assert against ─────────────────
        self.detect_calls: list[dict[str, int]] = []
        self.power_calls: list[tuple[float, float]] = []
        self.bench_mode_calls: list[tuple[bool, int]] = []
        self.eeprom_reads: list[tuple[str, int]] = []
        self.power_state: str = "off"
        self.bench_mode_active: bool = False

        # CAN reply queue — tests preload UDS responses in order.
        self._can_rx_queue: list[bytes] = []
        self.can_tx_log: list[tuple[int, bytes]] = []

    # ── Tester helpers ──────────────────────────────────────────
    def queue_can_reply(self, frame: bytes) -> None:
        """Tests call this to enqueue the next CAN frame can_recv_raw will
        return."""
        self._can_rx_queue.append(frame)

    # ── ABC implementations ─────────────────────────────────────
    async def detect_wiring(self, *, expected_pins: dict[str, int]
                            ) -> WiringReport:
        self.detect_calls.append(dict(expected_pins))
        return WiringReport(
            status=self.wiring_status,
            voltage_v=self.simulated_voltage,
            detail="MockSmartHarness — synthetic reading",
        )

    async def power_on(self, *, voltage_v: float,
                       tolerance_v: float = 0.5) -> float:
        self.power_calls.append((voltage_v, tolerance_v))
        measured = (self._power_on_voltage if self._power_on_voltage is not None
                    else self.simulated_voltage)
        if abs(measured - voltage_v) > tolerance_v:
            raise HarnessFailure(
                f"power rail out of tolerance: target={voltage_v} V, "
                f"measured={measured} V, tol=±{tolerance_v} V",
            )
        self.power_state = "on"
        return measured

    async def power_off(self) -> None:
        self.power_state = "off"
        self.bench_mode_active = False

    async def enter_bench_mode(self, *, hold_boot_pin: bool,
                               can_speed_kbps: int = 500) -> None:
        self.bench_mode_calls.append((hold_boot_pin, can_speed_kbps))
        self.bench_mode_active = True

    async def i2c_read_eeprom(self, *, chip: str, size: int) -> bytes:
        self.eeprom_reads.append((chip, size))
        if not self.eeprom_payload:
            raise HarnessFailure(f"no EEPROM payload preloaded for chip {chip!r}")
        # Pad or truncate so tests can preload a smaller blob and still
        # exercise size-handling code in the orchestrator.
        if len(self.eeprom_payload) >= size:
            return self.eeprom_payload[:size]
        return self.eeprom_payload + bytes(size - len(self.eeprom_payload))

    async def can_send_raw(self, *, arbitration_id: int, data: bytes) -> None:
        self.can_tx_log.append((arbitration_id, data))

    async def can_recv_raw(self, *, timeout_s: float = 1.0) -> bytes:
        if not self._can_rx_queue:
            raise HarnessFailure("can_recv_raw: queue empty (test forgot to enqueue)")
        return self._can_rx_queue.pop(0)
