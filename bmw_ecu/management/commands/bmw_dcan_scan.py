"""Read-only D-CAN scan through the FTDI K+DCAN cable (MINI R56 / post-2007 E).

WHAT IT DOES (and only this)
    Opens the cable once (switch on D-CAN, 115200 baud) and, for each candidate
    ECU address, sends READ requests only:

        1A 80        BMW ident record   → the module is present + part/SW numbers
        1A 90        VIN (if the ECU supports it)
        18 02 FF FF  fault memory       → stored fault codes + status

    No coding, no flash, no SecurityAccess, no clearing. It's the first thing to
    run on a new car/cable: it proves the cable really talks D-CAN and shows
    which modules (FRM, CAS, DME, ...) answer and what faults they hold.

USAGE
    python manage.py bmw_dcan_scan --port /dev/cu.usbserial-A50285BI
    python manage.py bmw_dcan_scan --port COM3 --targets 0x72,0x40
    python manage.py bmw_dcan_scan --sweep          # every address 0x00..0xFF
    python manage.py bmw_dcan_scan --json           # machine-readable report
"""
from __future__ import annotations

import asyncio
import json
import os

from django.core.management.base import BaseCommand, CommandError

from ...connection.base import TransportConfig, TransportKind
from ...connection.dcan_cable import DCanCableTransport
from ...exceptions import ConnectionError_, TransportTimeout, UdsNegativeResponse
from ...scan import kwp_eseries
from ...scan.module_map import MODULE_CATALOG
from ...uds.client import UdsClient

_BY_ADDRESS = {}
for _m in MODULE_CATALOG.values():
    _BY_ADDRESS.setdefault(_m.address, _m)


def module_label(addr: int) -> str:
    m = _BY_ADDRESS.get(addr)
    return m.name_ar if m else "موديول غير معروف"


