"""
🧪 متابعة الشركات التجريبية
===========================
لكل شركة تجريبية (أو تجربتها خلصت من قريب): فاضل كام يوم، آخر دخول لأي مستخدم فيها،
وعدد المستندات الفعلية (فواتير بيع للسيارات / طلبات طباعة للمطابع) آخر ٧ أيام وإجمالي.
الهدف: صاحب المنصة يعرف النهارده يكلّم مين — مين بيشتغل فعلاً ومين سجّل ونسي.

ملاحظة: بيدخل schema كل شركة تجريبية — مقصود يتعمل للتجريبي بس (عدد صغير).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Max
from django.utils import timezone
from django_tenants.utils import schema_context

logger = logging.getLogger(__name__)

RECENTLY_EXPIRED_DAYS = 14


def _documents_model(industry):
    if industry == 'printing':
        from printing.models import PrintOrder
        return PrintOrder, 'date_created'
    from inventory.models import SaleInvoice
    return SaleInvoice, 'date_created'


def tenant_activity(tenant, now=None) -> dict:
    now = now or timezone.now()
    week_ago = now - timedelta(days=7)
    out = {'last_login': None, 'docs_7d': 0, 'docs_total': 0, 'last_doc': None, 'error': ''}
    try:
        with schema_context(tenant.schema_name):
            out['last_login'] = get_user_model().objects.aggregate(m=Max('last_login'))['m']
            model, field = _documents_model(getattr(tenant, 'industry', ''))
            qs = model.objects.all()
            out['docs_total'] = qs.count()
            out['docs_7d'] = qs.filter(**{f'{field}__gte': week_ago}).count()
            out['last_doc'] = qs.aggregate(m=Max(field))['m']
    except Exception as e:   # schema ناقص/مكسور مايوقّعش الصفحة كلها
        logger.exception('trial_watch: %s', tenant.schema_name)
        out['error'] = str(e)[:120]
    return out


def _verdict(days_left, act, now):
    """(مفتاح، نص) — ترتيب الأولوية للمتابعة."""
    login = act['last_login']
    idle_days = (now - login).days if login else None
    if days_left < 0:
        return 'expired', 'التجربة خلصت — كلّمه للاشتراك'
    if act['docs_total'] == 0 and (idle_days is None or idle_days >= 2):
        return 'cold', 'سجّل ومابدأش — محتاج مساعدة في البداية'
    if days_left <= 3:
        return 'closing', 'التجربة بتخلص — وقت العرض'
    if act['docs_7d'] == 0:
        return 'idle', 'وقف الاستخدام الأسبوع ده'
    return 'active', 'بيشتغل فعلاً'


_ORDER = {'closing': 0, 'expired': 1, 'cold': 2, 'idle': 3, 'active': 4}


def trial_rows(tenants=None, now=None) -> list[dict]:
    from clients.models import Client
    now = now or timezone.now()
    today = timezone.localdate()
    if tenants is None:
        tenants = (Client.objects.exclude(schema_name='public').filter(is_deleted=False, status='trial')
                   .filter(trial_ends_at__gte=today - timedelta(days=RECENTLY_EXPIRED_DAYS)))
    rows = []
    for t in tenants:
        days_left = (t.trial_ends_at - today).days if t.trial_ends_at else -1
        act = tenant_activity(t, now)
        key, label = _verdict(days_left, act, now)
        phone = ''.join(ch for ch in (t.phone or '') if ch.isdigit())
        if phone.startswith('01') and len(phone) == 11:      # موبايل مصري محلي ⇒ دولي
            phone = '2' + phone
        rows.append({'tenant': t, 'days_left': days_left, 'verdict': key, 'verdict_label': label,
                     'wa_phone': phone, **act})
    rows.sort(key=lambda r: (_ORDER[r['verdict']], r['days_left']))
    return rows
