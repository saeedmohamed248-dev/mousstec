"""
robot/vision.py — perception for the ESP32-CAM image stream.

Wired to the ERP's existing Gemini vision stack (`inventory.ai_services`) and its
background-removal service (`inventory.services.bg_removal`), with a safe
deterministic fallback so the robot still runs with no API key.

Capabilities:
  * identify_part(image_bytes)      → (label, part_number, confidence)
  * assess_condition(image_bytes)   → (condition_score ∈ [0,1], notes)
  * fingerprint(image_bytes)        → dict (visual fingerprint, for learning)
  * white_background(image_bytes)   → bytes | None (studio white cut-out)

Provider selection (env `ROBOT_VISION_PROVIDER`):
  * "auto" (default): use Gemini when a GEMINI_API_KEY is configured, else stub.
  * "gemini": force the ERP Gemini services.
  * "stub":   force the offline deterministic fallback.
"""

from __future__ import annotations

import base64
import os
from typing import Optional, Tuple

VISION_PROVIDER = os.getenv("ROBOT_VISION_PROVIDER", "auto").lower()


def _gemini_ready() -> bool:
    """True if the ERP has a Gemini key configured (so vision is live)."""
    try:
        from django.conf import settings
        return bool(str(getattr(settings, "GEMINI_API_KEY", "") or "").strip())
    except Exception:
        return False


def _use_gemini() -> bool:
    if VISION_PROVIDER == "gemini":
        return True
    if VISION_PROVIDER == "stub":
        return False
    return _gemini_ready()  # auto


def _b64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("ascii")


# ---------------------------------------------------------------------------
# Part identification
# ---------------------------------------------------------------------------

def identify_part(image_bytes: bytes) -> Tuple[str, str, float]:
    """Classify a part image → (human label, normalized part_number, confidence).

    Uses the ERP's Gemini OCR (`read_part_codes_from_image_ai`) to read the part
    number printed on the part/label. Confidence reflects whether a code was
    read. Falls back to the safe stub (0 confidence → caller asks for the printed
    barcode) when Gemini isn't configured or errors.
    """
    if _use_gemini():
        try:
            from inventory.ai_services import read_part_codes_from_image_ai
            result = read_part_codes_from_image_ai(_b64(image_bytes)) or {}
            codes = result.get("codes") or []
            if codes:
                # First code is the most-likely part number (see ai_services).
                return (codes[0], codes[0], 0.9)
        except Exception:
            pass
    return ("", "", 0.0)


def scan_products(image_bytes: bytes) -> list:
    """Extract multiple product rows from a list/count-sheet image (intake helper).

    Delegates to the ERP's `scan_products_image_ai`; returns [] on stub/errors.
    """
    if _use_gemini():
        try:
            from inventory.ai_services import scan_products_image_ai
            return (scan_products_image_ai(_b64(image_bytes)) or {}).get("items") or []
        except Exception:
            pass
    return []


# ---------------------------------------------------------------------------
# Scrap condition assessment
# ---------------------------------------------------------------------------

def assess_condition(image_bytes: bytes) -> Tuple[float, dict]:
    """Estimate physical wear of a used part → (condition_score, notes).

    condition_score ∈ [0,1] (0 = destroyed, 1 = like-new). When Gemini is
    available we ask it to rate wear; otherwise a conservative neutral 0.5 keeps
    pricing safe until a model is wired in.
    """
    if _use_gemini():
        try:
            from inventory.ai_services import call_llm_layer
            import json
            messages = [
                {"role": "system", "content": (
                    "You grade the physical wear of a used auto part from a photo. "
                    "Return STRICTLY JSON: {\"condition_score\": float 0..1 "
                    "(0=destroyed,1=like-new), \"notes\": short string}."
                )},
                {"role": "user", "content": [
                    {"type": "text", "text": "Grade this used part's condition."},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{_b64(image_bytes)}"}},
                ]},
            ]
            raw = call_llm_layer(messages, json_mode=True, max_retries=2, require_pro=True)
            if raw:
                data = json.loads(raw)
                score = max(0.0, min(1.0, float(data.get("condition_score", 0.5))))
                return score, {"provider": "gemini", "note": data.get("notes", "")}
        except Exception:
            pass
    return (0.5, {"provider": "stub", "note": "no vision model configured"})


# ---------------------------------------------------------------------------
# Visual fingerprint (for the learning memory)
# ---------------------------------------------------------------------------

def fingerprint(image_bytes: bytes, product_context: Optional[dict] = None) -> dict:
    """Structured visual fingerprint of a part, for RobotKnowledge learning.

    Delegates to the ERP's `fingerprint_part_image`. Returns {} on stub/errors.
    """
    if _use_gemini():
        try:
            from inventory.ai_services import fingerprint_part_image
            return fingerprint_part_image(_b64(image_bytes), product_context) or {}
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# White-background studio cut-out (for new-product photos)
# ---------------------------------------------------------------------------

def white_background(image_bytes: bytes) -> Optional[bytes]:
    """Return the image with a clean studio-white background, or None.

    Reuses the ERP's `bg_removal` service (`studio_white` preset). Returns None
    when the background-removal model isn't installed — callers then keep the
    original photo.
    """
    try:
        from inventory.services import bg_removal
        if not bg_removal.is_available():
            return None
        return bg_removal.process(image_bytes, "studio_white")
    except Exception:
        return None
