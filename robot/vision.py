"""
robot/vision.py — pluggable perception for the ESP32-CAM image stream.

Two capabilities, both behind a swappable provider so the system runs with no
external AI (deterministic stub) and upgrades to a real model by setting
`ROBOT_VISION_PROVIDER` + an API key:

  * identify_part(image_bytes)      → (label, part_number, confidence)
  * assess_condition(image_bytes)   → (condition_score ∈ [0,1], notes)

The stub returns low-confidence "unknown" so callers fall back to asking the
employee to scan the printed barcode — safe default, never a wrong sale.

To plug in Gemini/Claude/OpenAI vision, implement `_provider_identify` /
`_provider_condition` and select via env. Keep the return contracts identical.
"""

from __future__ import annotations

import os
from typing import Tuple

VISION_PROVIDER = os.getenv("ROBOT_VISION_PROVIDER", "stub").lower()


def identify_part(image_bytes: bytes) -> Tuple[str, str, float]:
    """Classify a part image → (human label, normalized part_number, confidence).

    confidence ∈ [0,1]. Callers should require a printed-barcode fallback when
    confidence is below their threshold (default flow does).
    """
    if VISION_PROVIDER != "stub":
        try:
            return _provider_identify(image_bytes)
        except Exception:
            pass  # fall through to safe stub on any provider error
    return ("", "", 0.0)


def assess_condition(image_bytes: bytes) -> Tuple[float, dict]:
    """Estimate physical wear of a used part → (condition_score, notes).

    condition_score ∈ [0,1] (0 = destroyed, 1 = like-new). The stub returns a
    neutral 0.5 with a flag so pricing stays conservative until a real model is
    wired in.
    """
    if VISION_PROVIDER != "stub":
        try:
            return _provider_condition(image_bytes)
        except Exception:
            pass
    return (0.5, {"provider": "stub", "note": "no vision model configured"})


# --- Provider hooks (implement for real models) ---------------------------

def _provider_identify(image_bytes: bytes) -> Tuple[str, str, float]:  # pragma: no cover
    raise NotImplementedError("Configure a real vision provider for part ID")


def _provider_condition(image_bytes: bytes) -> Tuple[float, dict]:  # pragma: no cover
    raise NotImplementedError("Configure a real vision provider for condition")
