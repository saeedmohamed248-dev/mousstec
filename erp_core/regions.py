"""
🌐 Platform Regions — نسخ المنصة حسب الدولة (موقع مصري / موقع إماراتي).

فكرة: نفس المنصة تُقدَّم على أكثر من دومين عام، كل دومين يمثّل دولة:
    mousstec.com      → 🇪🇬 مصر    (EGP)
    ae.mousstec.com   → 🇦🇪 الإمارات (AED)

المنطقة تُشتق من host الطلب — لا middleware إضافي، لا tenant إضافي. تُستخدم في:
  - صفحات التسويق العامة (landing/pricing) → عرض العملة الصحيحة
  - التسجيل → المستأجر الجديد يأخذ دولة المنطقة تلقائياً

الإعداد (settings/env):
  REGION_AE_HOSTS = ['ae.mousstec.com']   # هوستات تُعامل كإمارات
  DEFAULT_REGION_COUNTRY = 'EG'           # الافتراضي لأي هوست آخر

يعتمد على erp_core.localization كمصدر الحقيقة للعملة/الضريبة.
"""
from django.conf import settings

from erp_core.localization import country_config, currency_symbol, DEFAULT_COUNTRY


def _ae_hosts():
    base = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
    return [h.lower() for h in getattr(settings, 'REGION_AE_HOSTS', [f'ae.{base}'])]


# 🍪 اسم كوكي تفضيل الدولة — يسمح بتبديل المنطقة على نفس الدومين بدون
# الحاجة لـ subdomain منفصل (ae.mousstec.com) قد لا يكون له DNS/شهادة.
MT_REGION_COOKIE = 'mt_region'
_VALID_COUNTRIES = ('EG', 'AE')


def region_country_for_request(request):
    """كود دولة المنطقة من الطلب — أولوية للتفضيل الصريح (باراميتر ?region=
    أو كوكي mt_region) ثم اكتشاف الـ host. آمن لأي إدخال.

    ده اللي بيخلّي مبدّل الدولة يشتغل على نفس الدومين فورًا حتى لو الـ
    subdomain الإقليمي (ae.*) مش متظبط DNS/شهادة."""
    try:
        override = (
            (getattr(request, 'GET', None) or {}).get('region')
            or (getattr(request, 'COOKIES', None) or {}).get(MT_REGION_COOKIE)
            or ''
        ).strip().upper()
    except Exception:
        override = ''
    if override in _VALID_COUNTRIES:
        return override
    try:
        return region_country_for_host(request.get_host())
    except Exception:
        return getattr(settings, 'DEFAULT_REGION_COUNTRY', DEFAULT_COUNTRY)


def region_country_for_host(host):
    """يرجّع كود دولة المنطقة (EG/AE/...) من الـ host. آمن لأي إدخال."""
    host = (host or '').split(':')[0].strip().lower()
    if not host:
        return getattr(settings, 'DEFAULT_REGION_COUNTRY', DEFAULT_COUNTRY)
    if host in _ae_hosts() or host.split('.')[0] == 'ae':
        return 'AE'
    return getattr(settings, 'DEFAULT_REGION_COUNTRY', DEFAULT_COUNTRY)


def _region_dict(cc):
    """إعدادات المنطقة الكاملة (dict) من كود دولة."""
    cfg = country_config(cc)
    return {
        'country': cc,
        'currency': cfg['currency'],
        'currency_symbol': currency_symbol(cfg['currency'], cfg['language']),
        'vat_rate': cfg['vat_rate'],
        'language': cfg['language'],
        'name_ar': cfg['name_ar'],
        'flag': cfg['flag'],
    }


def resolve_region(host):
    """
    إعدادات المنطقة الكاملة (dict) من host:
    {country, currency, currency_symbol, vat_rate, language, name_ar, flag}.
    """
    return _region_dict(region_country_for_host(host))


def region_from_request(request):
    """اختصار: يشتق المنطقة من request بأمان (يحترم التفضيل الصريح ثم الـ host)."""
    try:
        return _region_dict(region_country_for_request(request))
    except Exception:
        return _region_dict(getattr(settings, 'DEFAULT_REGION_COUNTRY', DEFAULT_COUNTRY))


def _host_for_country(country, base_domain):
    """host الموقع لكل دولة: مصر = الدومين الأساسي، الإمارات = أول REGION_AE_HOSTS."""
    if country == 'AE':
        return (_ae_hosts() or [f'ae.{base_domain}'])[0]
    return base_domain


def region_links(request):
    """
    قائمة الدول للتبديل بينها (مصري/إماراتي) مع رابط على *نفس الدومين* يمرّ
    عبر مسار set-region (يحفظ التفضيل في كوكي) ويرجّع لنفس الصفحة، وعلامة
    is_current للدولة الحالية. تُستخدم لمبدّل الدولة في الهيدر.

    الاعتماد على نفس الدومين (بدل التوجيه لـ ae.mousstec.com) بيضمن اشتغال
    التبديل فورًا حتى لو الـ subdomain الإقليمي مش متظبط DNS/شهادة.
    """
    from urllib.parse import quote
    try:
        current = region_country_for_request(request)
        path = request.get_full_path() or '/'
    except Exception:
        current, path = DEFAULT_COUNTRY, '/'
    # لو المسار نفسه فيه ?region=... نشيله عشان ميتراكمش
    base_path = path.split('?region=')[0].split('&region=')[0]
    out = []
    for code in ('EG', 'AE'):
        cfg = country_config(code)
        out.append({
            'country': code,
            'flag': cfg['flag'],
            'name_ar': cfg['name_ar'],
            'name_en': cfg['name_en'],
            'currency': cfg['currency'],
            'url': f"/set-region/?to={code}&next={quote(base_path)}",
            'is_current': code == current,
        })
    return out
