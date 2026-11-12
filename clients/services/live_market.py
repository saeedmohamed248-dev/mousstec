"""
🟢 لايف السوق (للسوبر أدمن) — مين أونلاين دلوقتي في كل سوق وبيعمل إيه، وأرقام النهارده.

المصدر: VisitorLog (كل طلب HTTP بيتسجّل في VisitorTrackingMiddleware) — مفيش جداول جديدة.
* «أونلاين» = آخر طلب خلال ONLINE_WINDOW.
* الزائر بيتعرّف بـ: عميل السوق (mp_session) ← حساب سوق المطابع (كاش بالـ IP من print_market.auth)
  ← الشركة (tenant_schema) ← زائر مجهول (IP + الجهاز).
* السوق بيتحدد من الصفحة، ولو صفحة عامة في /marketplace/ بنستخدم قطاع العميل.
"""
from __future__ import annotations

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


def online_now(now=None):
    """قائمة الموجودين دلوقتي (زائر واحد = صف واحد) مرتّبة بآخر نشاط."""
    from clients.models import Client, MarketplaceCustomer, VisitorLog
    now = now or timezone.now()
    since = now - ONLINE_WINDOW
    logs = list(VisitorLog.objects.filter(timestamp__gte=since).order_by('timestamp')
                .values('timestamp', 'ip_address', 'path', 'method', 'tenant_schema', 'marketplace_customer_id',
                        'device_type', 'referer', 'user_agent', 'user_id')[:MAX_ROWS])
    # جلسة الزائر النهارده (أول صفحة + المصدر) — من أول طلب ليه خلال آخر ساعتين
    visitors = OrderedDict()
    for lg in logs:
        lg['tenant_schema'] = lg['tenant_schema'] or 'public'
        if lg['tenant_schema'] == 'public' and lg['path'].startswith(_OWNER_PREFIXES):
            continue   # صاحب المنصة نفسه (السوبر أدمن / أدمن المنصة) مش زائر
        if lg['marketplace_customer_id']:
            key = ('mp', lg['marketplace_customer_id'])
        elif lg['tenant_schema'] and lg['tenant_schema'] != 'public':
            key = ('co', lg['tenant_schema'], lg['ip_address'])
        else:
            key = ('ip', lg['ip_address'], (lg['user_agent'] or '')[:60])
        v = visitors.get(key)
        if v is None:
            v = visitors[key] = {'key': key, 'first_seen': lg['timestamp'], 'pages': 0, 'ip': lg['ip_address'],
                                 'device': lg['device_type'], 'tenant_schema': lg['tenant_schema'],
                                 'customer_id': lg['marketplace_customer_id'], 'user_id': lg['user_id'],
                                 'source': source_of(lg['referer'], lg['path'])}
        v['last_seen'] = lg['timestamp']
        # طلبات الخلفية (عدّادات/إشعارات/تسجيل شاشة/فورمات) بتثبت إنه موجود بس مش «صفحة»
        if _is_page(lg):
            v['pages'] += 1
            v['path'] = lg['path']
        v.setdefault('path', lg['path'])
        if lg['marketplace_customer_id']:
            v['customer_id'] = lg['marketplace_customer_id']

    custs = {c.pk: c for c in MarketplaceCustomer.objects.filter(
        pk__in=[v['customer_id'] for v in visitors.values() if v['customer_id']])}
    tenants = {t.schema_name: t for t in Client.objects.filter(
        schema_name__in={v['tenant_schema'] for v in visitors.values() if v['tenant_schema'] != 'public'})}
    pm_ids = cache.get_many([PM_IP_KEY.format(v['ip']) for v in visitors.values() if v['ip']])
    pm_accounts = {}
    if pm_ids:
        from print_market.models import MarketAccount
        pm_accounts = {a.pk: a for a in MarketAccount.objects.filter(pk__in=set(pm_ids.values()))}

    rows = []
    for v in visitors.values():
        cust = custs.get(v['customer_id'])
        sector = cust.sector if cust else ''
        area = area_of(v['path'], v['tenant_schema'], sector)
        who, kind, link = '', 'visitor', ''
        if cust:
            who = cust.company_name or cust.full_name
            kind = 'company_customer' if cust.company_name else 'customer'
            link = f'/superadmin/customer/{cust.pk}/detail/'
        elif v['tenant_schema'] != 'public':
            t = tenants.get(v['tenant_schema'])
            who, kind = (t.name if t else v['tenant_schema']), 'company'
            if t:
                link = f'/superadmin/tenant/{t.pk}/360/'
        else:
            acc = pm_accounts.get(pm_ids.get(PM_IP_KEY.format(v['ip'])))
            if acc and area == 'print_market':
                who, kind = acc.name, ('pm_shop' if acc.is_shop else 'pm_customer')
                link = f'/{settings.ADMIN_URL}/print_market/marketaccount/{acc.pk}/change/'
        rows.append({**v, 'area': area, 'area_label': AREAS[area], 'who': who, 'kind': kind, 'link': link,
                     'ip_masked': _mask_ip(v['ip']), 'source_label': SOURCES.get(v['source'], v['source']),
                     'minutes': max(1, int((v['last_seen'] - v['first_seen']).total_seconds() // 60) + 1)})
    rows.sort(key=lambda r: r['last_seen'], reverse=True)
    return rows


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
    from print_market.models import MarketPayment
    owner_todo = {
        'pm_payments': MarketPayment.objects.filter(status='pending').count(),
        'pm_disputes': MarketOrder.objects.filter(status='disputed').count(),
        'pm_payouts': MarketOrder.objects.filter(status='completed', payout_done_at__isnull=True).count(),
    }

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
    return {'areas': areas, 'sources': sources, 'pending_requests': pending, 'owner_todo': owner_todo,
            'since': start}


def snapshot(now=None):
    rows = online_now(now)
    counts = OrderedDict((k, 0) for k in AREAS)
    for r in rows:
        counts[r['area']] += 1
    return {'online': rows, 'online_total': len(rows), 'online_by_area': counts,
            'online_areas': [{'key': k, 'label': AREAS[k], 'n': n} for k, n in counts.items()], 'today': today_stats(now), 'now': now or timezone.now()}
