"""
🔐 Two-Factor Authentication views (TOTP — RFC 6238).

Endpoints:
  - mfa_setup        : GET shows QR + secret, POST verifies first code → enables.
  - mfa_disable      : POST with password re-auth → disables.
  - mfa_challenge    : Step-up challenge after correct password in login finder.

Storage: `inventory.UserMFA` (tenant-scoped). Login challenge happens on the
public schema using a signed token that carries the target tenant + user id.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import secrets
import time

import pyotp
import qrcode
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.shortcuts import redirect, render
from django.utils import timezone
from django_tenants.utils import schema_context

from clients.models import Client, Domain
from clients.views.auth_views import (
    _client_ip, _throttle, _with_request_port, ADMIN_URL, User,
)

logger = logging.getLogger('mouss_tec_core')

MFA_CHALLENGE_SALT = 'tenant-mfa-challenge'
MFA_CHALLENGE_MAX_AGE = 300  # 5 minutes to enter the code


# =====================================================================
# Helpers
# =====================================================================
def _hash_backup_code(code: str) -> str:
    """SHA-256 hash for backup codes (one-time, stored hashed)."""
    return hashlib.sha256(code.encode('utf-8')).hexdigest()


def _generate_backup_codes(n: int = 10) -> tuple[list[str], list[str]]:
    """Return (plain_codes, hashed_codes). Show plain once, store hashed."""
    plain = [secrets.token_hex(4).upper() for _ in range(n)]
    hashed = [_hash_backup_code(c) for c in plain]
    return plain, hashed


def _qr_data_uri(uri: str) -> str:
    """Generate a PNG QR as a data: URI (no media storage needed)."""
    img = qrcode.make(uri, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


def _get_or_create_mfa(user):
    from inventory.models import UserMFA
    mfa, _ = UserMFA.objects.get_or_create(user=user, defaults={'secret': pyotp.random_base32()})
    return mfa


# =====================================================================
# 🛠️ Setup / Disable (logged-in user only)
# =====================================================================
@login_required
def mfa_setup(request):
    """
    GET  → عرض QR + secret + كود تأكيد.
    POST → التحقق من أول كود وتفعيل 2FA + توليد backup codes.
    """
    from inventory.models import UserMFA
    user = request.user

    try:
        mfa = UserMFA.objects.get(user=user)
    except UserMFA.DoesNotExist:
        mfa = UserMFA.objects.create(user=user, secret=pyotp.random_base32())

    # لو حد عنده secret قديم لكن مش enabled، نخليه يعيد الـ pairing لو طلب
    if request.method == 'GET' and request.GET.get('reset') == '1' and not mfa.is_enabled:
        mfa.secret = pyotp.random_base32()
        mfa.save(update_fields=['secret'])

    ctx = {
        'is_enabled': mfa.is_enabled,
        'enabled_at': mfa.enabled_at,
    }

    if not mfa.is_enabled:
        issuer = 'Mouss Tec'
        account = user.email or user.username
        totp = pyotp.TOTP(mfa.secret)
        uri = totp.provisioning_uri(name=account, issuer_name=issuer)
        ctx.update({
            'qr_data_uri': _qr_data_uri(uri),
            'manual_secret': mfa.secret,
        })

    if request.method == 'POST':
        action = request.POST.get('action', 'verify_enable')

        # 🛡️ Throttle: 5 محاولات / 5 دقايق على نفس المستخدم
        blocked, _ = _throttle(f"mfa_setup:{user.id}", limit=5, window=300)
        if blocked:
            ctx['error'] = '🚫 محاولات كثيرة. حاول بعد 5 دقايق.'
            return render(request, 'clients/mfa_setup.html', ctx)

        if action == 'verify_enable' and not mfa.is_enabled:
            code = request.POST.get('code', '').strip().replace(' ', '')
            if not pyotp.TOTP(mfa.secret).verify(code, valid_window=1):
                ctx['error'] = 'الكود غير صحيح. تأكد من ساعة جهازك ثم حاول مرة أخرى.'
                return render(request, 'clients/mfa_setup.html', ctx)

            plain_codes, hashed_codes = _generate_backup_codes()
            mfa.backup_codes = hashed_codes
            mfa.is_enabled = True
            mfa.enabled_at = timezone.now()
            mfa.save(update_fields=['backup_codes', 'is_enabled', 'enabled_at'])

            return render(request, 'clients/mfa_setup.html', {
                'is_enabled': True,
                'enabled_at': mfa.enabled_at,
                'just_enabled': True,
                'backup_codes': plain_codes,
            })

    return render(request, 'clients/mfa_setup.html', ctx)


@login_required
def mfa_disable(request):
    """Disable MFA — requires password re-auth."""
    from inventory.models import UserMFA
    if request.method != 'POST':
        return redirect('/account/mfa/')

    password = request.POST.get('password', '')
    if not request.user.check_password(password):
        return render(request, 'clients/mfa_setup.html', {
            'is_enabled': True,
            'error': 'كلمة السر غير صحيحة.',
        })

    try:
        mfa = UserMFA.objects.get(user=request.user)
        mfa.is_enabled = False
        mfa.backup_codes = []
        mfa.enabled_at = None
        mfa.save(update_fields=['is_enabled', 'backup_codes', 'enabled_at'])
    except UserMFA.DoesNotExist:
        pass

    return redirect('/account/mfa/?disabled=1')


# =====================================================================
# 🚪 Challenge during login (called from client_login_finder)
# =====================================================================
def issue_mfa_challenge(tenant, user_id, next_url='') -> str:
    """Return a signed token URL fragment for the MFA challenge page."""
    return signing.dumps({
        'schema_name': tenant.schema_name,
        'user_id': user_id,
        'created': int(time.time()),
        'next': next_url or '',
    }, salt=MFA_CHALLENGE_SALT)


def mfa_challenge(request):
    """
    GET /account/mfa/challenge/?token=...
    POST same URL with `code` field.
    """
    token = request.GET.get('token') or request.POST.get('token') or ''
    if not token:
        return redirect('/login/')

    try:
        data = signing.loads(token, salt=MFA_CHALLENGE_SALT, max_age=MFA_CHALLENGE_MAX_AGE)
    except signing.SignatureExpired:
        return redirect('/login/?msg=mfa_expired')
    except signing.BadSignature:
        return redirect('/login/?msg=mfa_bad')

    schema_name = data['schema_name']
    user_id = data['user_id']
    next_url = data.get('next', '')

    tenant = Client.objects.filter(schema_name=schema_name).first()
    if not tenant:
        return redirect('/login/')

    ctx = {'token': token}

    if request.method == 'POST':
        ip = _client_ip(request)
        blocked, _ = _throttle(f"mfa_challenge:{schema_name}:{user_id}:{ip}", limit=5, window=300)
        if blocked:
            ctx['error'] = '🚫 محاولات كثيرة. ابدأ من جديد من صفحة الدخول.'
            return render(request, 'clients/mfa_challenge.html', ctx)

        code = request.POST.get('code', '').strip().replace(' ', '').replace('-', '').upper()
        ok = False
        used_backup = False
        with schema_context(schema_name):
            try:
                from inventory.models import UserMFA
                mfa = UserMFA.objects.get(user_id=user_id, is_enabled=True)
                # TOTP first
                if code.isdigit() and pyotp.TOTP(mfa.secret).verify(code, valid_window=1):
                    ok = True
                # Backup code fallback (8 hex chars)
                elif len(code) == 8:
                    h = _hash_backup_code(code)
                    if h in (mfa.backup_codes or []):
                        ok = True
                        used_backup = True
                        mfa.backup_codes = [c for c in mfa.backup_codes if c != h]
                if ok:
                    mfa.last_used_at = timezone.now()
                    update_fields = ['last_used_at']
                    if used_backup:
                        update_fields.append('backup_codes')
                    mfa.save(update_fields=update_fields)
            except Exception as e:
                logger.warning(f"[MFA_CHALLENGE] lookup failed: {e}")

        if not ok:
            ctx['error'] = 'الكود غير صحيح أو منتهي. حاول مرة أخرى.'
            return render(request, 'clients/mfa_challenge.html', ctx)

        # ✅ Issue auto-login token (نفس الـ flow اللي بيستخدمه password-only login)
        auto_token = signing.dumps({
            'schema_name': schema_name,
            'user_id': user_id,
            'created': int(time.time()),
            'next': next_url,
        }, salt='tenant-auto-login-token')

        domain = Domain.objects.filter(tenant=tenant).first()
        if not domain:
            ctx['error'] = 'خطأ في إعدادات نطاق الشركة.'
            return render(request, 'clients/mfa_challenge.html', ctx)

        # Carry the request's non-standard port (dev :8000) onto the bare
        # tenant host, exactly like the password-only login flow. Without this
        # the cross-subdomain auto-login redirect drops to port 80 and fails
        # with ERR_CONNECTION_REFUSED in local dev. Production (80/443) is
        # untouched.
        protocol = 'https' if request.is_secure() else request.scheme
        host = _with_request_port(domain.domain, request)
        return redirect(f"{protocol}://{host}/auto-login/?token={auto_token}")

    return render(request, 'clients/mfa_challenge.html', ctx)
