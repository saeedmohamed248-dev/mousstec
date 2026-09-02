"""
Symmetric encryption helper for tenant-supplied secrets (Meta access tokens,
app secrets, BYO LLM keys).

We follow the same Fernet pattern already used for OBD device secrets
(clients/obd_device_models.py) so operators only manage one class of KEK.

Key resolution order:
  1. settings.OMNICHANNEL_SECRET_KEK   (preferred — a dedicated urlsafe-base64 Fernet key)
  2. settings.OBD_DEVICE_SECRET_KEK    (reuse the existing device KEK if present)
  3. a key *derived* from settings.SECRET_KEY (dev/self-heal fallback — logged as a warning)

The derived fallback means the feature works out-of-the-box in development, but
production MUST set OMNICHANNEL_SECRET_KEK: rotating SECRET_KEY would otherwise
render every stored token undecryptable.
"""
from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger("mouss_tec_core")

_warned_about_fallback = False


def _resolve_key() -> bytes:
    global _warned_about_fallback

    key = getattr(settings, "OMNICHANNEL_SECRET_KEK", None) or getattr(
        settings, "OBD_DEVICE_SECRET_KEK", None
    )
    if key:
        return key.encode() if isinstance(key, str) else key

    # Fallback: derive a stable Fernet key from SECRET_KEY. Not ideal, but keeps
    # the add-on functional before an operator provisions a dedicated KEK.
    if not _warned_about_fallback:
        logger.warning(
            "omnichannel: OMNICHANNEL_SECRET_KEK is not set — deriving an "
            "encryption key from SECRET_KEY. Set a dedicated Fernet key in "
            "production (Fernet.generate_key()) so token storage survives a "
            "SECRET_KEY rotation."
        )
        _warned_about_fallback = True
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_resolve_key())


def encrypt(plaintext: str) -> str:
    """Encrypt a secret. Returns urlsafe base64 ciphertext (str) safe for a TextField."""
    if plaintext is None:
        plaintext = ""
    token = _fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt(ciphertext: str) -> str:
    """Decrypt a stored secret. Returns "" for empty/invalid ciphertext (never raises)."""
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        logger.error("omnichannel: failed to decrypt a stored secret (bad KEK or corrupt data)")
        return ""
