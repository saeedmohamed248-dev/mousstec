"""
🌍 Central localization core — عملة / ضريبة / توقيت / لغة لكل دولة.

الهدف: فكّ القفل المصري (الـ 488 مكان اللي فيها "ج.م" ثابتة) عبر مصدر
حقيقة واحد. أي جزء في النظام (templates عبر money filter، admin، services،
reports) يسأل هذا الموديول عن كيفية عرض المبلغ بدل ما يـ hardcode العملة.

القاعدة: الـ tenant يحدد دولته (`Client.country`)، ومنها نشتق العملة والضريبة
والتوقيت واللغة الافتراضية — إلا لو الـ tenant عدّلها صراحةً.

لا يستورد أي model — kernel نقي (يمكن استدعاؤه من أي domain).
"""
from decimal import Decimal, ROUND_HALF_UP

# ---------------------------------------------------------------------------
# إعدادات الدول المدعومة. المفتاح = ISO 3166-1 alpha-2.
# vat_rate هي النسبة القانونية الافتراضية — الـ tenant يقدر يـ override.
# ---------------------------------------------------------------------------
COUNTRY_CONFIG = {
    'EG': {'name_ar': 'مصر',        'name_en': 'Egypt',                'currency': 'EGP', 'vat_rate': Decimal('14.00'), 'timezone': 'Africa/Cairo', 'language': 'ar', 'phone_code': '+20',  'flag': '🇪🇬'},
    'AE': {'name_ar': 'الإمارات',   'name_en': 'United Arab Emirates', 'currency': 'AED', 'vat_rate': Decimal('5.00'),  'timezone': 'Asia/Dubai',   'language': 'ar', 'phone_code': '+971', 'flag': '🇦🇪'},
    'SA': {'name_ar': 'السعودية',   'name_en': 'Saudi Arabia',         'currency': 'SAR', 'vat_rate': Decimal('15.00'), 'timezone': 'Asia/Riyadh',  'language': 'ar', 'phone_code': '+966', 'flag': '🇸🇦'},
    'QA': {'name_ar': 'قطر',        'name_en': 'Qatar',                'currency': 'QAR', 'vat_rate': Decimal('0.00'),  'timezone': 'Asia/Qatar',   'language': 'ar', 'phone_code': '+974', 'flag': '🇶🇦'},
    'KW': {'name_ar': 'الكويت',     'name_en': 'Kuwait',               'currency': 'KWD', 'vat_rate': Decimal('0.00'),  'timezone': 'Asia/Kuwait',  'language': 'ar', 'phone_code': '+965', 'flag': '🇰🇼'},
    'OM': {'name_ar': 'عُمان',      'name_en': 'Oman',                 'currency': 'OMR', 'vat_rate': Decimal('5.00'),  'timezone': 'Asia/Muscat',  'language': 'ar', 'phone_code': '+968', 'flag': '🇴🇲'},
    'BH': {'name_ar': 'البحرين',    'name_en': 'Bahrain',              'currency': 'BHD', 'vat_rate': Decimal('10.00'), 'timezone': 'Asia/Bahrain', 'language': 'ar', 'phone_code': '+973', 'flag': '🇧🇭'},
    'US': {'name_ar': 'الولايات المتحدة', 'name_en': 'United States',  'currency': 'USD', 'vat_rate': Decimal('0.00'),  'timezone': 'America/New_York', 'language': 'en', 'phone_code': '+1', 'flag': '🇺🇸'},
    'GB': {'name_ar': 'المملكة المتحدة', 'name_en': 'United Kingdom',  'currency': 'GBP', 'vat_rate': Decimal('20.00'), 'timezone': 'Europe/London', 'language': 'en', 'phone_code': '+44', 'flag': '🇬🇧'},
}

DEFAULT_COUNTRY = 'EG'

# رمز العملة حسب لغة العرض (عربي / إنجليزي) + عدد الخانات العشرية.
# الدينار (KWD/BHD/OMR) بثلاث خانات حسب المعيار الدولي.
CURRENCY_META = {
    'EGP': {'ar': 'ج.م',  'en': 'EGP', 'decimals': 2},
    'AED': {'ar': 'د.إ',  'en': 'AED', 'decimals': 2},
    'SAR': {'ar': 'ر.س',  'en': 'SAR', 'decimals': 2},
    'QAR': {'ar': 'ر.ق',  'en': 'QAR', 'decimals': 2},
    'KWD': {'ar': 'د.ك',  'en': 'KWD', 'decimals': 3},
    'OMR': {'ar': 'ر.ع',  'en': 'OMR', 'decimals': 3},
    'BHD': {'ar': 'د.ب',  'en': 'BHD', 'decimals': 3},
    'USD': {'ar': '$',    'en': '$',   'decimals': 2},
    'GBP': {'ar': '£',    'en': '£',   'decimals': 2},
    'EUR': {'ar': '€',    'en': '€',   'decimals': 2},
}

