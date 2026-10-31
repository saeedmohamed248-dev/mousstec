"""
🧩 الموديولات الاختيارية — كتالوج + مفاتيح تشغيل + قياس الاستخدام
==================================================================
السيستم فيه موديولات كتير، مش كلها بيستخدمها عملاء. بدل ما نمسح كود ممكن نحتاجه، المالك بيقفل
الموديول من «الموديولات» في لوحته: روابطه بترجع 404، بيختفي من القوايم ولوحة Django، ومهامه
المجدولة بتقف — والبيانات بتفضل زي ما هي لحد ما يتفتح تاني. وجنب كل موديول عدد الشركات اللي
استخدمته آخر 30 يوم، عشان قرار الإبقاء أو القفل يبقى بأرقام.

الموديولات الأساسية (المخزن، المطابع، الموارد البشرية، الفوترة، الدعم) مش في الكتالوج — مابتتقفلش.
"""
from __future__ import annotations

import time
from datetime import timedelta

from django.utils import timezone

# code ⇒ الاسم، الوصف، روابطه، تطبيق Django (للقوايم)، مقياس الاستخدام، الحالة الافتراضية
MODULES = {
    'bmw_ecu': {
        'name': 'برمجة BMW/Mini (غرفة التكويدات)', 'sector': 'automotive',
        'desc': 'جلسات برمجة كمبيوتر العربيات بالمعالج والدفع لكل عربية.',
        'prefixes': ('/bmw-ecu/', '/api/ecu/', '/api/admin/'), 'app_label': 'bmw_ecu',
        'usage': ('bmw_ecu.EcuSession', 'started_at', 'tenant'), 'default': True,
    },
    'robot': {
        'name': 'روبوت/كشك الورشة', 'sector': 'automotive',
        'desc': 'كشك بالصوت والوش، جرد بالكاميرا، طلبات إعادة الطلب.',
        'prefixes': ('/robot/', '/api/robot/'), 'app_label': 'robot',
        'usage': ('robot.RobotEntry', 'created_at', 'tenant'), 'default': True,
    },
    'repair_atlas': {
        'name': 'أطلس الإصلاح', 'sector': 'automotive',
        'desc': 'أسئلة الفني وإجابات موثّقة وصور الضفاير.',
        'prefixes': ('/repair-atlas/',), 'app_label': 'repair_atlas',
        'usage': ('repair_atlas.RepairSession', 'created_at', 'tenant'), 'default': True,
    },
    'social_ads': {
        'name': 'Social Studio (التسويق الآلي)', 'sector': 'both',
        'desc': 'نشر وإعلانات فيسبوك/إنستجرام وتجارب A/B وتقارير.',
        'prefixes': ('/social-studio/',), 'app_label': 'social_ads',
        'usage': ('social_ads.SocialPost', 'scheduled_at', 'public'), 'default': True,
    },
    'omnichannel': {
        'name': 'الرد الآلي على واتساب', 'sector': 'both',
        'desc': 'أرقام واتساب للشركات وبوت الرد على العملاء.',
        'prefixes': ('/omnichannel/', '/omnichannel-ai/'), 'app_label': 'omnichannel',
        'usage': ('omnichannel.ChannelMessageLog', 'created_at', 'public'), 'default': True,
    },
    'ai_rooms': {
        'name': 'الغرف الذكية', 'sector': 'both',
        'desc': 'غرف المساعدين الموحّدة (تشخيص · مستشار · تصميم).',
        'prefixes': ('/ai-rooms/',), 'app_label': 'ai_rooms',
        'usage': ('ai_rooms.AIRoomConversation', 'created_at', 'public'), 'default': True,
    },
    'wix': {
        'name': 'ربط متاجر Wix', 'sector': 'both',
        'desc': 'مزامنة منتجات وطلبات متاجر Wix.',
        'prefixes': ('/superadmin/wix/',), 'app_label': None,
        'usage': ('clients.WixConnection', None, 'public'), 'default': True,
    },
}

_CACHE = {'at': 0.0, 'states': {}}
_TTL = 30   # ثانية — التغيير من اللوحة بيوصل كل الـ workers خلال نص دقيقة


def _states():
    if time.monotonic() - _CACHE['at'] < _TTL:
        return _CACHE['states']
    states = {code: spec['default'] for code, spec in MODULES.items()}
    try:
        from clients.models import PlatformModule
        states.update(dict(PlatformModule.objects.values_list('code', 'is_enabled')))
    except Exception:   # noqa: BLE001 — قبل المهاجرة/من غير قاعدة: الافتراضي
        pass
    _CACHE.update(at=time.monotonic(), states=states)
    return states


def clear_cache():
    _CACHE['at'] = 0.0


def is_enabled(code: str) -> bool:
    return _states().get(code, True)


def disabled_codes() -> set[str]:
    return {c for c, on in _states().items() if not on}


def module_for_path(path: str):
    for code, spec in MODULES.items():
        if any(path.startswith(p) for p in spec['prefixes']):
            return code
    return None


def disabled_app_labels() -> set[str]:
    return {MODULES[c]['app_label'] for c in disabled_codes() if MODULES[c]['app_label']}


def skip_if_disabled(code: str):
    """للمهام المجدولة: الموديول المقفول مهامه بتقف من غير ما نشيلها من الجدول."""
    def deco(fn):
        import functools

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not is_enabled(code):
                return {'skipped': f'module {code} is off'}
            return fn(*args, **kwargs)
        return wrapper
    return deco


def usage_stats(days: int = 30) -> dict:
    """عدد الشركات اللي استخدمت كل موديول آخر ``days`` يوم + عدد السجلات. بيلف على سكيمات
    الشركات للموديولات اللي جوه الشركة — بيتكيّش ساعة."""
    from django.apps import apps as django_apps
    from django.core.cache import cache
    from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context

    key = f'platform_module_usage_v1_{days}'
    cached = cache.get(key)
    if cached is not None:
        return cached
    since = timezone.now() - timedelta(days=days)
    out = {}
    tenants = list(get_tenant_model().objects.exclude(schema_name=get_public_schema_name())
                   .filter(is_active=True).values_list('schema_name', flat=True))
    for code, spec in MODULES.items():
        label, date_field, scope = spec['usage']
        try:
            model = django_apps.get_model(label)
        except LookupError:
            continue
        companies = records = 0
        if scope == 'public':
            with schema_context(get_public_schema_name()):
                qs = model.objects.all()
                if date_field:
                    qs = qs.filter(**{f'{date_field}__gte': since})
                records = qs.count()
                companies = qs.values('tenant').distinct().count() if records else 0
        else:
            for schema in tenants:
                try:
                    with schema_context(schema):
                        n = model.objects.filter(**{f'{date_field}__gte': since}).count()
                except Exception:   # noqa: BLE001 — سكيمة ناقصة/قديمة مايوقفش الحساب
                    n = 0
                if n:
                    companies += 1
                    records += n
        out[code] = {'companies': companies, 'records': records}
    out['_tenants'] = len(tenants)
    cache.set(key, out, 3600)
    return out
