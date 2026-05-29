from django.db import migrations
from decimal import Decimal


def seed_packages(apps, schema_editor):
    DesignPackage = apps.get_model('clients', 'DesignPackage')
    defaults = [
        {
            'slug': 'starter', 'name_ar': 'Starter',
            'designs_count': 25, 'price_egp': Decimal('32.00'),
            'price_per_design': Decimal('1.28'),
            'icon_emoji': '🥉', 'accent_color': '#fbbf24', 'sort_order': 1,
            'allows_logo_upload': True, 'allows_watermark': False,
            'allows_source_files': False, 'allows_commercial_use': True,
            'allows_whatsapp_delivery': True, 'free_regenerations_per_design': 3,
            'quality_level': 'hd', 'resolution_max': '2048x2048',
            'description_html': 'مثالية للتجربة. تصاميم HD مع حق استخدام تجاري.',
            'badge_text': '', 'is_featured': False, 'is_active': True,
        },
        {
            'slug': 'pro', 'name_ar': 'Pro',
            'designs_count': 50, 'price_egp': Decimal('55.00'),
            'price_per_design': Decimal('1.10'),
            'icon_emoji': '🥈', 'accent_color': '#cbd5e1', 'sort_order': 2,
            'allows_logo_upload': True, 'allows_watermark': True,
            'allows_source_files': False, 'allows_commercial_use': True,
            'allows_whatsapp_delivery': True, 'free_regenerations_per_design': 3,
            'quality_level': 'hd', 'resolution_max': '2048x2048',
            'description_html': 'أفضل قيمة للأفراد ورواد الأعمال. توصيل واتساب مباشر.',
            'badge_text': 'الأكثر طلباً', 'is_featured': True, 'is_active': True,
        },
        {
            'slug': 'business', 'name_ar': 'Business',
            'designs_count': 100, 'price_egp': Decimal('100.00'),
            'price_per_design': Decimal('1.00'),
            'icon_emoji': '🥇', 'accent_color': '#facc15', 'sort_order': 3,
            'allows_logo_upload': True, 'allows_watermark': True,
            'allows_source_files': False, 'allows_commercial_use': True,
            'allows_whatsapp_delivery': True, 'free_regenerations_per_design': 3,
            'quality_level': 'hd', 'resolution_max': '2048x2048',
            'description_html': 'للشركات والمحلات. لوجو + علامة مائية + جودة فائقة.',
            'badge_text': '🔥 الأوفر', 'is_featured': False, 'is_active': True,
        },
        {
            'slug': 'studio', 'name_ar': 'Studio',
            'designs_count': 250, 'price_egp': Decimal('220.00'),
            'price_per_design': Decimal('0.88'),
            'icon_emoji': '💎', 'accent_color': '#8b5cf6', 'sort_order': 4,
            'allows_logo_upload': True, 'allows_watermark': True,
            'allows_source_files': True, 'allows_commercial_use': True,
            'allows_whatsapp_delivery': True, 'free_regenerations_per_design': 3,
            'quality_level': 'ultra', 'resolution_max': '4096x4096',
            'description_html': 'للوكالات والمصممين. ملفات مصدر + أعلى دقة.',
            'badge_text': '', 'is_featured': False, 'is_active': True,
        },
    ]
    for d in defaults:
        DesignPackage.objects.update_or_create(slug=d['slug'], defaults=d)


def remove_packages(apps, schema_editor):
    DesignPackage = apps.get_model('clients', 'DesignPackage')
    DesignPackage.objects.filter(slug__in=['starter', 'pro', 'business', 'studio']).delete()


class Migration(migrations.Migration):
    dependencies = [('clients', '0015_add_design_store')]
    operations = [migrations.RunPython(seed_packages, remove_packages)]
