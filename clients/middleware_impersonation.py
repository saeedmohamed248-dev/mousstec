"""
🎭 Impersonation banner middleware — يحقن بانر «أنت داخل كأحد العملاء» في الـ
HTML responses بتاعت المتجر لما الإدمن يكون مفعّل impersonate.

شغّال على Django session فقط — مش بيتدخل في الـ marketplace cookie auth الأصلي.
"""
from __future__ import annotations

from django.urls import reverse, NoReverseMatch


_BANNER_TPL = (
    '<div id="god-mode-impersonation-banner" style="'
    'position:fixed;top:0;left:0;right:0;z-index:2147483647;'
    'background:linear-gradient(90deg,#dc2626,#f59e0b);color:#fff;'
    'padding:10px 18px;font-family:system-ui,sans-serif;font-weight:800;'
    'box-shadow:0 2px 12px rgba(0,0,0,0.35);display:flex;'
    'justify-content:space-between;align-items:center;font-size:13px;">'
    '<span>🎭 <strong>God Mode</strong> — أنت تتصفح المتجر كأحد العملاء: '
    '<strong>{name}</strong> (admin: {admin})</span>'
    '<a href="{exit_url}" style="background:#0f172a;color:#fff;padding:7px 16px;'
    'border-radius:8px;text-decoration:none;font-weight:800;">↩ خروج للأدمن</a>'
    '</div>'
    '<style>body{{padding-top:48px !important;}}</style>'
)


class ImpersonationBannerMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        try:
            self._exit_url = reverse('admin_impersonate_exit')
        except NoReverseMatch:
            self._exit_url = '/admin/'

    def __call__(self, request):
        response = self.get_response(request)

        # نحقن البانر فقط على HTML responses في الـ marketplace
        if not request.path.startswith('/marketplace/'):
            return response
        if response.get('Content-Type', '').split(';')[0].strip() != 'text/html':
            return response
        if not hasattr(request, 'session'):
            return response

        cust_id = request.session.get('impersonating_customer_id')
        if not cust_id:
            return response

        try:
            content = response.content.decode('utf-8', errors='replace')
        except Exception:
            return response

        # حقن قبل </body> — fallback: نضيف في الآخر لو مفيش </body>
        banner = _BANNER_TPL.format(
            name=str(request.session.get('impersonating_customer_name', ''))[:80],
            admin=str(request.session.get('impersonator_admin_username', ''))[:40],
            exit_url=self._exit_url,
        )
        if '</body>' in content:
            content = content.replace('</body>', banner + '</body>', 1)
        else:
            content = content + banner
        response.content = content.encode('utf-8')
        if response.has_header('Content-Length'):
            response['Content-Length'] = len(response.content)
        return response
