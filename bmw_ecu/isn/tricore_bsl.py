"""Tricore BSL (Bootstrap Loader) link — the no-external-device ISN fallback.

When a used MEVD17 DME rejects the ISN write over UDS, the only path that needs
NO external bench programmer (Xprog/KESS) is the chip's own **bootstrap loader**
reached over the FTDI serial line: open the DME, pull the boot-config pin, power
the board on the bench, and the TriCore enters BSL and answers a handshake.

WHAT THIS MODULE DOES FOR REAL (safe, non-destructive):
    • 25 ms fast-init break pulse on the serial line,
    • send the BSL trigger byte and verify the chip's acknowledge (0x55),
  i.e. it PROVES the physical boot setup is correct before anything is written.

WHAT IT DELIBERATELY REFUSES (brick risk — confirmed data only):
    • reading/writing the ISN region. That needs (a) the exact ISN flash OFFSET
      and (b) the chip-specific TriCore BSL flash-command sequence. Both are
      per-board confirmed data. Issuing a guessed flash command, or writing to a
      guessed offset, can permanently brick the DME — so without a confirmed
      ``BslFlashProfile`` we raise ``BslNotConfigured`` rather than improvise.

NOTHING about the boot pad, the chip die (TC1796 vs TC1797), the resistor value,
or the serial pin map is asserted as fact here: those live in ``BslHardwareProfile``
as operator-supplied, ``verified``-flagged values, defaulting to clearly-marked
"confirm from your board catalog" placeholders.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ..logging_setup import get_logger

log = get_logger(__name__)

# TriCore BSL acknowledge byte (documented bootstrap handshake). Configurable
# per profile in case a variant differs — never silently assumed elsewhere.
DEFAULT_BSL_ACK = 0x55


class BslError(RuntimeError):
    """Base for any BSL-link failure."""


class BslHandshakeFailed(BslError):
    """The chip did not answer the boot handshake → physical setup is wrong
    (boot pin not pulled, serial pins miswired, no bench power). Recoverable:
    fix the wiring and retry."""


class BslNotConfigured(BslError):
    """Boot link is up, but the confirmed ISN offset / BSL flash-command
    profile is missing, so we will not read or write a guessed flash region."""


@dataclass(frozen=True)
class BslFlashProfile:
    """Confirmed, per-board flash access for the ISN region. Supplied by the
    operator (admin/catalog) once verified on the actual DME board. Absent ⇒
    read/write refuse. We never default an offset or a command sequence."""
    isn_offset: int
    isn_length: int = 32
    # The confirmed TriCore BSL command bytes to read / program the region.
    # Stored as raw bytes the operator confirmed; this module does not invent
    # them. (Kept opaque on purpose — the licensed/confirmed tool defines them.)
    read_cmd: bytes = b""
    write_cmd: bytes = b""


@dataclass(frozen=True)
class BslHardwareProfile:
    """Physical setup for entering BSL on a given DME family. Every hardware
    specific is operator-supplied and ``verified``-flagged — the wizard shows a
    warning until confirmed, because a wrong boot pad bricks the ECU."""
    dme_family: str
    chip: str                      # e.g. "Infineon TriCore (confirm TC1796/TC1797)"
    boot_pin_label: str            # e.g. "Boot-config pad (confirm from catalog)"
    pull: str                      # e.g. "1kΩ to GND"
    serial_pin_map: str            # e.g. "FTDI TX→RX pad, RX→TX pad, GND→GND"
    bench_voltage: str = "12V"
    bsl_ack_byte: int = DEFAULT_BSL_ACK
    baudrate: int = 9600           # BSL entry baud — confirm per chip
    verified: bool = False         # True only when confirmed on real hardware
    flash: Optional[BslFlashProfile] = None


# Starter registry. The MEVD17 entry carries the user's STATED method as a
# template — deliberately verified=False and with NO flash offset, so the
# physical wizard + handshake run, but any ISN read/write refuses until the
# confirmed offset + BSL command profile are registered (admin / catalog).
_BSL_PROFILES: dict[str, BslHardwareProfile] = {
    "MEVD17": BslHardwareProfile(
        dme_family="MEVD17",
        chip="Infineon TriCore (stated TC1797 — confirm TC1796/TC1797 per board)",
        boot_pin_label="TriCore boot-config pin (confirm exact pad from catalog)",
        pull="1kΩ to GND (stated — confirm)",
        serial_pin_map="FTDI TX→board RX, FTDI RX→board TX, GND→GND (confirm pads)",
        bench_voltage="12V",
        verified=False,
        flash=None,
    ),
}


def get_bsl_profile(dme_family: str) -> Optional[BslHardwareProfile]:
    return _BSL_PROFILES.get((dme_family or "").upper())


def register_bsl_profile(profile: BslHardwareProfile) -> None:
    _BSL_PROFILES[profile.dme_family.upper()] = profile


class TricoreBslLink:
    """Serial BSL link over the FTDI cable. Does the safe handshake for real;
    refuses ISN flash ops without a confirmed ``BslFlashProfile``.

    ``serial_factory`` is injectable for tests; production lazily imports
    pyserial and opens ``port`` at the profile's BSL baud.
    """

    def __init__(self, *, port: str, profile: BslHardwareProfile,
                 serial_factory: Optional[Callable[[], object]] = None) -> None:
        self._port = port
        self._profile = profile
        self._serial_factory = serial_factory
        self._ser: Optional[object] = None

    # -- open / close ------------------------------------------------------
    def _open_serial(self) -> object:
        if self._serial_factory is not None:
            return self._serial_factory()
        try:
            import serial  # type: ignore
        except ImportError as e:  # pragma: no cover - env without pyserial
            raise BslError("pyserial not installed (pip install pyserial)") from e
        return serial.Serial(port=self._port, baudrate=self._profile.baudrate,
                             timeout=1.0, write_timeout=1.0)

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        self._ser = await loop.run_in_executor(None, self._open_serial)

    async def close(self) -> None:
        if self._ser is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, getattr(self._ser, "close", lambda: None))
        self._ser = None

    # -- the SAFE part: prove the boot handshake ---------------------------
    async def handshake(self) -> None:
        """25 ms fast-init + BSL trigger, verify the chip's ack byte (0x55).

        Non-destructive: it only confirms the TriCore is in bootstrap mode and
        the serial wiring is correct. Raises BslHandshakeFailed otherwise."""
        if self._ser is None:
            await self.open()
        loop = asyncio.get_running_loop()

        def _do() -> int:
            ser = self._ser
            # Fast-init: drive break low 25 ms, idle high 25 ms (same timing as
            # the K-Line fast-init; latency-sensitive over USB-FTDI).
            try:
                ser.reset_input_buffer()  # type: ignore[attr-defined]
                ser.break_condition = True  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - some fakes lack break
                pass
            time.sleep(0.025)
            try:
                ser.break_condition = False  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                pass
            time.sleep(0.025)
            # BSL entry: send 0x00 at the boot baud; TriCore answers the ack.
            ser.write(bytes([0x00]))  # type: ignore[attr-defined]
            ser.flush()               # type: ignore[attr-defined]
            ack = ser.read(1)          # type: ignore[attr-defined]
            return ack[0] if ack else -1

        ack = await loop.run_in_executor(None, _do)
        if ack != self._profile.bsl_ack_byte:
            raise BslHandshakeFailed(
                f"BSL handshake failed: expected ack 0x{self._profile.bsl_ack_byte:02X}, "
                f"got {('0x%02X' % ack) if ack >= 0 else 'no answer'}. Check the "
                "boot pin pull, the FTDI serial wiring, and 12V bench power.")
        log.info("Tricore BSL handshake OK", extra={"family": self._profile.dme_family})

    # -- the GATED part: ISN flash ops (confirmed data only) ---------------
    def _require_flash(self) -> BslFlashProfile:
        flash = self._profile.flash
        if flash is None or not flash.read_cmd or not flash.write_cmd:
            raise BslNotConfigured(
                "Boot link is up, but no confirmed ISN flash profile is "
                "registered for this DME (offset + TriCore BSL command "
                "sequence). Register it (admin/catalog) — refusing to issue a "
                "guessed flash command that could brick the DME.")
        return flash

    async def read_isn(self) -> bytes:
        flash = self._require_flash()
        # With a confirmed profile this issues flash.read_cmd at flash.isn_offset.
        # Intentionally not implemented against guessed commands.
        raise BslNotConfigured(
            "ISN read over BSL needs the confirmed command execution wired to "
            f"the verified profile (offset 0x{flash.isn_offset:X}).")

    async def write_isn(self, isn: bytes) -> None:
        flash = self._require_flash()
        if len(isn) != flash.isn_length:
            raise BslError(
                f"ISN must be {flash.isn_length} bytes, got {len(isn)}.")
        raise BslNotConfigured(
            "ISN write over BSL needs the confirmed command execution wired to "
            f"the verified profile (offset 0x{flash.isn_offset:X}).")
