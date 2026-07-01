"""Non-destructive K-Line (KWP2000) target-address scanner.

WHY THIS EXISTS
    On a pre-2007 E-series bench bridge (e.g. a JBBFE gateway wired FTDI K-Line
    pin 7 -> Diagnostic TXD -> PT-CAN -> CAS/DME), the ONE thing you must know
    before anything else is: which KWP2000 target address answers a
    StartCommunication on this specific wiring? BMW never published a single
    universal number for a home-brew bridge, and Mouss Tec NEVER guesses against
    live hardware. So instead of inventing a value for BMW_ECU_KLINE_TARGET, this
    command empirically finds it.

WHAT IT DOES (and only this)
    For each candidate target address it performs exactly ONE ISO 14230 fast-init
    StartCommunication handshake:

        25ms break-low + 25ms idle-high  ->  request SID 0x81  ->  expect 0xC1

    That handshake opens a KWP diagnostic session and NOTHING else. It writes no
    data, touches no flash, reads no ISN, sends no SecurityAccess. It is the
    safest possible probe — the same first frame the real transport already
    sends on every connect. An address that replies 0xC1 is the one to put in
    BMW_ECU_KLINE_TARGET.

SAFETY / RESOURCE NOTE
    KLineTransport.open() leaves its serial handle unclosed if _fast_init raises
    (the local `ser` is never assigned to self._ser), which would leak a handle
    per failed probe and can wedge sequential opens on the same port. So this
    command owns its own serial.Serial handle and always closes it in a finally
    block, reusing the production KLineTransport._fast_init for byte-identical
    timing and framing.

USAGE
    python manage.py bmw_kline_scan                 # scan default candidates
    python manage.py bmw_kline_scan --targets 0x40,0x12,0x10,0x00
    python manage.py bmw_kline_scan --sweep         # brute 0x00..0xFF
    python manage.py bmw_kline_scan --port /dev/cu.usbserial-A50285BI --timeout 1.5
"""
from __future__ import annotations

import asyncio
import os

from django.core.management.base import BaseCommand, CommandError

from ...connection.base import TransportConfig, TransportKind
from ...connection.kline import KLineTransport

# Sensible, non-guessed default probe set: the user's confirmed CAS (0x40) and
# DME (0x12) diagnostic addresses, plus the usual E-series gateway candidates.
# These are *candidates to test*, never asserted as correct — the scan decides.
_DEFAULT_TARGETS = [0x40, 0x12, 0x10, 0x00]


