#!/usr/bin/env python3
"""
ELM327 Wi-Fi → WebSocket bridge for the Mouss Tec Diagnostics Room.

The browser cannot open raw TCP sockets. This tiny script runs on the
mechanic's laptop, opens a TCP connection to the ELM327 Wi-Fi dongle,
and exposes it as a WebSocket on localhost. The Diagnostics Room
connects to that WebSocket and speaks ELM327 AT commands as if it were
talking over Bluetooth.

═════════════════════════════════════════════════════════════════════
USAGE  (one-time setup per laptop)
═════════════════════════════════════════════════════════════════════

  1. Install dependencies:
        pip install websockets

  2. On the laptop, join the ELM327 Wi-Fi network
     (usually "WiFi_OBDII", "V-LINK", or similar).
     The internet will be unreachable while connected — that is normal.

  3. Run the bridge:
        python obd_wifi_bridge.py

     Optional flags:
        --dongle 192.168.0.10:35000   ELM327 TCP endpoint
        --listen 127.0.0.1:8765       WebSocket bind address

  4. Open the Diagnostics Room in the browser and click
     "اتصل عبر Wi-Fi (الجسر المحلي)".

═════════════════════════════════════════════════════════════════════
"""
import argparse
import asyncio
import logging
import sys

try:
    import websockets
except ImportError:
    sys.stderr.write(
        "FATAL: the 'websockets' package is not installed.\n"
        "Run:    pip install websockets\n"
    )
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("obd-wifi-bridge")


async def _pump_ws_to_tcp(ws, writer):
    async for message in ws:
        data = message.encode("ascii", errors="ignore") if isinstance(message, str) else message
        if not data:
            continue
        writer.write(data)
        await writer.drain()


async def _pump_tcp_to_ws(reader, ws):
    while True:
        chunk = await reader.read(256)
        if not chunk:                       # TCP half-closed
            return
        try:
            await ws.send(chunk.decode("ascii", errors="ignore"))
        except websockets.ConnectionClosed:
            return


async def handle_session(ws, dongle_host, dongle_port):
    peer = getattr(ws, "remote_address", ("?", "?"))
    log.info("Browser connected from %s:%s — opening TCP to %s:%s",
             peer[0], peer[1], dongle_host, dongle_port)

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(dongle_host, dongle_port),
            timeout=5.0,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        msg = f"could not reach ELM327 at {dongle_host}:{dongle_port} — {exc}"
        log.error(msg)
        # Emit a fake ELM "prompt" so the JS sees a synthetic error reply
        # instead of timing out.
        try:
            await ws.send(f"BRIDGE_ERROR: {msg}\r>")
        except websockets.ConnectionClosed:
            pass
        await ws.close()
        return

    log.info("TCP up — pumping bytes both ways")
    try:
        await asyncio.gather(
            _pump_ws_to_tcp(ws, writer),
            _pump_tcp_to_ws(reader, ws),
        )
    except websockets.ConnectionClosed:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        log.info("Session closed — bridge idle")


async def main(args):
    log.info("ELM327 Wi-Fi bridge")
    log.info("  • WebSocket listen : ws://%s:%s", args.listen_host, args.listen_port)
    log.info("  • Dongle target    : tcp://%s:%s", args.dongle_host, args.dongle_port)
    log.info("Open the Diagnostics Room and press 'اتصل عبر Wi-Fi'.")
    log.info("Ctrl+C to stop.")

    async with websockets.serve(
        lambda ws: handle_session(ws, args.dongle_host, args.dongle_port),
        args.listen_host,
        args.listen_port,
    ):
        await asyncio.Future()              # run forever


def _parse_host_port(spec, default_port):
    if ":" in spec:
        host, port = spec.rsplit(":", 1)
        return host, int(port)
    return spec, default_port


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dongle", default="192.168.0.10:35000",
        help="ELM327 TCP endpoint (default 192.168.0.10:35000)",
    )
    parser.add_argument(
        "--listen", default="127.0.0.1:8765",
        help="WebSocket bind (default 127.0.0.1:8765)",
    )
    cli = parser.parse_args()
    cli.dongle_host, cli.dongle_port = _parse_host_port(cli.dongle, 35000)
    cli.listen_host, cli.listen_port = _parse_host_port(cli.listen, 8765)

    try:
        asyncio.run(main(cli))
    except KeyboardInterrupt:
        log.info("Bye")
