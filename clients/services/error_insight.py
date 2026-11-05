"""🔎 «المشكلة فين بالظبط؟» — بيحوّل صف SystemErrorLog لكلام مفهوم.

قبل كده مركز الأخطاء كان بيقول «404 /logo.png» وخلاص — الأدمن مايعرفش:
* دي صفحة إيه في السيستم (أوامر الطباعة؟ فاتورة؟) ومين فتحها ومن أنهي جهاز،
* المستخدم كان واقف في أنهي صفحة لما الغلط حصل (يعني الرابط المكسور موجود فين)،
* ولو عطل في الكود: أنهي ملف وأنهي سطر،
* وهل ده عميل حقيقي اتضايق ولا بوت بيلف على الدومينات.

`classify()` نفسها بتستخدمها ErrorTrackingMiddleware وهي بتسجّل، عشان ضوضاء البوتات
تتقفل لوحدها ومركز الأخطاء يفضل فيه المشاكل الحقيقية بس.
"""
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.urls import Resolver404, resolve
from django.utils.translation import gettext_lazy as _

# مسارات بيجرّبها بس بوتات الفحص/الاختراق — مفيش مستخدم حقيقي بيوصلها
_PROBE_PATH = re.compile(
    r'^/(\.env|\.git|\.aws|\.ssh|\.DS_Store|\.well-known/(security\.txt|traffic-advice)|wp-|wordpress|xmlrpc\.php'
    r'|phpmyadmin|pma|administrator|admin/?$|admin\.php|config\.(json|php|yml)|server-status|actuator'
    r'|cgi-bin|vendor/|laravel|telescope|swagger|api-docs|backup|\.?(sql|zip|tar|gz|bak)$|owa/|ecp/|boaform'
    r'|HNAP1|solr|console|debug)', re.I)
# 🐛 [FIX]: الـ regex فوق متثبّت على أول المسار — `/api/actuator/env` و`/blog/wp-login.php` و`/x/.env`
#    كانوا بيعدّوا ويقعدوا مفتوحين في مركز الأخطاء (اتكرر ٤٠ مرة). دول بصمات سكانرات في أي جزء من
#    المسار — مفيش صفحة عندنا اسمها كده — فبتتقفل لوحدها حتى لو المستخدم داخل.
SCANNER_PATH = re.compile(
    r'(?:^|/)(?:\.env|\.git/|\.git$|\.aws|\.ssh|actuator(?:/|$)|wp-(?:admin|login|content|includes|json|config)'
    r'|wordpress|xmlrpc\.php|phpmyadmin|phpunit|cgi-bin|boaform|HNAP1|server-status|telescope)', re.I)
# نفس النمط بصيغة Postgres (`path__iregex`) — لقفل اللي اتسجّل قبل كده (error_autoheal.autoclose_stale)
SCANNER_PATH_SQL = (r'(^|/)(\.env|\.git/|\.git$|\.aws|\.ssh|actuator(/|$)|wp-(admin|login|content|includes|json|config)'
                    r'|wordpress|xmlrpc\.php|phpmyadmin|phpunit|cgi-bin|boaform|HNAP1|server-status|telescope)')

# استثناءات «متوقعة» — جانجو بيحوّلها لـ 404/403/400 مش عطل في الكود.
# 🐛 [FIX]: Http404 من get_object_or_404 كان بيتسجّل «500 — عطل في الكود» (process_exception).
EXPECTED_EXCEPTIONS = {
    'Http404': 404, 'Resolver404': 404, 'PermissionDenied': 403,
    'SuspiciousOperation': 400, 'DisallowedHost': 400, 'DisallowedRedirect': 400, 'BadRequest': 400,
    'RequestDataTooBig': 400, 'TooManyFieldsSent': 400, 'TooManyFilesSent': 400,
    'SuspiciousFileOperation': 400, 'SuspiciousMultipartForm': 400,
}


def effective_status(status, exception_class=''):
    """الحالة الحقيقية: صف قديم متسجّل 500 بسبب Http404 = 404."""
    mapped = EXPECTED_EXCEPTIONS.get(exception_class or '')
    return mapped if mapped and (status or 0) >= 500 else status


_PROBE_EXT = re.compile(r'\.(php\d?|asp|aspx|jsp|cgi|env|bak|sql|ini|log)$', re.I)
_ASSET = re.compile(r'\.(png|jpe?g|gif|ico|svg|webp|css|js|map|woff2?|ttf|txt|xml)$', re.I)
_BOT_UA = re.compile(
    r'bot|crawl|spider|slurp|preview|facebookexternalhit|whatsapp|telegram|discord|skype|curl|wget'
    r'|python-requests|python-urllib|go-http|okhttp|java/|libwww|httpclient|scan|nmap|masscan|zgrab'
    r'|nuclei|censys|shodan|headless|phantom', re.I)

