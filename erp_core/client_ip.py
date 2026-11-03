"""🌐 IP العميل الحقيقي — مكان واحد بدل ما كل ملف يقرا X-Forwarded-For بطريقته.

ورا Caddy (deploy/Caddyfile) الهيدر ده Caddy هو اللي بيكتبه (بيتجاهل اللي جاي من برّه
طالما مفيش trusted_proxies)، وبورت 8000 مش مفتوح برّه — فأول قيمة فيه هي IP العميل.
بس أي قيمة مش IP (طلب مباشر على السيرفر أو بروكسي غلط) كانت بتتحفظ زي ما هي
وبتوقّع أي عمود inet ⇒ بنتأكد إنها IP، وإلا REMOTE_ADDR.
"""
import ipaddress


def valid_ip(value):
    try:
        return str(ipaddress.ip_address((value or '').strip()))
    except ValueError:
        return None


def client_ip(request_or_environ):
    """IP العميل من `request` أو من WSGI `environ` — أو None لو مفيش IP سليم."""
    meta = getattr(request_or_environ, 'META', request_or_environ)
    xff = meta.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        ip = valid_ip(xff.split(',')[0])
        if ip:
            return ip
    return valid_ip(meta.get('REMOTE_ADDR'))
