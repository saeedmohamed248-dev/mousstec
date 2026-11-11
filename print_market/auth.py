"""🔑 دخول سوق المطابع — كوكي موقّع (pm_session) منفصل عن حسابات الشركات."""
from functools import wraps

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from erp_core.client_ip import client_ip as get_client_ip

from .models import MarketAccount

COOKIE = 'pm_session'
SALT = 'print_market.session'
MAX_AGE = 60 * 60 * 24 * 30


def current_account(request):
    if hasattr(request, '_pm_account'):
        return request._pm_account
    acc = None
    try:
        raw = request.get_signed_cookie(COOKIE, salt=SALT, max_age=MAX_AGE)
        pk, key = raw.split(':', 1)
        acc = MarketAccount.objects.filter(pk=int(pk), session_key=key, is_active=True).first()
    except (KeyError, ValueError, signing.BadSignature):
        acc = None
    request._pm_account = acc
    return acc


def login(response, account):
    response.set_signed_cookie(COOKIE, f'{account.pk}:{account.session_key}', salt=SALT, max_age=MAX_AGE,
                               httponly=True, samesite='Lax', secure=not settings.DEBUG)
    return response


def logout(response):
    response.delete_cookie(COOKIE, samesite='Lax')
    return response


def safe_next(request, default):
    nxt = request.POST.get('next') or request.GET.get('next') or ''
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return nxt
    return default


def require_account(role=None):
    """يلزم دخول (وممكن نوع حساب معيّن)."""
    def deco(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            acc = current_account(request)
            if acc is None:
                return redirect(f"{reverse('print_market:login')}?next={request.get_full_path()}")
            if role and acc.role != role:
                return redirect('print_market:home_for_account')
            request.pm_account = acc
            return view(request, *args, **kwargs)
        return wrapper
    return deco


def rate_limited(request, bucket, limit=10, window=900):
    """حد بسيط للمحاولات لكل IP (دخول / تسجيل)."""
    key = f'pm_rl:{bucket}:{get_client_ip(request)}'
    n = cache.get(key, 0)
    if n >= limit:
        return True
    cache.set(key, n + 1, window)
    return False
