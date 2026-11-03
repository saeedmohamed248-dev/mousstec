"""🩹 صفحات أخطاء بتعالج نفسها.

csrf_failure — `CSRF_FAILURE_VIEW`
    🐛 [FIX]: فشل CSRF كان بيطلّع صفحة جانجو الافتراضي (إنجليزي، «CSRF verification failed»)
    والعميل مايعرفش يعمل إيه وبيخسر اللي كتبه. أغلب الحالات بتتعالج قبل ما توصل هنا
    (static/js/pwa-init.js بيحدّث التوكن لحظة الإرسال)؛ اللي بيوصل هنا غالباً الكوكي نفسه
    اتمسح/انتهى. الصفحة دي بتعمل كوكي جديد (`get_token`) وبتقول للعميل «ارجع وابعت تاني» —
    المتصفح بيرجّع الفورم باللي كان مكتوب، وسكريبت الإرسال بيحط التوكن الجديد فتعدّي.
"""
from urllib.parse import urlsplit

from django.http import HttpResponseForbidden, JsonResponse
from django.middleware.csrf import get_token
from django.utils.html import escape
from django.utils.translation import gettext as _


def _safe_back(request):
    """الصفحة اللي الفورم اتبعت منها — بس لو من نفس الدومين."""
    ref = request.META.get('HTTP_REFERER', '')
    parts = urlsplit(ref)
    if ref and parts.netloc == request.get_host() and parts.path.startswith('/'):
        return parts.path + (f'?{parts.query}' if parts.query else '')
    return '/'


def csrf_failure(request, reason=''):
    get_token(request)   # كوكي CSRF جديد — الإرسال الجاي هيعدّي
    msg = _('الصفحة قعدت مفتوحة مدة طويلة فاتأمّن الإرسال من جديد. ارجع للصفحة وابعت تاني — اللي كتبته لسه موجود.')
    wants_json = (request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                  or 'application/json' in request.headers.get('Accept', '')
                  or (request.content_type or '').startswith('application/json'))
    if wants_json:
        return JsonResponse({'error': 'csrf_failed', 'message': msg, 'retry': True}, status=403)

    back = escape(_safe_back(request))
    title = escape(_('محتاج تبعت تاني'))
    btn_back = escape(_('ارجع وابعت تاني'))
    btn_reload = escape(_('افتح الصفحة من جديد'))
    return HttpResponseForbidden(
        '<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{title}</title></head>'
        '<body style="font-family:Cairo,Tahoma,system-ui,sans-serif;background:#0f172a;color:#e2e8f0;'
        'margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center;'
        'text-align:center;padding:24px;">'
        '<div style="max-width:440px"><div style="font-size:44px;margin-bottom:12px">🔄</div>'
        f'<h1 style="font-size:20px;margin:0 0 10px">{title}</h1>'
        f'<p style="color:#94a3b8;font-size:14px;line-height:1.9;margin:0 0 22px">{escape(msg)}</p>'
        '<button type="button" onclick="history.length>1?history.back():location.assign(this.dataset.back)" '
        f'data-back="{back}" style="background:#7c3aed;color:#fff;border:0;border-radius:10px;'
        f'padding:11px 26px;font:inherit;font-weight:700;cursor:pointer">{btn_back}</button>'
        f'<div style="margin-top:14px"><a href="{back}" style="color:#c4b5fd;font-size:13px">{btn_reload}</a></div>'
        '</div></body></html>'
    )