class Command(BaseCommand):
    help = (
        "Read-only D-CAN scan via the FTDI K+DCAN cable (switch on D-CAN): which "
        "ECUs answer, their ident/VIN and stored fault codes. No writes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--port", default=os.environ.get("BMW_ECU_DCAN_CABLE_PORT"),
            help="Cable serial device (default: $BMW_ECU_DCAN_CABLE_PORT).")
        parser.add_argument(
            "--targets", default=None,
            help="Comma-separated ECU addresses (hex 0x.. or decimal). "
                 "Default: the MINI R56 candidate set (FRM 0x72, CAS 0x40, ...).")
        parser.add_argument(
            "--sweep", action="store_true",
            help="Probe every address 0x00..0xFF (slow, overrides --targets).")
        parser.add_argument(
            "--baud", type=int,
            default=int(os.environ.get("BMW_ECU_DCAN_CABLE_BAUD", "115200")),
            help="Cable serial baud (default: 115200).")
        parser.add_argument(
            "--timeout", type=float, default=1.5,
            help="Per-request timeout in seconds (default: 1.5).")
        parser.add_argument(
            "--no-dtc", action="store_true", help="Skip reading fault memory.")
        parser.add_argument(
            "--json", action="store_true", help="Print the report as JSON.")

    def handle(self, *args, **opts):
        port = opts["port"]
        if not port:
            raise CommandError(
                "No serial port. Pass --port or set BMW_ECU_DCAN_CABLE_PORT "
                "(e.g. /dev/cu.usbserial-A50285BI or COM3).")

        if opts["sweep"]:
            targets = list(range(0x00, 0x100))
        elif opts["targets"]:
            targets = self._parse_targets(opts["targets"])
        else:
            targets = [m.address for m in kwp_eseries.r56_candidates()]

        cfg = TransportConfig(
            kind=TransportKind.DCAN_CABLE, serial_port=port,
            dcan_cable_baudrate=opts["baud"], timeout=opts["timeout"])
        try:
            report = asyncio.run(self.scan(
                DCanCableTransport(cfg), targets,
                timeout=opts["timeout"], read_dtc=not opts["no_dtc"],
                quiet=opts["json"]))
        except ConnectionError_ as e:
            raise CommandError(
                f"الكابل ماتفتحش ({e}). اتأكد إن الكابل متركّب وإن اسم البورت صح.") from e

        if opts["json"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            self._print_summary(report)

    @staticmethod
    def _parse_targets(raw: str) -> list[int]:
        out = []
        for tok in raw.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                val = int(tok, 0)
            except ValueError as e:
                raise CommandError(f"Bad target address: {tok!r}") from e
            if not 0x00 <= val <= 0xFF:
                raise CommandError(f"Target 0x{val:X} out of range 0x00..0xFF.")
            out.append(val)
        if not out:
            raise CommandError("--targets was empty.")
        return out

    async def scan(self, transport, targets, *, timeout, read_dtc, quiet=False) -> dict:
        """Probe each address over one open cable. Returns a JSON-able report."""
        modules = []
        await transport.open()
        try:
            for addr in targets:
                entry = await self._probe(transport, addr, timeout, read_dtc)
                if entry is None:
                    if not quiet:
                        self.stdout.write(f"  0x{addr:02X}  مفيش رد       {module_label(addr)}")
                    continue
                modules.append(entry)
                if not quiet:
                    self.stdout.write(self.style.SUCCESS(
                        f"  0x{addr:02X}  رد ✓          {entry['name_ar']}"))
        finally:
            await transport.close()
        vins = sorted({m["vin"] for m in modules if m.get("vin")})
        return {"port": transport.config.serial_port, "modules": modules, "vins": vins}

    async def _probe(self, transport, addr, timeout, read_dtc):
        client = UdsClient(transport, ecu_addr=addr, session_name="dcan_scan")
        entry = {"address": f"0x{addr:02X}", "name_ar": module_label(addr)}
        try:
            ident = await kwp_eseries.read_ident(client, timeout=timeout)
            entry["ident"] = ident.to_dict()
        except TransportTimeout:
            return None
        except UdsNegativeResponse as e:
            # It answered (just refused this ident) — the module is on the bus.
            entry["ident_error"] = str(e)
        except ValueError as e:
            entry["ident_error"] = str(e)

        try:
            entry["vin"] = await kwp_eseries.read_vin(client, timeout=timeout)
        except (TransportTimeout, ValueError):
            entry["vin"] = None

        if read_dtc:
            try:
                entry["dtcs"] = [d.to_dict() for d in
                                 await kwp_eseries.read_dtcs(client, timeout=timeout)]
            except (TransportTimeout, UdsNegativeResponse, ValueError) as e:
                entry["dtc_error"] = str(e)
        return entry

    def _print_summary(self, report: dict) -> None:
        mods = report["modules"]
        self.stdout.write("")
        if not mods:
            self.stdout.write(self.style.ERROR(
                "ولا موديول رد. راجع: زرار الكابل على D-CAN، الكونتاكت شغال، "
                "واسم البورت صح. لو لسه مفيش رد، جرّب --sweep."))
            return
        self.stdout.write(self.style.MIGRATE_HEADING(f"{len(mods)} موديول رد:"))
        for m in mods:
            line = f"  {m['address']}  {m['name_ar']}"
            if m.get("ident", {}).get("strings"):
                line += "  | " + " ".join(m["ident"]["strings"][:3])
            self.stdout.write(line)
            if m.get("vin"):
                self.stdout.write(f"        رقم الشاسيه: {m['vin']}")
            if "dtcs" in m:
                if m["dtcs"]:
                    codes = ", ".join(f"{d['code']}({d['status']})" for d in m["dtcs"])
                    self.stdout.write(self.style.WARNING(
                        f"        أعطال ({len(m['dtcs'])}): {codes}"))
                else:
                    self.stdout.write("        مفيش أعطال متسجلة")
            elif m.get("dtc_error"):
                self.stdout.write(f"        ماقدرناش نقرا الأعطال: {m['dtc_error']}")
        if report["vins"]:
            self.stdout.write("")
            self.stdout.write("رقم الشاسيه: " + ", ".join(report["vins"]))