# fallback لأي عملة غير معروفة: نعرض الكود نفسه بخانتين.
_UNKNOWN_CURRENCY = {'ar': '', 'en': '', 'decimals': 2}

# اختيارات جاهزة للـ model fields.
COUNTRY_CHOICES = [(code, f"{cfg['flag']} {cfg['name_ar']}") for code, cfg in COUNTRY_CONFIG.items()]
CURRENCY_CHOICES = [(code, f"{meta['ar']} — {code}") for code, meta in CURRENCY_META.items()]


def country_config(country):
    """يرجّع إعدادات الدولة (أو الافتراضية لو غير معروفة)."""
    return COUNTRY_CONFIG.get((country or '').upper(), COUNTRY_CONFIG[DEFAULT_COUNTRY])


def currency_meta(currency):
    return CURRENCY_META.get((currency or '').upper(), dict(_UNKNOWN_CURRENCY))


def currency_symbol(currency, lang='ar'):
    """رمز العملة بلغة العرض. لو العملة غير معروفة نرجّع الكود نفسه."""
    meta = currency_meta(currency)
    sym = meta.get(lang) or meta.get('ar')
    return sym or (currency or '').upper()


def currency_for_country(country):
    return country_config(country)['currency']


def vat_rate_for_country(country):
    return country_config(country)['vat_rate']


def format_money(amount, currency='EGP', lang='ar', decimals=None, symbol=True):
    """
    نسّق مبلغ كنص مقروء: ``1,234.50 د.إ``.

    - ``amount``: أي رقم/Decimal/نص رقمي؛ None → 0.
    - ``currency``: كود ISO؛ يحدد الرمز وعدد الخانات العشرية.
    - ``lang``: 'ar' أو 'en' لاختيار الرمز.
    - ``decimals``: تجاوز عدد الخانات (مثلاً 0 للأرقام الكبيرة).
    - ``symbol``: لو False نرجّع الرقم بدون رمز.
    """
    meta = currency_meta(currency)
    if decimals is None:
        decimals = meta['decimals']
    try:
        value = Decimal(str(amount if amount is not None else 0))
    except (TypeError, ValueError, ArithmeticError):
        value = Decimal('0')
    quant = Decimal(1).scaleb(-decimals) if decimals else Decimal(1)
    value = value.quantize(quant, rounding=ROUND_HALF_UP)
    formatted = f"{value:,.{decimals}f}"
    if not symbol:
        return formatted
    return f"{formatted} {currency_symbol(currency, lang)}"


def current_tenant_symbol():
    """رمز عملة المستأجر الحالي من الـ DB connection (django-tenants).

    مخصّص لأكواد العرض (admin/views/services) اللي بتشتغل جوه request/tenant.
    يسقط بأمان لرمز الافتراضي لو مفيش tenant (public schema) أو أي خطأ.
    """
    try:
        from django.db import connection
        tenant = getattr(connection, 'tenant', None)
        if tenant is None or getattr(tenant, 'schema_name', 'public') == 'public':
            tenant = None
        loc = resolve_tenant_localization(tenant)
        return currency_symbol(loc['currency'], loc['language'])
    except Exception:
        return CURRENCY_META[COUNTRY_CONFIG[DEFAULT_COUNTRY]['currency']]['ar']


def resolve_tenant_localization(tenant):
    """
    يشتق إعدادات التوطين الفعّالة لمستأجر: يبدأ من الحقول الصريحة على
    الـ tenant، ويسقط لإعدادات الدولة، ثم للافتراضي المصري.

    يرجّع dict: {country, currency, vat_rate, timezone, language}.
    آمن لو الـ tenant None أو ناقص أي حقل (public schema / legacy rows).
    """
    country = (getattr(tenant, 'country', None) or DEFAULT_COUNTRY)
    cfg = country_config(country)
    currency = getattr(tenant, 'currency', None) or cfg['currency']
    vat = getattr(tenant, 'vat_rate', None)
    if vat is None:
        vat = cfg['vat_rate']
    tz = getattr(tenant, 'timezone', None) or cfg['timezone']
    lang = getattr(tenant, 'default_language', None) or cfg['language']
    return {
        'country': country,
        'currency': currency,
        'vat_rate': vat,
        'timezone': tz,
        'language': lang,
    }
