"""The employees' PINs for the counter page («مخ موس» with the net down).

Shared on purpose: the server (robot/views_ui.py) hashes a PIN when a manager
sets it, the brain checks one on its counter page — this one file, no
Django. The format is Django's own PBKDF2 («pbkdf2_sha256$iterations$salt$
hash»), so `django.contrib.auth.hashers.check_password` reads it too.

A 4–6 digit PIN says who recorded a sale or an expense; it isn't a
password. The hashes travel to the robot inside its catalog, so the catalog
on that computer must stay on that computer.
"""

import base64
import hashlib
import hmac
import secrets

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 200_000                 # ~0.2 s on a mini PC: fine for a login, slow to guess
MIN_DIGITS, MAX_DIGITS = 4, 6


def valid(pin: str) -> bool:
    pin = str(pin or "")
    return pin.isdigit() and pin.isascii() and MIN_DIGITS <= len(pin) <= MAX_DIGITS


def hash_pin(pin: str, *, salt: str = "", iterations: int = ITERATIONS) -> str:
    if not valid(pin):
        raise ValueError(f"PIN must be {MIN_DIGITS}–{MAX_DIGITS} digits")
    salt = salt or secrets.token_hex(11)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), iterations)
    return f"{ALGORITHM}${iterations}${salt}${base64.b64encode(digest).decode()}"


def check_pin(pin: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = str(encoded or "").split("$", 3)
        iterations = int(iterations)
    except ValueError:
        return False
    if algorithm != ALGORITHM or not valid(pin) or not 1 <= iterations <= 5_000_000:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", str(pin).encode(), salt.encode(), iterations)
    return hmac.compare_digest(base64.b64encode(digest).decode(), expected)
