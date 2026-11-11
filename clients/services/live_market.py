"""
🟢 لايف السوق (للسوبر أدمن) — مين أونلاين دلوقتي في كل سوق وبيعمل إيه، وأرقام النهارده.

المصدر: VisitorLog (كل طلب HTTP بيتسجّل في VisitorTrackingMiddleware) — مفيش جداول جديدة.
* «أونلاين» = آخر طلب خلال ONLINE_WINDOW.
* الزائر بيتعرّف بـ: عميل السوق (mp_session) ← حساب سوق المطابع (كاش بالـ IP من print_market.auth)
  ← الشركة (tenant_schema) ← زائر مجهول (IP + الجهاز).
* السوق بيتحدد من الصفحة، ولو صفحة عامة في /marketplace/ بنستخدم قطاع العميل.
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

ONLINE_WINDOW = timedelta(minutes=5)
MAX_ROWS = 20000

AREAS = OrderedDict((
    ('printing', _('🎨 سوق الطباعة والتصميم')),
    ('automotive', _('🚗 سوق السيارات')),
    ('print_market', _('🖨️ سوق المطابع')),
    ('marketplace', _('🛒 السوق (صفحات عامة)')),
    ('companies', _('🏢 الشركات (السيستم)')),
    ('site', _('🌐 باقي الموقع')),
))

_PREFIXES = (
    ('print_market', ('/marketplace/print-market/',)),
    ('printing', ('/marketplace/printing', '/marketplace/catalog/printing', '/marketplace/design-store',
                  '/printing/', '/design')),
    ('automotive', ('/marketplace/automotive', '/marketplace/catalog/automotive', '/marketplace/parts',
                    '/marketplace/merchant/parts')),
    ('marketplace', ('/marketplace/',)),
)

_OWNER_PREFIXES = ('/superadmin', '/secure-portal', '/admin-tools')
PUBLIC = ('public', '')
PM_IP_KEY = 'pm_online_ip:{}'   # بيتكتب من print_market.auth.current_account


def area_of(path, tenant_schema='public', sector=''):
    if tenant_schema not in PUBLIC:
        return 'companies'
    for area, prefixes in _PREFIXES:
        if path.startswith(prefixes):
            if area == 'marketplace' and sector in ('printing', 'automotive'):
                return sector
            return area
    return 'site'


def _area_q(area):
    """فلتر DB تقريبي لكل سوق (لأرقام النهارده)."""
    if area == 'companies':
        return ~Q(tenant_schema__in=PUBLIC)
    pub = Q(tenant_schema__in=PUBLIC)
    if area == 'site':
        owner = Q()
        for p in _OWNER_PREFIXES:
            owner |= Q(path__startswith=p)
        return pub & ~owner & ~Q(path__startswith='/marketplace/') & ~Q(path__startswith='/printing/') \
            & ~Q(path__startswith='/design')
    for name, prefixes in _PREFIXES:
        if name == area:
            q = Q()
            for p in prefixes:
                q |= Q(path__startswith=p)
            if area == 'marketplace':   # الصفحات العامة بس (من غير اللي اتحسبت لسوق معيّن)
                for other, ps in _PREFIXES[:-1]:
                    for p in ps:
                        q &= ~Q(path__startswith=p)
            return pub & q
    return Q(pk__in=[])


def source_of(referer, path=''):
    """جاي منين — فيسبوك/إنستجرام/جوجل/واتساب/مباشر (من الـ referer أو علامة الإعلان في الرابط)."""
    r = (referer or '').lower()
    p = (path or '').lower()
    if 'fbclid' in p or 'facebook' in p:
        return 'facebook'
    if 'instagram' in p:
        return 'instagram'
    if 'gclid' in p or 'google' in p:
        return 'google'
    if 'instagram.' in r:
        return 'instagram'
    if 'facebook.' in r or 'fb.' in r or 'fb.com' in r:
        return 'facebook'
    if 'google.' in r:
        return 'google'
    if 'whatsapp' in r or 'wa.me' in r:
        return 'whatsapp'
    if 'tiktok' in r:
        return 'tiktok'
    if not r or 'mousstec' in r:
        return 'direct'
    return 'other'


SOURCES = OrderedDict((('facebook', _('فيسبوك')), ('instagram', _('إنستجرام')), ('google', _('جوجل')),
                       ('tiktok', _('تيك توك')), ('whatsapp', _('واتساب')), ('direct', _('مباشر')),
                       ('other', _('مواقع تانية'))))


_BACKGROUND = ('/api/', '/ingest/', '/report/', '/count/', '/notifications/', '/heartbeat', '/ping',
               '/i18n/', '/manifest.json', '/screen-rec/', '/chat/')


def _is_page(lg):
    path = lg['path'].split('?', 1)[0]
    if lg.get('method', 'GET') != 'GET':
        return False
    # صفحة الإشعارات نفسها صفحة؛ الـ polling بتاعها /marketplace/notifications/ (JSON) مش صفحة
    if path.rstrip('/').endswith('print-market/notifications'):
        return True
    return not any(b in path for b in _BACKGROUND)


def _mask_ip(ip):
    ip = str(ip or '')
    if '.' in ip:
        parts = ip.split('.')
        return '.'.join(parts[:2] + ['*', '*'])
    return ip[:9] + '…' if ip else '—'


# 🧭 أسماء مفهومة للصفحات (عربي/إنجليزي) — الباقي بيظهر بالمسار نفسه. الأطول الأول.
PAGE_LABELS = (
    ('/marketplace/print-market/notifications', _('سوق المطابع — الإشعارات')),
    ('/marketplace/print-market/dashboard', _('سوق المطابع — لوحة الحساب')),
    ('/marketplace/print-market/', _('سوق المطابع')),
    ('/marketplace/catalog/printing', _('كتالوج الطباعة')),
    ('/marketplace/catalog/automotive', _('كتالوج السيارات')),
    ('/marketplace/catalog/', _('الكتالوج')),
    ('/marketplace/login/', _('تسجيل الدخول')),
    ('/marketplace/register/', _('صفحة التسجيل')),
    ('/marketplace/verify-otp/', _('تأكيد كود الموبايل')),
    ('/marketplace/password/', _('نسيت كلمة السر')),
    ('/marketplace/logout/', _('تسجيل خروج')),
    ('/marketplace/dashboard/', _('لوحة العميل')),
    ('/marketplace/request/', _('طلب عميل')),
    ('/marketplace/printing', _('قسم الطباعة')),
    ('/marketplace/automotive', _('قسم السيارات')),
    ('/marketplace/design-store', _('متجر التصاميم')),
    ('/marketplace/parts', _('سوق قطع الغيار')),
    ('/marketplace/notifications/', _('الإشعارات')),
    ('/secure-portal/', _('لوحة الشركة')),
    ('/accounts/login', _('تسجيل دخول السيستم')),
    ('/ai/', _('استوديو الذكاء الاصطناعي')),
    ('/printing/', _('الطباعة')),
    ('/design', _('التصميم')),
    ('/manifest.json', _('تحميل التطبيق (PWA)')),
)

# ✍️ العمليات (POST/PUT/DELETE) — «عمل إيه» بكلام مفهوم
ACTION_LABELS = (
    ('/marketplace/print-market/login', _('سجّل دخول في سوق المطابع')),
    ('/marketplace/print-market/register', _('سجّل حساب في سوق المطابع')),
    ('/marketplace/login/', _('سجّل دخول')),
    ('/marketplace/register/', _('عمل حساب جديد')),
    ('/marketplace/verify-otp/', _('أكّد كود الموبايل')),
    ('/marketplace/password/', _('طلب استرجاع كلمة السر')),
    ('/marketplace/logout/', _('سجّل خروج')),
    ('/marketplace/request/create', _('عمل طلب جديد')),
    ('/accounts/login', _('سجّل دخول السيستم')),
)


def page_label(path):
    path = (path or '').split('?', 1)[0]
    if path == '/':
        return _('الصفحة الرئيسية')
    if path == '/marketplace/':
        return _('الصفحة الرئيسية للسوق')
    for prefix, label in PAGE_LABELS:
        if path.startswith(prefix):
            return label
    return ''


def action_label(path):
    path = (path or '').split('?', 1)[0]
    for prefix, label in ACTION_LABELS:
        if path.startswith(prefix):
            return label
    if 'offer' in path:
        return _('قدّم عرض')
    if 'quote' in path:
        return _('طلب عرض سعر')
    if 'order' in path:
        return _('عمل/عدّل أوردر')
    return _('بعت بيانات (فورم/عملية)')


def ua_info(ua):
    """(المتصفح، نظام التشغيل) من الـ User-Agent — تقريبي من غير مكتبات."""
    u = (ua or '').lower()
    if 'edg/' in u:
        browser = 'Edge'
    elif 'samsungbrowser' in u:
        browser = 'Samsung Internet'
    elif 'opr/' in u or 'opera' in u:
        browser = 'Opera'
    elif 'fban' in u or 'fbav' in u:
        browser = 'Facebook App'
    elif 'instagram' in u:
        browser = 'Instagram App'
    elif 'crios' in u or 'chrome' in u:
        browser = 'Chrome'
    elif 'fxios' in u or 'firefox' in u:
        browser = 'Firefox'
    elif 'safari' in u:
        browser = 'Safari'
    else:
        browser = ''
    if 'iphone' in u or 'ipad' in u or 'ios' in u:
        os_name = 'iOS'
    elif 'android' in u:
        os_name = 'Android'
    elif 'windows' in u:
        os_name = 'Windows'
    elif 'mac os' in u or 'macintosh' in u:
        os_name = 'macOS'
    elif 'linux' in u:
        os_name = 'Linux'
    else:
        os_name = ''
    return browser, os_name


VISITOR_TOKEN_KEY = 'live_visitor:{}'
VISITOR_TOKEN_TTL = 60 * 60 * 24 * 3


def visitor_token(key):
    """رمز ثابت للزائر في الرابط (من غير ما الـ IP الكامل يظهر في الصفحة) — بيتحفظ في الكاش ٣ أيام."""
    raw = '|'.join(str(k) for k in key)
    token = hashlib.sha1((settings.SECRET_KEY + raw).encode()).hexdigest()[:20]
    cache.set(VISITOR_TOKEN_KEY.format(token), key, VISITOR_TOKEN_TTL)
    return token


def _visitor_key(lg):
    if lg['marketplace_customer_id']:
        return ('mp', lg['marketplace_customer_id'])
    if lg['tenant_schema'] and lg['tenant_schema'] != 'public':
        return ('co', lg['tenant_schema'], lg['ip_address'])
    return ('ip', lg['ip_address'], (lg['user_agent'] or '')[:60])


_FIELDS = ('timestamp', 'ip_address', 'path', 'method', 'status_code', 'tenant_schema', 'marketplace_customer_id',
           'device_type', 'referer', 'user_agent', 'user_id', 'country')


def _identify(rows):
    """يكمّل لكل زائر: مين (بالاسم لو معروف)، نوعه، رابط ملفه، والسوق."""
    from clients.models import Client, MarketplaceCustomer
    custs = {c.pk: c for c in MarketplaceCustomer.objects.filter(
        pk__in=[v['customer_id'] for v in rows if v['customer_id']])}
    tenants = {t.schema_name: t for t in Client.objects.filter(
        schema_name__in={v['tenant_schema'] for v in rows if v['tenant_schema'] != 'public'})}
    pm_ids = cache.get_many([PM_IP_KEY.format(v['ip']) for v in rows if v['ip']])
    pm_accounts = {}
    if pm_ids:
        from print_market.models import MarketAccount
        pm_accounts = {a.pk: a for a in MarketAccount.objects.filter(pk__in=set(pm_ids.values()))}
    for v in rows:
        cust = custs.get(v['customer_id'])
        sector = cust.sector if cust else ''
        area = area_of(v['path'], v['tenant_schema'], sector)
        who, kind, link, phone = '', 'visitor', '', ''
        if cust:
            who = cust.company_name or cust.full_name
            kind = 'company_customer' if cust.company_name else 'customer'
            link = f'/superadmin/customer/{cust.pk}/detail/'
            phone = cust.phone
        elif v['tenant_schema'] != 'public':
            t = tenants.get(v['tenant_schema'])
            who, kind = (t.name if t else v['tenant_schema']), 'company'
            if t:
                link = f'/superadmin/tenant/{t.pk}/360/'
        else:
            acc = pm_accounts.get(pm_ids.get(PM_IP_KEY.format(v['ip'])))
            if acc and (area == 'print_market' or any(p.startswith('/marketplace/print-market/')
                                                      for p in v.get('paths', ()))):
                who, kind = acc.name, ('pm_shop' if acc.is_shop else 'pm_customer')
                link = f'/{settings.ADMIN_URL}/print_market/marketaccount/{acc.pk}/change/'
        v.update(area=area, area_label=AREAS[area], who=who, kind=kind, link=link, phone=phone)
    return rows


def collect_visitors(since, now=None, max_rows=MAX_ROWS):
    """كل الزوار من وقت since (زائر واحد = صف واحد): دخل منين، آخر صفحة، صفحات، عمليات، أخطاء."""
    from clients.models import VisitorLog
    now = now or timezone.now()
    logs = list(VisitorLog.objects.filter(timestamp__gte=since, timestamp__lte=now).order_by('timestamp')
                .values(*_FIELDS)[:max_rows])
    visitors = OrderedDict()
    for lg in logs:
        lg['tenant_schema'] = lg['tenant_schema'] or 'public'
        if lg['tenant_schema'] == 'public' and lg['path'].startswith(_OWNER_PREFIXES):
            continue   # صاحب المنصة نفسه (السوبر أدمن / أدمن المنصة) مش زائر
        key = _visitor_key(lg)
        v = visitors.get(key)
        if v is None:
            browser, os_name = ua_info(lg['user_agent'])
            v = visitors[key] = {'key': key, 'first_seen': lg['timestamp'], 'pages': 0, 'actions': 0, 'errors': 0,
                                 'ip': lg['ip_address'], 'device': lg['device_type'], 'browser': browser,
                                 'os': os_name, 'country': lg['country'] or '',
                                 'tenant_schema': lg['tenant_schema'],
                                 'customer_id': lg['marketplace_customer_id'], 'user_id': lg['user_id'],
                                 'source': source_of(lg['referer'], lg['path']), 'entry_path': '',
                                 'paths': set()}
        v['last_seen'] = lg['timestamp']
        v['paths'].add(lg['path'])
        if (lg['status_code'] or 0) >= 400:
            v['errors'] += 1
        if lg.get('method', 'GET') not in ('GET', 'HEAD', 'OPTIONS'):
            v['actions'] += 1
            v['last_action'] = action_label(lg['path'])
            v['last_action_at'] = lg['timestamp']
        # طلبات الخلفية (عدّادات/إشعارات/تسجيل شاشة/فورمات) بتثبت إنه موجود بس مش «صفحة»
        if _is_page(lg):
            v['pages'] += 1
            v['path'] = lg['path']
            if not v['entry_path']:
                v['entry_path'] = lg['path']
        v.setdefault('path', lg['path'])
        if lg['marketplace_customer_id']:
            v['customer_id'] = lg['marketplace_customer_id']

    rows = _identify(list(visitors.values()))
    online_since = now - ONLINE_WINDOW
    for v in rows:
        v.pop('paths', None)
        v.update(ip_masked=_mask_ip(v['ip']), source_label=SOURCES.get(v['source'], v['source']),
                 token=visitor_token(v['key']), page_label=page_label(v['path']),
                 entry_label=page_label(v['entry_path'] or v['path']), is_online=v['last_seen'] >= online_since,
                 minutes=max(1, int((v['last_seen'] - v['first_seen']).total_seconds() // 60) + 1))
    rows.sort(key=lambda r: r['last_seen'], reverse=True)
    return rows


def online_now(now=None):
    """قائمة الموجودين دلوقتي (زائر واحد = صف واحد) مرتّبة بآخر نشاط."""
    now = now or timezone.now()
    return collect_visitors(now - ONLINE_WINDOW, now)


def today_visitors(now=None, area='', q=''):
    """كل زوار النهارده (من نص الليل) — مع فلتر السوق والبحث بالاسم/الصفحة."""
    now = now or timezone.now()
    start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = collect_visitors(start, now)
    if area in AREAS:
        rows = [r for r in rows if r['area'] == area]
    if (q or '').strip():
        rows = today_visitors_filter(rows, q)
    return rows


# ───────────────────────── 🧭 رحلة الزائر ─────────────────────────
SESSION_GAP = timedelta(minutes=30)       # سكوت أكتر من كده = زيارة جديدة
MAX_PAGE_SECONDS = 30 * 60                # مدة الصفحة مابتتحسبش أكتر من كده
JOURNEY_MAX_ROWS = 3000


def _journey_filter(key):
    if key[0] == 'mp':
        return Q(marketplace_customer_id=key[1])
    if key[0] == 'co':
        return Q(tenant_schema=key[1], ip_address=key[2])
    q = Q(ip_address=key[1], tenant_schema__in=PUBLIC, marketplace_customer__isnull=True)
    if key[2]:
        q &= Q(user_agent__startswith=key[2])
    return q


def visitor_journey(token, days=1, show_background=False, now=None):
    """رحلة زائر واحد بالتفصيل: كل زيارة (جلسة) — دخل منين وإمتى، كل صفحة شافها والساعة كام وقعد قد إيه،
    كل عملية عملها، الأخطاء اللي قابلته، ووقف فين. يرجّع None لو الرمز انتهى."""
    from clients.models import VisitorLog
    key = cache.get(VISITOR_TOKEN_KEY.format(token))
    if not key:
        return None
    cache.set(VISITOR_TOKEN_KEY.format(token), key, VISITOR_TOKEN_TTL)
    now = now or timezone.now()
    since = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    qs = VisitorLog.objects.filter(_journey_filter(key), timestamp__gte=since, timestamp__lte=now)
    logs = list(qs.order_by('timestamp').values(*_FIELDS)[:JOURNEY_MAX_ROWS])
    truncated = len(logs) >= JOURNEY_MAX_ROWS
    logs = [lg for lg in logs if not ((lg['tenant_schema'] or 'public') == 'public'
                                      and lg['path'].startswith(_OWNER_PREFIXES))]

    sessions, cur = [], None
    for lg in logs:
        lg['tenant_schema'] = lg['tenant_schema'] or 'public'
        is_action = lg['method'] not in ('GET', 'HEAD', 'OPTIONS')
        is_page = _is_page(lg)
        if cur is None or lg['timestamp'] - cur['end'] > SESSION_GAP:
            browser, os_name = ua_info(lg['user_agent'])
            cur = {'start': lg['timestamp'], 'end': lg['timestamp'], 'steps': [], 'pages': 0, 'actions': 0,
                   'errors': 0, 'background': 0, 'device': lg['device_type'], 'browser': browser, 'os': os_name,
                   'ip_masked': _mask_ip(lg['ip_address']), 'country': lg['country'] or '',
                   'source': source_of(lg['referer'], lg['path']), 'referer': lg['referer'] or ''}
            cur['source_label'] = SOURCES.get(cur['source'], cur['source'])
            sessions.append(cur)
        cur['end'] = lg['timestamp']
        status = lg['status_code'] or 0
        if status >= 400:
            cur['errors'] += 1
        if is_action:
            kind, label = 'action', action_label(lg['path'])
            cur['actions'] += 1
        elif is_page:
            kind, label = 'page', page_label(lg['path'])
            cur['pages'] += 1
        else:
            kind, label = 'background', page_label(lg['path'])
            cur['background'] += 1
        cur['steps'].append({'time': lg['timestamp'], 'path': lg['path'], 'label': label, 'kind': kind,
                             'method': lg['method'], 'status': status, 'is_error': status >= 400,
                             'area_label': AREAS[area_of(lg['path'], lg['tenant_schema'])], 'seconds': None})

    pages_total = OrderedDict()
    for s in sessions:
        # مدة كل صفحة = لحد الصفحة/العملية اللي بعدها (طلبات الخلفية مابتقطعش المدة)
        main = [st for st in s['steps'] if st['kind'] != 'background']
        for a, b in zip(main, main[1:]):
            a['seconds'] = min(int((b['time'] - a['time']).total_seconds()), MAX_PAGE_SECONDS)
        if main:
            # آخر صفحة: لو فيه طلبات خلفية بعدها يبقى كان لسه فاتحها لحد آخر طلب
            last = main[-1]
            tail = int((s['end'] - last['time']).total_seconds())
            last['seconds'] = min(tail, MAX_PAGE_SECONDS) if tail else None
            pages_ = [st for st in main if st['kind'] == 'page']
            if pages_:
                pages_[0]['is_entry'] = True
                pages_[-1]['is_exit'] = True
            s['entry'] = pages_[0] if pages_ else main[0]
            s['exit'] = pages_[-1] if pages_ else main[-1]
        else:
            s['entry'] = s['exit'] = s['steps'][0]
        for st in main:
            if st['kind'] != 'page':
                continue
            p = st['path'].split('?', 1)[0]
            agg = pages_total.setdefault(p, {'path': p, 'label': st['label'], 'views': 0, 'seconds': 0})
            agg['views'] += 1
            agg['seconds'] += st['seconds'] or 0
        s['duration_seconds'] = int((s['end'] - s['start']).total_seconds())
        if not show_background:
            s['steps'] = [st for st in s['steps'] if st['kind'] != 'background']

    # مين الزائر ده (نفس منطق لايف السوق)
    head = None
    if logs:
        lg0, lgN = logs[0], logs[-1]
        browser, os_name = ua_info(lgN['user_agent'])
        head = {'key': key, 'ip': lgN['ip_address'], 'tenant_schema': lgN['tenant_schema'] or 'public',
                'customer_id': key[1] if key[0] == 'mp' else None, 'path': lgN['path'],
                'paths': {lg['path'] for lg in logs}}
        _identify([head])
        head.pop('paths', None)
        head.update(ip_masked=_mask_ip(head['ip']), device=lgN['device_type'], browser=browser, os=os_name,
                    country=lgN['country'] or '', first_seen=lg0['timestamp'], last_seen=lgN['timestamp'],
                    is_online=lgN['timestamp'] >= now - ONLINE_WINDOW,
                    source_label=SOURCES.get(source_of(lg0['referer'], lg0['path'])))
    top_pages = sorted(pages_total.values(), key=lambda p: (-p['views'], -p['seconds']))[:10]
    sessions.reverse()   # الأحدث فوق
    return {
        'token': token, 'key_kind': key[0], 'visitor': head, 'sessions': sessions, 'days': days,
        'truncated': truncated, 'top_pages': top_pages, 'since': since,
        'totals': {'sessions': len(sessions), 'pages': sum(s['pages'] for s in sessions),
                   'actions': sum(s['actions'] for s in sessions), 'errors': sum(s['errors'] for s in sessions),
                   'seconds': sum(s['duration_seconds'] for s in sessions)},
    }


def today_stats(now=None):
    """أرقام النهارده لكل سوق: زوار، صفحات، تسجيلات، طلبات، عروض — ومصادر الزيارات."""
    from clients.models import (CatalogQuoteRequest, MarketplaceCustomer, ServiceRequest, TenderOffer,
                                VisitorLog)
    from print_market.models import MarketAccount, MarketOrder, MarketOffer
    now = now or timezone.now()
    start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    base = VisitorLog.objects.filter(timestamp__gte=start)
    areas = OrderedDict()
    for key, label in AREAS.items():
        qs = base.filter(_area_q(key))
        areas[key] = {'label': label, 'visitors': qs.values('ip_address').distinct().count(),
                      'views': qs.count(), 'signups': None, 'requests': None, 'offers': None}

    def by_sector(qs, field='sector'):
        return {r[field]: r['n'] for r in qs.values(field).annotate(n=Count('id'))}

    signups = by_sector(MarketplaceCustomer.objects.filter(created_at__gte=start))
    reqs = by_sector(ServiceRequest.objects.filter(created_at__gte=start))
    offers = by_sector(TenderOffer.objects.filter(created_at__gte=start), 'service_request__sector')
    quotes = by_sector(CatalogQuoteRequest.objects.filter(created_at__gte=start))
    for s in ('printing', 'automotive'):
        areas[s].update(signups=signups.get(s, 0), requests=reqs.get(s, 0) + quotes.get(s, 0),
                        offers=offers.get(s, 0))
    pm_new = MarketAccount.objects.filter(created_at__gte=start)
    areas['print_market'].update(
        signups=pm_new.count(), signups_shops=pm_new.filter(role='shop').count(),
        requests=MarketOrder.objects.filter(created_at__gte=start).count(),
        offers=MarketOffer.objects.filter(created_at__gte=start).count())
    pending = ServiceRequest.objects.filter(status='pending_approval').count()

    # 📣 مصادر الزيارات (أول طلب لكل زائر النهارده في الصفحات العامة)
    first = {}
    for ip, ref, path in (base.filter(tenant_schema__in=PUBLIC).order_by('timestamp')
                          .values_list('ip_address', 'referer', 'path')[:MAX_ROWS]):
        if ip not in first:
            first[ip] = source_of(ref, path)
    src = OrderedDict((k, 0) for k in SOURCES)
    for s in first.values():
        src[s] = src.get(s, 0) + 1
    total = sum(src.values()) or 1
    sources = [{'key': k, 'label': SOURCES[k], 'n': n, 'pct': round(100 * n / total)} for k, n in src.items()]
    return {'areas': areas, 'sources': sources, 'pending_requests': pending, 'since': start}


TABS = ('online', 'today', 'stats')


def snapshot(now=None, tab=None, area='', q=''):
    """tab=None ⇒ كل حاجة (للاختبارات)؛ في الصفحة بنحسب التاب المفتوح بس عشان التحديث كل ١٥ ثانية يفضل خفيف."""
    now = now or timezone.now()
    rows = online_now(now)
    counts = OrderedDict((k, 0) for k in AREAS)
    for r in rows:
        counts[r['area']] += 1
    ctx = {'online': rows, 'online_total': len(rows), 'online_by_area': counts,
           'online_areas': [{'key': k, 'label': AREAS[k], 'n': n} for k, n in counts.items()],
           'now': now, 'tab': tab or 'online', 'area': area if area in AREAS else '', 'q': q or '',
           'areas_list': list(AREAS.items())}
    from clients.models import ServiceRequest
    ctx['pending_requests'] = ServiceRequest.objects.filter(status='pending_approval').count()
    if tab in (None, 'stats'):
        ctx['today'] = today_stats(now)
    if tab in (None, 'today'):
        ctx['visitors_today'] = today_visitors(now, area, q)
        ctx['visitors_today_total'] = len(ctx['visitors_today'])
    if ctx['area'] or ctx['q']:
        ctx['online'] = [r for r in rows if (not ctx['area'] or r['area'] == ctx['area'])
                         and (not ctx['q'] or today_visitors_filter([r], ctx['q']))]
    return ctx


def today_visitors_filter(rows, q):
    q = (q or '').strip().lower()
    return [r for r in rows if q in (r['who'] or '').lower() or q in (r['path'] or '').lower()
            or q in str(r['page_label']).lower() or q in (r['phone'] or '') or q in r['ip_masked']]
