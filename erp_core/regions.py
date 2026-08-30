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


def region_country_for_host(host):
    """يرجّع كود دولة المنطقة (EG/AE/...) من الـ host. آمن لأي إدخال."""
    host = (host or '').split(':')[0].strip().lower()
    if not host:
        return getattr(settings, 'DEFAULT_REGION_COUNTRY', DEFAULT_COUNTRY)
    if host in _ae_hosts() or host.split('.')[0] == 'ae':
        return 'AE'
    return getattr(settings, 'DEFAULT_REGION_COUNTRY', DEFAULT_COUNTRY)


def resolve_region(host):
    """
    إعدادات المنطقة الكاملة (dict) من host:
    {country, currency, currency_symbol, vat_rate, language, name_ar, flag}.
    """
    cc = region_country_for_host(host)
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


def region_from_request(request):
    """اختصار: يشتق المنطقة من request بأمان (fallback للافتراضي)."""
    try:
        return resolve_region(request.get_host())
    except Exception:
        cfg = country_config(getattr(settings, 'DEFAULT_REGION_COUNTRY', DEFAULT_COUNTRY))
        return {
            'country': DEFAULT_COUNTRY, 'currency': cfg['currency'],
            'currency_symbol': currency_symbol(cfg['currency'], cfg['language']),
            'vat_rate': cfg['vat_rate'], 'language': cfg['language'],
            'name_ar': cfg['name_ar'], 'flag': cfg['flag'],
        }


def _host_for_country(country, base_domain):
    """host الموقع لكل دولة: مصر = الدومين الأساسي، الإمارات = أول REGION_AE_HOSTS."""
    if country == 'AE':
        return (_ae_hosts() or [f'ae.{base_domain}'])[0]
    return base_domain


def region_links(request):
    """
    قائمة مواقع المنصة للتبديل بينها (مصري/إماراتي) مع رابط كامل يحافظ على
    نفس المسار، وعلامة is_current للموقع الحالي. تُستخدم لمبدّل الدولة في الهيدر.
    """
    base = getattr(settings, 'BASE_DOMAIN', 'mousstec.com')
    try:
        current = region_country_for_host(request.get_host())
        path = request.get_full_path()
        secure = request.is_secure()
    except Exception:
        current, path, secure = DEFAULT_COUNTRY, '/', True
    scheme = 'https' if secure else 'http'
    out = []
    for code in ('EG', 'AE'):
        cfg = country_config(code)
        host = _host_for_country(code, base)
        out.append({
            'country': code,
            'flag': cfg['flag'],
            'name_ar': cfg['name_ar'],
            'name_en': cfg['name_en'],
            'currency': cfg['currency'],
            'url': f"{scheme}://{host}{path}",
            'is_current': code == current,
        })
    return out
