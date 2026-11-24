"""
📈 بورصة أسعار المطابع — المنطق كله هنا (الإدخال، المتوسطات، الأسعار الشاذة، التذكير اليومي).

* المطبعة (بائع سوق أو شركة على السيستم) بتكتب سعرها على بنود ثابتة المواصفات.
* «ادّي تاخد»: متوسط السوق وأقل/أعلى سعر في محافظتها بيظهرلها بس لو سعّرت النهارده.
* العميل (صفحة المؤشر العامة) بيشوف نطاق من غير أسماء، وبس لو فيه MIN_PROVIDERS مطابع على الأقل.
* السعر «طازة» FRESH_DAYS أيام — بعد كده مابيدخلش في الحساب لحد ما المطبعة تأكّده.
* سعر بعيد عن متوسط الباقيين بأكتر من OUTLIER_FACTOR مرات ⇒ is_outlier ومايدخلش في الحساب.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from statistics import median

from celery import shared_task
from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone

log = logging.getLogger('mouss_tec_core')

FRESH_DAYS = 3          # السعر بيفضل محسوب ٣ أيام من آخر تأكيد
MIN_PROVIDERS = 3       # أقل عدد مطابع عشان النطاق يظهر للعملاء (خصوصية المطبعة)
OUTLIER_FACTOR = Decimal('3')
TOKEN_SALT = 'price-board-confirm'
TOKEN_MAX_AGE = 60 * 60 * 48

GOVERNORATES = (
    ('cairo', 'القاهرة', 'Cairo'), ('giza', 'الجيزة', 'Giza'), ('alex', 'الإسكندرية', 'Alexandria'),
    ('qalyubia', 'القليوبية', 'Qalyubia'), ('sharqia', 'الشرقية', 'Sharqia'), ('dakahlia', 'الدقهلية', 'Dakahlia'),
    ('gharbia', 'الغربية', 'Gharbia'), ('monufia', 'المنوفية', 'Monufia'), ('beheira', 'البحيرة', 'Beheira'),
    ('kafr_elsheikh', 'كفر الشيخ', 'Kafr El Sheikh'), ('damietta', 'دمياط', 'Damietta'),
    ('port_said', 'بورسعيد', 'Port Said'), ('ismailia', 'الإسماعيلية', 'Ismailia'), ('suez', 'السويس', 'Suez'),
    ('fayoum', 'الفيوم', 'Faiyum'), ('beni_suef', 'بني سويف', 'Beni Suef'), ('minya', 'المنيا', 'Minya'),
    ('assiut', 'أسيوط', 'Asyut'), ('sohag', 'سوهاج', 'Sohag'), ('qena', 'قنا', 'Qena'), ('luxor', 'الأقصر', 'Luxor'),
    ('aswan', 'أسوان', 'Aswan'), ('red_sea', 'البحر الأحمر', 'Red Sea'), ('new_valley', 'الوادي الجديد', 'New Valley'),
    ('matrouh', 'مطروح', 'Matrouh'), ('north_sinai', 'شمال سيناء', 'North Sinai'),
    ('south_sinai', 'جنوب سيناء', 'South Sinai'),
)
GOV_KEYS = {k for k, _ar, _en in GOVERNORATES}


def gov_label(key):
    en = _is_en()
    for k, ar, en_name in GOVERNORATES:
        if k == key:
            return en_name if en else ar
    return key


def gov_choices():
    en = _is_en()
    return [(k, en_name if en else ar) for k, ar, en_name in GOVERNORATES]


def _is_en():
    from django.utils.translation import get_language
    return (get_language() or 'ar').startswith('en')


def guess_governorate(text):
    """«القاهرة - مدينة نصر» ⇒ cairo (أول محافظة اسمها جوه النص)."""
    text = (text or '').replace('أ', 'ا').replace('إ', 'ا').lower()
    for k, ar, en in GOVERNORATES:
        if ar.replace('أ', 'ا').replace('إ', 'ا') in text or en.lower() in text:
            return k
    return ''


def today():
    return timezone.localdate()


# ─────────────── صاحب السعر: بائع سوق أو شركة ───────────────

@dataclass(frozen=True)
class Provider:
    kind: str      # 'seller' | 'merchant'
    obj: object

    @property
    def key(self):
        return {self.kind: self.obj}

    @property
    def name(self):
        return self.obj.store.name if self.kind == 'seller' else self.obj.name

    @property
    def phone(self):
        return self.obj.phone

    def token(self):
        return signing.dumps({'k': self.kind, 'id': self.obj.pk}, salt=TOKEN_SALT, compress=True)

    @classmethod
    def from_token(cls, token):
        from clients.models import Client, MarketSeller
        try:
            data = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
        except signing.BadSignature:
            return None
        if data.get('k') == 'seller':
            obj = MarketSeller.objects.select_related('store').filter(pk=data.get('id'), is_blocked=False).first()
        elif data.get('k') == 'merchant':
            obj = Client.objects.filter(pk=data.get('id'), is_active=True).first()
        else:
            obj = None
        return cls(data['k'], obj) if obj else None


def active_specs(sector='printing'):
    from clients.models import PriceSpec
    return list(PriceSpec.objects.filter(sector=sector, is_active=True))


def last_quotes(provider):
    """{spec_id: آخر سعر} — للتعبئة المسبقة والتأكيد بضغطة."""
    from clients.models import PriceQuote
    out = {}
    for q in PriceQuote.objects.filter(**provider.key).order_by('spec_id', '-quoted_on'):
        out.setdefault(q.spec_id, q)
    return out


def last_governorate(provider):
    q = next(iter(sorted(last_quotes(provider).values(), key=lambda x: x.quoted_on, reverse=True)), None)
    if q:
        return q.governorate
    city = provider.obj.store.city if provider.kind == 'seller' else getattr(provider.obj, 'address', '')
    return guess_governorate(city)


def submitted_today(provider):
    from clients.models import PriceQuote
    return PriceQuote.objects.filter(**provider.key, quoted_on=today()).exists()


def parse_price(raw):
    raw = (raw or '').strip().replace(',', '').translate(str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789'))
    if not raw:
        return None
    try:
        v = Decimal(raw)
    except InvalidOperation:
        raise ValueError(raw)
    if not v.is_finite() or v <= 0 or v >= Decimal('10000000'):
        raise ValueError(raw)
    return v.quantize(Decimal('0.01'))


def save_prices(provider, governorate, prices):
    """prices = {spec_id: Decimal}. سعر النهارده لكل بند (تحديث نفس اليوم بيعدّل نفس الصف)."""
    from clients.models import PriceQuote
    if governorate not in GOV_KEYS:
        raise ValueError('governorate')
    day = today()
    with transaction.atomic():
        for spec_id, price in prices.items():
            PriceQuote.objects.update_or_create(
                spec_id=spec_id, quoted_on=day, **provider.key,
                defaults={'price': price, 'governorate': governorate, 'is_outlier': False})
    flag_outliers(list(prices))
    return len(prices)


def confirm_last(provider):
    """⚡ «أسعاري زي امبارح»: آخر سعر لكل بند يتأكّد بتاريخ النهارده. بيرجّع عدد البنود."""
    last = last_quotes(provider)
    if not last:
        return 0
    gov = last_governorate(provider)
    return save_prices(provider, gov, {sid: q.price for sid, q in last.items() if q.spec.is_active})


def _fresh(qs):
    return qs.filter(quoted_on__gte=today() - timedelta(days=FRESH_DAYS - 1))


def _latest_per_provider(qs):
    """آخر سعر طازة لكل مطبعة (مش كل الأيام) — عشان مطبعة واحدة ماتتحسبش ٣ مرات."""
    seen, out = set(), []
    rows = qs.order_by('-quoted_on', '-updated_at') if hasattr(qs, 'order_by') else \
        sorted(qs, key=lambda q: (q.quoted_on, q.updated_at), reverse=True)
    for q in rows:
        pk = ('s', q.seller_id) if q.seller_id else ('m', q.merchant_id)
        if pk not in seen:
            seen.add(pk)
            out.append(q)
    return out


def flag_outliers(spec_ids):
    """سعر بعيد عن وسيط باقي المطابع بأكتر من OUTLIER_FACTOR مرات ⇒ شاذ (محتاج ٤ أسعار على الأقل)."""
    from clients.models import PriceQuote
    for sid in spec_ids:
        quotes = _latest_per_provider(_fresh(PriceQuote.objects.filter(spec_id=sid)))
        if len(quotes) < 4:
            continue
        for q in quotes:
            others = [o.price for o in quotes if o.pk != q.pk]
            mid = Decimal(median(others))
            bad = q.price > mid * OUTLIER_FACTOR or q.price * OUTLIER_FACTOR < mid
            if bad != q.is_outlier:
                PriceQuote.objects.filter(pk=q.pk).update(is_outlier=bad)


def stats(spec_ids=None, governorate=None):
    """{spec_id: {'n', 'min', 'avg', 'max', 'updated'}} من الأسعار الطازة غير الشاذة."""
    from clients.models import PriceQuote
    qs = _fresh(PriceQuote.objects.filter(is_outlier=False, spec__is_active=True))
    if spec_ids is not None:
        qs = qs.filter(spec_id__in=spec_ids)
    if governorate:
        qs = qs.filter(governorate=governorate)
    by_spec = {}
    for q in qs:
        by_spec.setdefault(q.spec_id, []).append(q)
    out = {}
    for sid, quotes in by_spec.items():
        latest = _latest_per_provider(quotes)
        prices = [q.price for q in latest]
        out[sid] = {
            'n': len(prices), 'min': min(prices), 'max': max(prices),
            'avg': (sum(prices) / len(prices)).quantize(Decimal('1')),
            'updated': max(q.updated_at for q in latest),
        }
    return out


def public_board(sector='printing', governorate=None):
    """صفحة المؤشر العامة: [(spec, stats أو None)] — النطاق بيظهر بس من MIN_PROVIDERS مطابع."""
    specs = active_specs(sector)
    st = stats([s.pk for s in specs], governorate)
    rows = []
    for s in specs:
        row = st.get(s.pk)
        rows.append((s, row if row and row['n'] >= MIN_PROVIDERS else None))
    return rows


def coverage(sector='printing'):
    """المحافظات اللي فيها أسعار كفاية — لفلتر صفحة المؤشر."""
    from clients.models import PriceQuote
    qs = _fresh(PriceQuote.objects.filter(spec__sector=sector, is_outlier=False))
    counts = {}
    for gov, sid, s_id, m_id in qs.values_list('governorate', 'spec_id', 'seller_id', 'merchant_id'):
        counts.setdefault((gov, sid), set()).add((s_id, m_id))
    govs = {gov for (gov, _sid), provs in counts.items() if len(provs) >= MIN_PROVIDERS}
    return [(k, label) for k, label in gov_choices() if k in govs]


# ─────────────── ⏰ تذكير واتساب يومي ───────────────

def _providers_to_remind():
    from clients.models import Client, MarketSeller
    for s in MarketSeller.objects.select_related('store').filter(
            is_blocked=False, upgraded_to__isnull=True, store__sector='printing', store__is_active=True):
        yield Provider('seller', s)
    for c in Client.objects.filter(industry='printing', is_active=True, status__in=('active', 'trial')) \
            .exclude(schema_name='public'):
        yield Provider('merchant', c)


def reminder_link(provider):
    base = (getattr(settings, 'PRINT_MARKET_BASE_URL', '') or 'https://mousstec.com').rstrip('/')
    return f'{base}/marketplace/prices/confirm/{provider.token()}/'


@shared_task(ignore_result=True)
def send_daily_price_reminders():
    """⏰ كل يوم الصبح: كل مطبعة لسه ماسعّرتش النهارده ⇒ واتساب بلينك «أسعاري زي امبارح» بضغطة."""
    if not (getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '') and getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '')):
        return 0
    from clients.services.whatsapp_notify import send_platform_whatsapp
    tpl = getattr(settings, 'PRICE_BOARD_WHATSAPP_TEMPLATE', '')
    sent = 0
    for p in _providers_to_remind():
        if not p.phone or submitted_today(p):
            continue
        has_last = bool(last_quotes(p))
        line = ('أسعار النهارده؟ لو زي امبارح أكّدها بضغطة، أو عدّل اللي اتغيّر — وشوف متوسط السوق في محافظتك.'
                if has_last else
                'سجّل أسعارك في بورصة أسعار المطابع وشوف متوسط السوق في محافظتك — دقيقتين بس.')
        link = reminder_link(p)
        try:
            send_platform_whatsapp(p.phone, template=tpl, params=(p.name, line, link),
                                   text='' if tpl else f'📈 Mouss Tec\n{p.name}، {line}\n{link}')
            sent += 1
        except Exception:  # noqa: BLE001
            log.exception('[PRICE BOARD] reminder failed for %s %s', p.kind, p.obj.pk)
    return sent