class Command(BaseCommand):
    help = (
        "Non-destructive K-Line/KWP2000 target scan: tries a StartCommunication "
        "fast-init handshake (0x81 -> expect 0xC1) against candidate KWP target "
        "addresses and reports which answers. No writes, no ISN, no flash access."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--port",
            default=os.environ.get("BMW_ECU_KLINE_PORT"),
            help="FTDI serial device (default: $BMW_ECU_KLINE_PORT).",
        )
        parser.add_argument(
            "--targets",
            default=None,
            help="Comma-separated target addresses to probe (hex 0x.. or "
            "decimal). Default: 0x40,0x12,0x10,0x00.",
        )
        parser.add_argument(
            "--sweep",
            action="store_true",
            help="Brute-force every address 0x00..0xFF (overrides --targets). "
            "Slow but exhaustive.",
        )
        parser.add_argument(
            "--source",
            default=os.environ.get("BMW_ECU_KLINE_SOURCE", "0xF1"),
            help="Tester KWP source address (default: 0xF1 / $BMW_ECU_KLINE_SOURCE).",
        )
        parser.add_argument(
            "--baud",
            type=int,
            default=int(os.environ.get("BMW_ECU_KLINE_BAUD", "10400")),
            help="KWP2000 baud rate (default: 10400).",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=1.5,
            help="Per-probe read timeout in seconds (default: 1.5).",
        )
        parser.add_argument(
            "--first",
            action="store_true",
            help="Stop at the first address that answers 0xC1.",
        )

    def handle(self, *args, **opts):
        port = opts["port"]
        if not port:
            raise CommandError(
                "No serial port. Pass --port or set BMW_ECU_KLINE_PORT "
                "(e.g. /dev/cu.usbserial-A50285BI)."
            )

        try:
            source = int(str(opts["source"]).strip(), 0)
        except ValueError as e:
            raise CommandError(f"Bad --source address: {opts['source']!r}") from e

        if opts["sweep"]:
            targets = list(range(0x00, 0x100))
        elif opts["targets"]:
            targets = self._parse_targets(opts["targets"])
        else:
            targets = list(_DEFAULT_TARGETS)

        baud = opts["baud"]
        timeout = opts["timeout"]

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"K-Line target scan on {port} @ {baud} baud "
                f"(source 0x{source:02X}, timeout {timeout}s)"
            )
        )
        self.stdout.write(
            "Probe = ISO 14230 fast-init StartCommunication (0x81), expecting "
            "0xC1. Read-only: no writes, no ISN, no flash.\n"
        )

        hits = asyncio.run(self._scan(port, targets, source, baud, timeout, opts["first"]))

        self.stdout.write("")
        if hits:
            self.stdout.write(self.style.SUCCESS(
                f"{len(hits)} address(es) answered StartCommunication:"))
            for addr in hits:
                self.stdout.write(self.style.SUCCESS(f"    -> 0x{addr:02X}"))
            best = hits[0]
            self.stdout.write("")
            self.stdout.write(
                "Put the answering address in your .env and restart the server:")
            self.stdout.write(self.style.HTTP_INFO(
                f"    BMW_ECU_KLINE_TARGET=0x{best:02X}"))
        else:
            self.stdout.write(self.style.ERROR(
                "No address answered. Check: cable/port, ignition ON, JBBFE "
                "power + PT-CAN wiring, and that this bench really speaks K-Line "
                "on pin 7 (post-03/2007 ZGW is D-CAN and will never answer here)."
            ))

    @staticmethod
    def _parse_targets(raw: str):
        out = []
        for tok in raw.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                val = int(tok, 0)
            except ValueError as e:
                raise CommandError(f"Bad target address: {tok!r}") from e
            if not (0x00 <= val <= 0xFF):
                raise CommandError(
                    f"Target 0x{val:X} out of KWP address range 0x00..0xFF.")
            out.append(val)
        if not out:
            raise CommandError("--targets was empty.")
        return out

    async def _scan(self, port, targets, source, baud, timeout, stop_first):
        hits = []
        for addr in targets:
            ok, detail = await self._probe(port, addr, source, baud, timeout)
            if ok:
                self.stdout.write(self.style.SUCCESS(
                    f"  0x{addr:02X}  ANSWERED (0xC1)  {detail}"))
                hits.append(addr)
                if stop_first:
                    break
            else:
                self.stdout.write(f"  0x{addr:02X}  no response   {detail}")
        return hits

    async def _probe(self, port, target, source, baud, timeout):
        """One non-destructive StartCommunication against `target`.

        Owns its own serial handle and always closes it, sidestepping the
        KLineTransport.open() leak when _fast_init raises.
        """
        try:
            import serial  # type: ignore  # pyserial
        except ImportError as e:  # pragma: no cover - env dependent
            raise CommandError(
                "pyserial not installed. Run: pip install pyserial") from e

        cfg = TransportConfig(
            kind=TransportKind.KLINE,
            serial_port=port,
            kline_target_addr=target,
            kline_source_addr=source,
            kline_baudrate=baud,
            kline_fast_init=True,
            timeout=timeout,
        )
        transport = KLineTransport(cfg)

        loop = asyncio.get_running_loop()

        def _do() -> str:
            ser = serial.Serial(
                port=port,
                baudrate=baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=timeout,
                write_timeout=timeout,
            )
            try:
                # Reuse the production handshake for byte-identical timing/framing.
                transport._fast_init(ser, serial)
                return ""
            finally:
                try:
                    ser.close()
                except Exception:
                    pass

        try:
            await loop.run_in_executor(None, _do)
            return True, ""
        except Exception as e:  # noqa: BLE001 - probe: any failure = "no answer"
            return False, f"({type(e).__name__}: {e})"
