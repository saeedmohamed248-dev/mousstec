"""
erp_client.py — the kiosk's link to the real Mouss Tec ERP.

When `MOUSS_ERP_API` (e.g. https://<workshop>.mousstec.com/api/robot/v1) and
`MOUSS_ROBOT_TOKEN` (a device token from `create_robot_device`) are set, the
kiosk's tools answer from the live ERP through its robot API — retail prices
only, real stock and shelf, real invoices. Otherwise `database.py` keeps using
its mock tables so the demo runs anywhere.

If the ERP is configured but unreachable we say so ({"found": False,
"error": "erp_unavailable"}) instead of falling back to mock data: a real
customer must never be told about made-up stock or a made-up invoice.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = os.getenv("MOUSS_ERP_API", "").rstrip("/")
TOKEN = os.getenv("MOUSS_ROBOT_TOKEN", "")
TIMEOUT = float(os.getenv("MOUSS_ERP_TIMEOUT", "8"))


def enabled() -> bool:
    return bool(API and TOKEN)


def get(path: str, **params) -> dict:
    """GET `path` on the robot API; {"found": False, "error": ...} on failure."""
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    url = f"{API}/{path.strip('/')}/" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"X-Robot-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"found": False, "error": "erp_unavailable", "detail": str(exc)[:200]}
