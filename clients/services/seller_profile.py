"""
🧭 بروفايل بائع السوق — التخصصات والإمكانيات واكتمال البروفايل.

* التخصصات = أقسام الكتالوج اللي بيشتغل فيها (كروت، بنرات…) — نفس مفاتيح SECTORS.
* الإمكانيات = طرق الطباعة والخدمات (أوفست، ديجيتال، لارج فورمات، توصيل…) لكل قطاع.
* اكتمال البروفايل: checklist بيظهر للبائع (زي «Complete your profile» في الأسواق العالمية)
  وبيظهر لصاحب المنصة في لوحة البائعين.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

# (key, label, icon)
CAPABILITIES = {
    'printing': (
        ('offset', _('طباعة أوفست (كميات كبيرة)'), 'fa-industry'),
        ('digital', _('طباعة ديجيتال (كميات صغيرة وسريعة)'), 'fa-print'),
        ('large_format', _('لارج فورمات (بنرات ولافتات)'), 'fa-panorama'),
        ('screen', _('سلك سكرين'), 'fa-fill-drip'),
        ('sublimation', _('سبليميشن (مجات وتيشيرتات)'), 'fa-mug-hot'),
        ('dtf', _('طباعة DTF على القماش'), 'fa-shirt'),
        ('laser', _('ليزر قص وحفر'), 'fa-wand-magic-sparkles'),
        ('cnc', _('CNC وحروف بارزة'), 'fa-gears'),
        ('packaging', _('علب وتغليف (داي كت)'), 'fa-box'),
        ('embroidery', _('تطريز'), 'fa-scissors'),
        ('design', _('تصميم جرافيك'), 'fa-pen-nib'),
        ('urgent', _('طباعة مستعجلة في نفس اليوم'), 'fa-bolt'),
        ('delivery', _('توصيل'), 'fa-truck'),
        ('installation', _('تركيب لافتات'), 'fa-screwdriver-wrench'),
        ('wholesale', _('جملة للمطابع والمكاتب'), 'fa-warehouse'),
    ),
    'automotive': (
        ('original', _('قطع أصلية (أوريجينال)'), 'fa-certificate'),
        ('aftermarket', _('قطع بديلة (تجاري)'), 'fa-box-open'),
        ('used', _('قطع استيراد / مستعمل'), 'fa-recycle'),
        ('chinese', _('عربيات صيني'), 'fa-car'),
        ('korean', _('عربيات كوري'), 'fa-car'),
        ('japanese', _('عربيات ياباني'), 'fa-car'),
        ('european', _('عربيات أوروبي'), 'fa-car'),
        ('delivery', _('توصيل'), 'fa-truck'),
        ('installation', _('تركيب'), 'fa-screwdriver-wrench'),
        ('wholesale', _('جملة'), 'fa-warehouse'),
        ('warranty', _('ضمان على القطع'), 'fa-shield-halved'),
    ),
}


def capability_choices(sector):
    return [(k, label) for k, label, _icon in CAPABILITIES.get(sector, ())]


def capability_items(sector, keys):
    """[(label, icon)] للإمكانيات المختارة بالترتيب الثابت."""
    keys = set(keys or ())
    return [(label, icon) for k, label, icon in CAPABILITIES.get(sector, ()) if k in keys]


def specialty_choices(sector):
    from clients.views.catalog_views import SECTORS, _categories
    return [(k, label) for k, label, _icon in _categories(SECTORS[sector])]


def specialty_items(sector, keys):
    """[(key, label, icon)] للتخصصات المختارة بترتيب الأقسام."""
    from clients.views.catalog_views import SECTORS, _categories
    keys = set(keys or ())
    return [(k, label, icon) for k, label, icon in _categories(SECTORS[sector]) if k in keys]


def whatsapp_digits(raw):
    digits = ''.join(ch for ch in (raw or '') if ch.isdigit())
    if digits.startswith('0') and len(digits) == 11:
        digits = '20' + digits[1:]
    return digits


def completeness(seller):
    """{'pct': 0–100, 'steps': [(done, label, url)]} — خطوات بروفايل المتجر."""
    store = seller.store
    items = store.items.filter(is_active=True).count()
    steps = [
        (bool(seller.specialties), _('حدّد تخصصاتك'), '/marketplace/seller/store/'),
        (bool(seller.capabilities), _('اختار إمكانياتك (طرق الطباعة والخدمات)'), '/marketplace/seller/store/'),
        (bool(store.logo), _('ارفع لوجو المتجر'), '/marketplace/seller/store/'),
        (len((store.description or '').strip()) >= 40, _('اكتب نبذة عن شغلك (٤٠ حرف على الأقل)'),
         '/marketplace/seller/store/'),
        (bool(seller.governorate and seller.address), _('اكتب المحافظة والعنوان'), '/marketplace/seller/store/'),
        (bool(seller.whatsapp), _('ضيف رقم واتساب'), '/marketplace/seller/store/'),
        (items >= 3, _('اعرض ٣ منتجات على الأقل'), '/marketplace/seller/products/new/'),
        (items >= 1 and store.items.filter(is_active=True).exclude(image='').exclude(image__isnull=True).exists(),
         _('ضيف صورة لمنتج واحد على الأقل'), '/marketplace/seller/products/'),
    ]
    if store.sector == 'printing':
        from clients.models import PriceQuote
        steps.append((PriceQuote.objects.filter(seller=seller).exists(), _('سجّل أسعارك في بورصة الأسعار'),
                      '/marketplace/seller/prices/'))
    done = sum(1 for d, _l, _u in steps if d)
    return {'pct': round(100 * done / len(steps)), 'steps': steps, 'done': done, 'total': len(steps)}
