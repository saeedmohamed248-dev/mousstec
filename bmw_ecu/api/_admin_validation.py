"""Pure-Python validators for admin endpoints. No Django/DRF imports.

Kept separate so unit tests can exercise the validation matrix without
booting Django.
"""
from __future__ import annotations

from typing import Any

VALID_GRANT_TYPES = {"coding_credits", "isn_credits", "subscription_window"}


def validate_gift_payload(payload: dict[str, Any]) -> str:
    """Return error string if invalid; empty string if valid."""
    if not payload.get("tenant_schema"):
        return "tenant_schema is required"
    gt = payload.get("grant_type")
    if gt not in VALID_GRANT_TYPES:
        return f"grant_type must be one of {sorted(VALID_GRANT_TYPES)}"
    if gt == "subscription_window":
        if not payload.get("valid_until"):
            return "subscription_window requires valid_until"
    else:
        try:
            credits = int(payload.get("credits") or 0)
        except (TypeError, ValueError):
            return "credits must be an integer"
        if credits <= 0:
            return "credits must be > 0 for credit-type gifts"
    return ""