KINDS = {
    'code':        ('🔥', _('عطل في الكود')),
    'bad_request': ('⚠️', _('طلب غير صالح')),
    'broken_link': ('🔗', _('رابط مكسور')),
    'permission':  ('🔒', _('صلاحيات')),
    'csrf':        ('🛡️', _('فشل تحقق الأمان (CSRF)')),
    'not_found':   ('❓', _('صفحة مش موجودة')),
    'asset':       ('🖼️', _('ملف/أيقونة ناقصة')),
    'bot':         ('🤖', _('بوت/زائر — مش مشكلة')),
}
# الأنواع اللي بتتقفل لوحدها وقت التسجيل (مفيش حد يتصرف فيها)
AUTO_RESOLVED_KINDS = {'bot'}


def _is_internal(ref, hosts=()):
    """الـ referer من صفحة عندنا؟ (دومين المنصة أو أي دومين من `hosts` — دومينات الشركة)."""
    if not ref:
        return False
    host = (urlsplit(ref).hostname or '').lower()
    base = (getattr(settings, 'BASE_DOMAIN', '') or 'mousstec.com').lower()
    return bool(host) and (host == base or host.endswith('.' + base) or host in hosts
                           or host.endswith('.localhost') or host in ('localhost', '127.0.0.1'))


def classify(status, path, user_id=None, exception_class='', ua='', referer='', method='GET', hosts=()):
    """نوع الخطأ (مفتاح من KINDS) — من غير أي query. `hosts` = دومينات الشركة (الـ referer منها = من جوه)."""
    path = path or ''
    hosts = {h.lower() for h in hosts if h}
    status = effective_status(status, exception_class)
    if exception_class in EXPECTED_EXCEPTIONS:
        exception_class = ''
    if exception_class or (status or 0) >= 500:
        return 'code'
    if SCANNER_PATH.search(path):
        return 'bot'
    if status == 400:
        return 'bad_request'
    anonymous = not user_id
    if anonymous and (_PROBE_PATH.match(path) or _PROBE_EXT.search(path)
                      or not ua or _BOT_UA.search(ua or '')):
        return 'bot'
    if status == 404 and _ASSET.search(path):
        return 'asset'
    if status == 403:
        if (method or '').upper() == 'POST':
            # 🐛 [FIX]: بوتات بتبعت POST على «/» من غير ما تفتح أي صفحة (٤ طلبات في ثانية من سيرفرات
            #    سحابية) كانت بتظهر «فشل تحقق الأمان — مفتوح» كأن عميل اتمنع. المتصفح الحقيقي اللي
            #    بيبعت فورم من صفحة عندنا بيبعت الـ Referer (SECURE_REFERRER_POLICY = same-origin)؛
            #    زائر مش داخل ومن غير referer من عندنا = بوت. المستخدم الداخل يفضل «CSRF» زي ما هو.
            if anonymous and not _is_internal(referer, hosts):
                return 'bot'
            return 'csrf'   # فورم اتبعت ورجع 403 = غالباً CSRF (الصفحة قعدت مفتوحة/الكوكيز مقفولة)
        return 'permission' if (not anonymous or _is_internal(referer, hosts)) else 'bot'
    if status == 404 and _is_internal(referer, hosts):
        return 'broken_link'
    if anonymous and not _is_internal(referer, hosts):
        return 'bot'      # زائر مش داخل ومش جاي من صفحة عندنا = حد كاتب الرابط/بوت
    return 'not_found'


def view_key(path, resolver_match=None):
    """اسم الـ view (`inventory:job_card_save`) — أو المسار بعد شيل الأرقام لو مالوش view (404)."""
    match = resolver_match
    if match is None:
        try:
            match = resolve(path or '/')
        except Exception:  # noqa: BLE001 — Resolver404 أو urlconf بايظ: نرجع للمسار
            match = None
    if match is not None:
        name = match.view_name or getattr(match.func, '__qualname__', '') or ''
        if name and getattr(match.func, '__name__', '') != 'admin_honeypot':
            return name[:200]
    from clients.services.error_autoheal import normalize_path
    return normalize_path(path)[:200]


def fingerprint(status, path, exception_class='', resolver_match=None):
    """🧬 بصمة الخطأ = الـ view + كود الحالة + الاستثناء. نفس العطل بأرقام مختلفة في الرابط = نفس البصمة."""
    import hashlib
    status = effective_status(status, exception_class)
    key = f"{view_key(path, resolver_match)}|{status}|{exception_class or ''}"
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]


def fingerprint_of(err):
    return getattr(err, 'fingerprint', '') or fingerprint(err.status_code, err.path, err.exception_class)


def page_title(path):
    """اسم الصفحة في السيستم: «أوامر الطباعة — تعديل #12» أو اسم الـ URL."""
    if not path:
        return ''
    try:
        match = resolve(path)
    except Resolver404:
        return _('مسار مش موجود في السيستم')
    except Exception:
        return ''
    model_admin = getattr(match.func, 'model_admin', None)
    if model_admin is not None:
        meta = model_admin.model._meta
        action = (match.url_name or '').rsplit('_', 1)[-1]
        label = {'changelist': _('القائمة'), 'add': _('إضافة'), 'change': _('تعديل'),
                 'delete': _('حذف'), 'history': _('السجل')}.get(action, action)
        obj = match.kwargs.get('object_id')
        return f"{meta.verbose_name_plural} — {label}{f' #{obj}' if obj else ''}"
    if 'admin' in match.namespaces and match.url_name == 'index':
        return _('لوحة الإدارة')
    if getattr(match.func, '__name__', '') == 'admin_honeypot':
        return _('فخ البوتات (مسار بيجرّبه المخترقين بس)')
    return match.view_name or getattr(match.func, '__name__', '')


