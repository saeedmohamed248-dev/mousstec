"""Persistent FA catalog — verified type→engine + engine transforms.

The in-memory registries in `fa_engine` / `fa_transform` are wiped on every
restart, which is fine for tests but useless in production. This module
persists the VERIFIED values in a JSON file the workshop owns, and loads
them into the registries at app startup (`BmwEcuConfig.ready`).

Catalog shape:
    {
      "type_code_engine": { "3F30": "N14", "3R18": "N18" },
      "engine_transforms": [
        { "from": "N14", "to": "N18", "new_type_code": "3R18",
          "add_options": ["S1CBA"], "remove_options": ["S205A"],
          "note": "verified from a genuine R56 N18 FA" }
      ]
    }

Nothing is seeded from memory — the file ships absent, and the loader is a
no-op until the workshop fills it with values read from a real car. That is
the whole safety contract: a wrong type code mis-codes the car.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .fa_engine import register_type_engine
from .fa_transform import register_fa_engine_transform


def default_catalog_path() -> Path:
    """Env override, else <app>/data/fa_catalog.json."""
    env = os.environ.get("BMW_ECU_FA_CATALOG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "fa_catalog.json"


def load_fa_catalog_from_dict(data: dict[str, Any]) -> int:
    """Register every entry in `data`. Returns how many were registered.
    Malformed individual entries are skipped (logged by the caller)."""
    count = 0
    for tc, engine in (data.get("type_code_engine") or {}).items():
        if tc and engine:
            register_type_engine(str(tc), str(engine))
            count += 1
    for tf in (data.get("engine_transforms") or []):
        try:
            register_fa_engine_transform(
                tf["from"], tf["to"],
                new_type_code=tf["new_type_code"],
                add_options=tf.get("add_options", ()) or (),
                remove_options=tf.get("remove_options", ()) or (),
                note=tf.get("note", "") or "",
            )
            count += 1
        except (KeyError, ValueError):
            continue
    return count


def load_fa_catalog_from_file(path: Path | None = None) -> int:
    """Load the catalog JSON if it exists. Returns entries registered (0 if
    the file is absent or unreadable — never raises at startup)."""
    path = path or default_catalog_path()
    try:
        if not path.exists():
            return 0
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    return load_fa_catalog_from_dict(data)


def save_engine_transform(path: Path, *, from_engine: str, to_engine: str,
                          new_type_code: str,
                          from_type_code: str = "",
                          add_options: list[str] | None = None,
                          remove_options: list[str] | None = None,
                          note: str = "") -> None:
    """Read-modify-write the catalog JSON with one verified transform.

    Also records `from_type_code → from_engine` (when given) so the source
    engine is derivable from a car whose FA carries no explicit engine token.
    Validates by registering into the live registry before persisting.
    """
    # Validate first — raises if new_type_code is empty, engines missing, etc.
    register_fa_engine_transform(
        from_engine, to_engine, new_type_code=new_type_code,
        add_options=add_options or (), remove_options=remove_options or (),
        note=note)

    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except ValueError:
            data = {}

    tce = data.setdefault("type_code_engine", {})
    if from_type_code:
        tce[from_type_code.strip().upper()] = from_engine.strip().upper()
    tce[new_type_code.strip().upper()] = to_engine.strip().upper()

    transforms = data.setdefault("engine_transforms", [])
    key = (from_engine.strip().upper(), to_engine.strip().upper())
    # Replace an existing entry for the same (from,to) pair.
    transforms = [t for t in transforms
                  if (str(t.get("from", "")).upper(),
                      str(t.get("to", "")).upper()) != key]
    transforms.append({
        "from": key[0], "to": key[1],
        "new_type_code": new_type_code.strip().upper(),
        "add_options": [o.strip().upper() for o in (add_options or [])],
        "remove_options": [o.strip().upper() for o in (remove_options or [])],
        "note": note,
    })
    data["engine_transforms"] = transforms

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8")
