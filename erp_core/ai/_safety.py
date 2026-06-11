"""
🛡️ AI Safety Utilities
=====================================================================
Shared safety helpers for all AI-touching code:
  • safe_log_text(): strips API keys / Bearer tokens before logging.
  • safe_fetch_image(): SSRF-safe HTTP image fetch with domain whitelist.
  • check_ai_rate_limit(): per-tenant + per-user rate limit for AI calls.

كل LLM caller و image-downloader في النظام لازم يستخدم الدوال دي
بدل ما يـ log الـ response.text خام أو يعمل requests.get مباشر.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import Optional
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger('mouss_tec_core')

# ---------------------------------------------------------------------
# 1) Log sanitization
# ---------------------------------------------------------------------
_SECRET_PATTERNS = [
    re.compile(r'(Bearer\s+)[A-Za-z0-9_\-\.]{8,}', re.IGNORECASE),
    re.compile(r'("?api[_-]?key"?\s*[:=]\s*"?)([A-Za-z0-9_\-\.]{8,})', re.IGNORECASE),
    re.compile(r'("?authorization"?\s*[:=]\s*"?)([^"\s,}]{8,})', re.IGNORECASE),
    re.compile(r'(sk-[A-Za-z0-9]{20,})'),
    re.compile(r'(tgp_v[12]_[A-Za-z0-9_\-]{20,})'),  # Together AI keys
    re.compile(r'(AIza[0-9A-Za-z\-_]{30,})'),        # Google / Gemini keys
]


def safe_log_text(text: object, max_len: int = 300) -> str:
    """Redacts secrets in arbitrary text before it hits the log handler."""
    s = str(text or '')
    for pat in _SECRET_PATTERNS:
        if pat.groups >= 2:
            s = pat.sub(lambda m: m.group(1) + '***REDACTED***', s)
        else:
            s = pat.sub('***REDACTED***', s)
    if len(s) > max_len:
        s = s[:max_len] + '…'
    return s


# ---------------------------------------------------------------------
# 2) SSRF-safe image fetch
# ---------------------------------------------------------------------
_ALLOWED_IMAGE_HOSTS = {
    'api.together.xyz',
    'api.together.ai',
    'together.ai',
    'generativelanguage.googleapis.com',
    'storage.googleapis.com',
    'lh3.googleusercontent.com',
    'api.ideogram.ai',
    'ideogram.ai',
    'replicate.delivery',
    'replicate.com',
    'pbxt.replicate.delivery',
    'oaidalleapiprodscus.blob.core.windows.net',
    'cdn.openai.com',
    'fal.media',
    'v3.fal.media',
}


def _is_private_ip(host: str) -> bool:
    """Block requests to RFC1918 / loopback / link-local addresses."""
    try:
        resolved = socket.gethostbyname(host)
        addr = ipaddress.ip_address(resolved)
        return (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
        )
    except (socket.gaierror, ValueError):
        return True  # if we can't resolve, treat as unsafe


def safe_fetch_image(
    url: str,
    timeout: int = 20,
    max_bytes: int = 25 * 1024 * 1024,  # 25 MB cap
    extra_allowed_hosts: Optional[set[str]] = None,
) -> Optional[bytes]:
    """
    Safely fetch an image URL with SSRF protection:
      • Scheme must be http/https.
      • Host must be in the whitelist (or extra_allowed_hosts).
      • Resolved IP must not be private/loopback.
      • Response capped at max_bytes.

    Returns bytes on success, None on any failure (logged safely).
    """
    if not url or not isinstance(url, str):
        return None

    try:
        parsed = urlparse(url.strip())
    except Exception:
        logger.warning('[AI SAFETY] malformed image URL')
        return None

    if parsed.scheme not in ('http', 'https'):
        logger.warning(f'[AI SAFETY] blocked non-http scheme: {parsed.scheme}')
        return None

    host = (parsed.hostname or '').lower()
    if not host:
        return None

    allow = set(_ALLOWED_IMAGE_HOSTS)
    if extra_allowed_hosts:
        allow |= {h.lower() for h in extra_allowed_hosts}

    # allow subdomains of whitelisted hosts (e.g. *.replicate.delivery)
    host_ok = host in allow or any(host.endswith('.' + h) for h in allow)
    if not host_ok:
        logger.warning(f'[AI SAFETY] blocked image host: {host}')
        return None

    if _is_private_ip(host):
        logger.warning(f'[AI SAFETY] blocked private/loopback host: {host}')
        return None

    try:
        with requests.get(url, timeout=timeout, stream=True) as resp:
            if resp.status_code != 200:
                logger.warning(f'[AI SAFETY] HTTP {resp.status_code} fetching image from {host}')
                return None
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    logger.warning(f'[AI SAFETY] image exceeded {max_bytes} bytes from {host}')
                    return None
            return bytes(buf)
    except requests.RequestException as e:
        logger.warning(f'[AI SAFETY] image fetch failed: {safe_log_text(e, 120)}')
        return None


# ---------------------------------------------------------------------
# 3) Rate limiting for AI endpoints
# ---------------------------------------------------------------------
def check_ai_rate_limit(
    key: str,
    *,
    per_minute: int = 20,
    per_hour: int = 300,
) -> tuple[bool, Optional[str]]:
    """
    Sliding-bucket rate limit using Django cache.

    Usage:
        ok, msg = check_ai_rate_limit(f'ai_gen:{tenant.id}:{user.id}',
                                      per_minute=10, per_hour=200)
        if not ok:
            return JsonResponse({'error': msg}, status=429)

    Returns (allowed, error_message_or_None).
    """
    minute_key = f'ratelimit:{key}:m'
    hour_key = f'ratelimit:{key}:h'

    try:
        m_count = cache.get(minute_key, 0)
        h_count = cache.get(hour_key, 0)

        if m_count >= per_minute:
            return False, f'تجاوزت الحد المسموح ({per_minute} طلب/دقيقة). حاول بعد قليل.'
        if h_count >= per_hour:
            return False, f'تجاوزت الحد المسموح ({per_hour} طلب/ساعة). حاول لاحقاً.'

        # increment with TTL
        try:
            cache.incr(minute_key)
        except ValueError:
            cache.set(minute_key, 1, 60)
        try:
            cache.incr(hour_key)
        except ValueError:
            cache.set(hour_key, 1, 3600)

        return True, None
    except Exception as e:
        # cache failures must NOT block legitimate requests
        logger.warning(f'[AI SAFETY] rate-limit cache error: {e}')
        return True, None