_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)\n(?P<src>[^\n]*)')


def code_location(tb):
    """آخر سطر من كودنا في الـ traceback (مش من مكتبات بايثون/جانجو)."""
    if not tb:
        return None
    base = str(getattr(settings, 'BASE_DIR', '')).rstrip('/') + '/'
    found = None
    for m in _FRAME.finditer(tb):
        f = m.group('file')
        if 'site-packages' in f or '/lib/python' in f or f.startswith('<'):
            continue
        found = m
    if not found:
        return None
    f = found.group('file')
    for prefix in (base, '/app/'):
        if prefix and f.startswith(prefix):
            f = f[len(prefix):]
            break
    return {'file': f, 'line': int(found.group('line')), 'func': found.group('func'),
            'src': found.group('src').strip()[:160]}


def device(ua):
    ua = ua or ''
    if not ua:
        return _('من غير متصفح')
    if _BOT_UA.search(ua):
        for name in ('WhatsApp', 'facebookexternalhit', 'Telegram', 'Googlebot', 'bingbot', 'curl',
                     'python-requests', 'Go-http-client'):
            if name.lower() in ua.lower():
                return f"🤖 {name}"
        return '🤖 bot'
    os_ = next((n for k, n in (('iPhone', 'آيفون'), ('iPad', 'آيباد'), ('Android', 'أندرويد'),
                               ('Windows', 'ويندوز'), ('Mac OS', 'ماك'), ('Linux', 'لينكس')) if k in ua), '')
    br = next((n for k, n in (('Edg/', 'Edge'), ('OPR/', 'Opera'), ('SamsungBrowser', 'Samsung'),
                              ('Firefox', 'Firefox'), ('Chrome', 'Chrome'), ('Safari', 'Safari')) if k in ua), '')
    return ' — '.join(x for x in (os_, br) if x) or ua[:40]


_HINTS = {
    'code':        _('عطل حقيقي — افتح الملف والسطر اللي تحت وصلّحه.'),
    'bad_request': _('غالباً الصفحة قعدت مفتوحة كتير (انتهت الجلسة) أو الفورم ناقص — لو بيتكرر من نفس الصفحة يبقى فيه مشكلة.'),
    'broken_link': _('فيه رابط في الصفحة اللي كان واقف فيها بيودّي لمكان مش موجود — صلّح الرابط في الصفحة دي.'),
    'csrf':        _('فورم اتبعت واترفض أمنياً — غالباً الصفحة قعدت مفتوحة كتير أو الكوكيز مقفولة. لو بيتكرر من نفس الصفحة يبقى الفورم ناقصه csrf_token.'),
    'permission':  _('المستخدم ملوش صلاحية على الصفحة دي — لو المفروض يكون له، راجع دوره؛ ولو مش المفروض، اخفي الزرار عنه.'),
    'not_found':   _('المستخدم فتح رابط مش موجود (يمكن قديم أو متكتب غلط).'),
    'asset':       _('صورة/ملف متطلب ومش موجود — غالباً المتصفح بيطلبه لوحده.'),
    'bot':         _('بوت أو حد بيجرّب روابط — العميل ماشافش حاجة. بيتقفل لوحده.'),
}


def explain(err, hosts=()):
    """تفاصيل خطأ واحد للعرض. `err` = SystemErrorLog أو dict من .values().
    `hosts` = دومينات الشركة دي (عشان «كان واقف في» تتعرف إنها صفحة عندنا)."""
    g = (err.get if isinstance(err, dict) else lambda k, d=None: getattr(err, k, d))
    rd = g('request_data') or {}
    rd = rd if isinstance(rd, dict) else {}
    ua, ref = rd.get('UA', ''), rd.get('REF', '')
    kind = classify(g('status_code'), g('path'), g('user_id'), g('exception_class') or '', ua, ref,
                    g('method') or 'GET', hosts)
    icon, label = KINDS[kind]
    from_page = None
    if ref:
        parts = urlsplit(ref)
        if _is_internal(ref, {h.lower() for h in hosts}):
            from_page = {'path': parts.path, 'title': page_title(parts.path), 'internal': True}
        else:
            from_page = {'path': parts.hostname or ref[:60], 'title': _('من برّه السيستم'), 'internal': False}
    return {
        'kind': kind, 'icon': icon, 'label': label, 'hint': _HINTS[kind],
        'page': page_title(g('path')),
        'from_page': from_page,
        'code_at': code_location(g('traceback') or ''),
        'who': g('username') or _('زائر (مش داخل)'),
        'device': device(ua),
    }
