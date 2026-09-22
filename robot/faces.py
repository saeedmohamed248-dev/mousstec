"""
robot/faces.py — turn a face photo into an embedding the ERP can match.

The ESP32-CAM sends a JPEG; `hr.Employee.face_encoding` stores an embedding
vector. This module bridges the two by extracting an embedding from an image,
using the SAME function for enrollment and for recognition so the vectors are
always comparable.

Providers (env `ROBOT_FACE_PROVIDER`):
  * "auto" (default): use the `face_recognition` (dlib) library if it is
    installed, else the deterministic fallback.
  * "dlib": force `face_recognition`.
  * "fallback": force the dependency-free extractor.

The fallback is a real, reproducible embedding (grayscale downscale → normalized
vector). It is NOT production-grade biometrics, but it is consistent, so a face
enrolled with it matches itself on recognition — the pipeline works end-to-end
out of the box, and swapping in dlib/InsightFace is a one-line env change once
you install the library and re-enroll faces.
"""

from __future__ import annotations

import io
import os
from typing import List, Optional

FACE_PROVIDER = os.getenv("ROBOT_FACE_PROVIDER", "auto").lower()

# Fallback embedding geometry.
_FALLBACK_SIZE = 16          # 16x16 grayscale
_FALLBACK_DIM = _FALLBACK_SIZE * _FALLBACK_SIZE  # 256-d vector


def is_biometric() -> bool:
    """True when a real face model is actually doing the matching.

    The fallback extractor is a 16x16 grayscale thumbnail, not a face
    embedding: two different people photographed by the same fixed camera score
    ~0.99 against each other, well past the 0.85 match threshold. It is fine for
    wiring the pipeline up end to end, but it cannot tell people apart, so
    nothing may be authorized on it. `robot.security` checks this before it
    matches anyone.
    """
    if FACE_PROVIDER == "fallback":
        return False
    try:
        import face_recognition  # noqa: F401
        return True
    except Exception:
        # "dlib" was forced but isn't installed -> nothing can match at all.
        return False


def extract_embedding(image_bytes: bytes) -> Optional[List[float]]:
    """Extract a face embedding from a JPEG/PNG, or None if no face/failure.

    The SAME function must be used to enroll employees (see `enroll_employee`)
    so recognition vectors are comparable.
    """
    if not image_bytes:
        return None

    if FACE_PROVIDER in ("auto", "dlib"):
        emb = _dlib_embedding(image_bytes)
        if emb is not None:
            return emb
        if FACE_PROVIDER == "dlib":
            return None  # forced dlib but it couldn't run

    return _fallback_embedding(image_bytes)


def enroll_employee(employee, image_bytes: bytes) -> bool:
    """Compute + store an embedding on an `hr.Employee` from a reference photo.

    Uses the same extractor as recognition, so the stored vector will match live
    scans. Returns True on success.
    """
    emb = extract_embedding(image_bytes)
    if emb is None:
        return False
    employee.face_encoding = emb
    employee.save(update_fields=["face_encoding"])
    return True


# --- Providers -------------------------------------------------------------

def _dlib_embedding(image_bytes: bytes) -> Optional[List[float]]:
    """128-d face embedding via the `face_recognition` library, if installed."""
    try:
        import face_recognition  # type: ignore
        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        arr = np.array(img)
        encs = face_recognition.face_encodings(arr)
        if not encs:
            return None
        return [float(x) for x in encs[0]]
    except Exception:
        return None


def _fallback_embedding(image_bytes: bytes) -> Optional[List[float]]:
    """Dependency-free, reproducible embedding: grayscale downscale, normalized.

    Requires Pillow (already a project dependency). Returns a 256-d unit vector.
    """
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        img = (Image.open(io.BytesIO(image_bytes))
               .convert("L")
               .resize((_FALLBACK_SIZE, _FALLBACK_SIZE)))
        pixels = list(img.getdata())
        if len(pixels) != _FALLBACK_DIM:
            return None
        # Normalize to a unit vector so cosine similarity is stable across
        # brightness changes.
        vec = [p / 255.0 for p in pixels]
        norm = sum(v * v for v in vec) ** 0.5
        if norm == 0:
            return None
        return [v / norm for v in vec]
    except Exception:
        return None
