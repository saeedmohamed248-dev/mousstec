"""Structured logging helpers.

We don't reconfigure Django's root logger — we just expose `get_logger()`
that returns a child of `bmw_ecu.*` and a `bind()` adapter that attaches
session/ECU context to every record.
"""
from __future__ import annotations

import logging
from typing import Any

_BASE = "bmw_ecu"


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger. `name` should be the module __name__."""
    if not name.startswith(_BASE):
        name = f"{_BASE}.{name}"
    return logging.getLogger(name)


class ContextAdapter(logging.LoggerAdapter):
    """Attaches a dict of context to every record without mutating the logger."""

    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        extra = kwargs.setdefault("extra", {})
        extra.update(self.extra or {})
        ctx = " ".join(f"{k}={v}" for k, v in (self.extra or {}).items())
        return (f"{msg}  [{ctx}]" if ctx else msg), kwargs


def bind(logger: logging.Logger, **context: Any) -> ContextAdapter:
    return ContextAdapter(logger, context)
