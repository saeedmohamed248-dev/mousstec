#!/usr/bin/env python3
"""Mousstec BMW/MINI field CLI — talk to the car over a CANable, no Django.

This runs next to the car with ONLY the transport deps — no database, no
Redis, no tenancy, no web server. It reuses the exact same tested logic the
web Coding Room uses (cable transport, FA reader, engine-swap diagnosis, the
catalog-gated FA transform), so a field test needs nothing but:

    pip install python-can can-isotp pyserial
    python -m bmw_ecu.scripts.field_cli ping   --port /dev/ttyACM0 --tx 0x6F1 --rx 0x612
    python -m bmw_ecu.scripts.field_cli read-fa
    python -m bmw_ecu.scripts.field_cli diagnose --engine N18 --bench
    python -m bmw_ecu.scripts.field_cli fa-plan  --to N18 --catalog fa_catalog.json
    python -m bmw_ecu.scripts.field_cli fa-write --to N18 --confirm --catalog fa_catalog.json

Cable settings come from --port/--tx/--rx (or the BMW_ECU_KDCAN_PORT /
BMW_ECU_CAN_TX_ID / BMW_ECU_CAN_RX_ID env vars). Use a CANable/slcan
adapter — the blue FTDI K+DCAN cable is NOT supported.

HONEST LIMITS: reading the ISN needs SecurityAccess/crypto that is not
shipped, so `diagnose` reads the FA (open, no crypto) and takes the ISNs as
hex flags if you already have them. FA writing is catalog-gated: it refuses
unless you registered the verified type code.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Optional

from ..connection.cable import CableConfigError, cable_config
from ..coding.fa_catalog import load_fa_catalog_from_file
from ..coding.fa_engine import engine_from_vo
from ..coding.fa_transform import UnverifiedFaTransform, plan_from_raw
from ..coding.fa_vo import parse_fa
from ..repair.swap_diagnosis import diagnose_engine_swap

_TESTER_PRESENT = b"\x3E\x00"


def _cfg_from_args(a) -> "object":
    port = a.port or os.environ.get("BMW_ECU_KDCAN_PORT")
    tx = a.tx or os.environ.get("BMW_ECU_CAN_TX_ID")
    rx = a.rx or os.environ.get("BMW_ECU_CAN_RX_ID")
    return cable_config(serial_port=port or "", can_tx_id=tx, can_rx_id=rx,
                        bitrate=a.bitrate)


async def _open(cfg):
    from ..connection.kdcan import KDCANTransport
    t = KDCANTransport(cfg)
    await t.open()
    return t


def _hex_isn(s: Optional[str]) -> Optional[bytes]:
    if not s:
        return None
    return bytes.fromhex(s.replace(" ", ""))


# ── commands ────────────────────────────────────────────────────────────
async def cmd_ping(a) -> int:
    cfg = _cfg_from_args(a)
    t = await _open(cfg)
    try:
        try:
            resp = await t.request(cfg.target_addr, _TESTER_PRESENT, timeout=1.5)
            ok = bool(resp) and resp[0] in (0x7E, 0x7F)
            print("✅ الكابل والعربية بيردّوا / cable + car responding"
                  if ok else
                  "🟠 الأدابتر فتح بس مفيش رد — راجع الـ CAN IDs والكونتاكت ON")
            return 0 if ok else 2
        except Exception as e:
            print(f"🟠 الأدابتر فتح بس مفيش رد ({e}) — راجع الـ CAN IDs / ignition ON")
            return 2
    finally:
        await t.close()


async def _read_fa(cfg) -> str:
    from ..coding.vo_parser import read_vo_from_vcm
    from ..uds.client import UdsClient
    t = await _open(cfg)
    try:
        client = UdsClient(t, ecu_addr=(cfg.can_rx_id or 0x40), session_name="fa")
        vo = await read_vo_from_vcm(client)
        return vo.raw or ""
    finally:
        await t.close()


async def cmd_read_fa(a) -> int:
    cfg = _cfg_from_args(a)
    raw = await _read_fa(cfg)
    if not raw:
        print("🟠 العربية مردّتش بالـ FA على الـ DIDs المعروفة — الصقه يدوي.")
        return 2
    vo = parse_fa(raw)
    print(f"FA raw : {raw}")
    print(f"type   : {vo.type_code}")
    print(f"engine : {engine_from_vo(vo) or '(غير محدّد من الـ FA)'}")
    print(f"options: {', '.join(sorted(vo.options))}")
    return 0


async def cmd_diagnose(a) -> int:
    # FA: read live unless pasted.
    fa_raw = a.fa_raw
    if not fa_raw:
        try:
            cfg = _cfg_from_args(a)
            fa_raw = await _read_fa(cfg)
        except Exception as e:
            print(f"(تعذّر قراية الـ FA حياً: {e})")
            fa_raw = ""
    fa_engine = engine_from_vo(parse_fa(fa_raw)) if fa_raw else ""

    diag = diagnose_engine_swap(
        cas_isn=_hex_isn(a.cas_isn),
        dme_isn=_hex_isn(a.dme_isn),
        fa_engine=fa_engine,
        dme_reported_engine=a.engine,
        dme_requires_bench=a.bench,
    )
    print("── التشخيص / diagnosis ─────────────────────────")
    print(diag.summary_ar)
    if fa_raw:
        print(f"FA: {fa_raw}  (engine {fa_engine or '?'})")
    for act in diag.actions:
        where = "🔧 بنش" if act.where == "bench" else "🔌 OBD"
        who = "🤖 البوت" if act.bot_can_do else "✋ يدوي"
        print(f"  • {act.title_ar}   [{where} · {who}]")
        print(f"      {act.detail_ar}")
    return 0


def _load_catalog(a) -> None:
    if a.catalog:
        from pathlib import Path
        n = load_fa_catalog_from_file(Path(a.catalog))
        print(f"(كتالوج: اتحمّل {n} عنصر من {a.catalog})")


async def cmd_fa_plan(a) -> int:
    _load_catalog(a)
    fa_raw = a.fa_raw or await _read_fa(_cfg_from_args(a))
    try:
        plan = plan_from_raw(fa_raw, to_engine=a.to_engine)
    except UnverifiedFaTransform as e:
        print(f"⛔ {e}\n   سجّل القيمة المتأكّدة الأول (register_fa_transform).")
        return 3
    print(f"FA update {plan.from_engine} → {plan.to_engine}")
    print(f"  type: {plan.old_type_code} → {plan.new_type_code}")
    print(f"  old : {plan.old_raw}")
    print(f"  new : {plan.new_raw}")
    if plan.added_options:   print(f"  +options: {', '.join(plan.added_options)}")
    if plan.removed_options: print(f"  -options: {', '.join(plan.removed_options)}")
    return 0


async def cmd_fa_write(a) -> int:
    _load_catalog(a)
    cfg = _cfg_from_args(a)
    fa_raw = a.fa_raw or await _read_fa(cfg)
    try:
        plan = plan_from_raw(fa_raw, to_engine=a.to_engine)
    except UnverifiedFaTransform as e:
        print(f"⛔ {e}")
        return 3
    if not a.confirm:
        print("الخطة دي هتتكتب — راجعها وضيف --confirm عشان تكتب:")
        print(f"  {plan.old_raw}\n  → {plan.new_raw}")
        return 4

    from ..coding.vo_parser import read_vo_from_vcm
    from ..uds.client import UdsClient
    t = await _open(cfg)
    try:
        client = UdsClient(t, ecu_addr=(cfg.can_rx_id or 0x40), session_name="fa-write")
        backup = await read_vo_from_vcm(client)
        print(f"backup FA: {backup.raw}")
        await client.write_data_by_identifier(0xF802, plan.new_raw.encode("ascii"))
        after = await read_vo_from_vcm(client)
        ok = plan.new_type_code.upper() in (after.raw or "").upper()
        print("✅ اتكتب واتأكد" if ok else "🟠 اتكتب بس التأكيد مطابقش")
        return 0 if ok else 2
    finally:
        await t.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="field_cli",
        description="Mousstec BMW/MINI field CLI (CANable, no Django)")
    p.add_argument("--port", help="CANable serial device (or BMW_ECU_KDCAN_PORT)")
    p.add_argument("--tx", help="tester→ECU CAN id, e.g. 0x6F1 (or env)")
    p.add_argument("--rx", help="ECU→tester CAN id, e.g. 0x612 (or env)")
    p.add_argument("--bitrate", type=int, default=500000)
    p.add_argument("--fa-raw", default="", help="paste the FA instead of reading it")
    p.add_argument("--catalog", default="", help="FA catalog JSON path")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="open the cable + Tester-Present")
    sub.add_parser("read-fa", help="read + show the FA")

    d = sub.add_parser("diagnose", help="engine-swap diagnosis")
    d.add_argument("--engine", required=True, help="fitted DME engine, e.g. N18")
    d.add_argument("--bench", action="store_true",
                   help="the DME's ISN needs bench (true for MEVD17/N18)")
    d.add_argument("--cas-isn", help="CAS ISN hex (if you already have it)")
    d.add_argument("--dme-isn", help="DME ISN hex (if you already have it)")

    fp = sub.add_parser("fa-plan", help="preview an FA engine change")
    fp.add_argument("--to", dest="to_engine", required=True)

    fw = sub.add_parser("fa-write", help="write an FA engine change")
    fw.add_argument("--to", dest="to_engine", required=True)
    fw.add_argument("--confirm", action="store_true")
    return p


_COMMANDS = {
    "ping": cmd_ping, "read-fa": cmd_read_fa, "diagnose": cmd_diagnose,
    "fa-plan": cmd_fa_plan, "fa-write": cmd_fa_write,
}


def main(argv: Optional[list[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return asyncio.run(_COMMANDS[a.cmd](a))
    except CableConfigError as e:
        print(f"⛔ إعداد الكابل ناقص: {e}", file=sys.stderr)
        print("   لازم --port و --tx و --rx (أو الـ env vars).", file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
