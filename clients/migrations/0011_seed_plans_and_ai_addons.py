"""
Seed default subscription plans and AI add-on packages.
"""
from django.db import migrations
from decimal import Decimal


def seed_plans(apps, schema_editor):
    Plan = apps.get_model('clients', 'Plan')
    AIAddonPackage = apps.get_model('clients', 'AIAddonPackage')

    # ── Automotive Plans ──
    Plan.objects.get_or_create(slug='auto-silver', defaults={
        'name': 'Silver — سيلفر',
        'industry': 'automotive',
        'monthly_price': Decimal('685.00'),
        'quarterly_discount': 10,
        'semi_annual_discount': 15,
        'annual_discount': 20,
        'max_branches': 1, 'max_users': 1, 'max_treasuries': 1,
        'features': [
            'فاتورة بيع وشراء', 'مخزون قطع غيار', 'خزينة واحدة',
            '150 كرت صيانة/شهر', '500 صنف مخزن',
        ],
        'sort_order': 1,
    })
    Plan.objects.get_or_create(slug='auto-gold', defaults={
        'name': 'Gold — جولد',
        'industry': 'automotive',
        'monthly_price': Decimal('1185.00'),
        'quarterly_discount': 10,
        'semi_annual_discount': 15,
        'annual_discount': 20,
        'max_branches': 2, 'max_users': 4, 'max_treasuries': 2,
        'features': [
            'كل مميزات Silver', 'فرعين', '4 مستخدمين',
            'كروت صيانة غير محدودة', 'مخزن غير محدود',
            'سوق B2B', 'تقارير أرباح متقدمة',
        ],
        'sort_order': 2,
    })
    Plan.objects.get_or_create(slug='auto-empire', defaults={
        'name': 'Empire — إمباير',
        'industry': 'automotive',
        'monthly_price': Decimal('3000.00'),
        'quarterly_discount': 10,
        'semi_annual_discount': 15,
        'annual_discount': 20,
        'max_branches': 999, 'max_users': 9999, 'max_treasuries': 999,
        'features': [
            'كل مميزات Gold', 'فروع غير محدودة', 'مستخدمين غير محدود',
            'AI Copilot', 'عقود أساطيل', 'دعم فني أولوية',
        ],
        'sort_order': 3,
    })

    # ── Printing Plans ──
    Plan.objects.get_or_create(slug='print-starter', defaults={
        'name': 'Starter — ستارتر',
        'industry': 'printing',
        'monthly_price': Decimal('875.00'),
        'quarterly_discount': 10,
        'semi_annual_discount': 15,
        'annual_discount': 20,
        'max_branches': 1, 'max_users': 1, 'max_treasuries': 1,
        'features': [
            'طلبات طباعة', 'تسعير تلقائي', 'فرع واحد',
            'مستخدم واحد', 'خزينة واحدة',
        ],
        'sort_order': 10,
    })
    Plan.objects.get_or_create(slug='print-pro', defaults={
        'name': 'Pro — برو',
        'industry': 'printing',
        'monthly_price': Decimal('1499.00'),
        'quarterly_discount': 10,
        'semi_annual_discount': 15,
        'annual_discount': 20,
        'max_branches': 2, 'max_users': 4, 'max_treasuries': 2,
        'features': [
            'كل مميزات Starter', 'فرعين', '4 مستخدمين',
            'طلبات غير محدودة', 'تقارير متقدمة',
            'سجل أعمال المصممين',
        ],
        'sort_order': 11,
    })
    Plan.objects.get_or_create(slug='print-enterprise', defaults={
        'name': 'Enterprise — إنتربرايز',
        'industry': 'printing',
        'monthly_price': Decimal('3000.00'),
        'quarterly_discount': 10,
        'semi_annual_discount': 15,
        'annual_discount': 20,
        'max_branches': 999, 'max_users': 9999, 'max_treasuries': 999,
        'features': [
            'كل مميزات Pro', 'فروع غير محدودة', 'مستخدمين غير محدود',
            'دعم فني أولوية', 'تخصيص كامل',
        ],
        'sort_order': 12,
    })

    # ── AI Add-on Packages ──
    AIAddonPackage.objects.get_or_create(slug='ai-basic', defaults={
        'name': 'AI Basic',
        'monthly_price': Decimal('350.00'),
        'ai_generations_limit': 100,
        'whatsapp_messages_limit': 100,
        'features': [
            'AI Prompt-to-Image Generation',
            'Smart Watermarking',
            'Auto-WhatsApp Client Delivery',
        ],
        'sort_order': 1,
    })
    AIAddonPackage.objects.get_or_create(slug='ai-pro', defaults={
        'name': 'AI Pro',
        'monthly_price': Decimal('750.00'),
        'ai_generations_limit': 300,
        'whatsapp_messages_limit': 500,
        'features': [
            'AI Prompt-to-Image Generation',
            'Smart Watermarking',
            'Auto-WhatsApp Client Delivery',
            'أولوية في التوليد',
        ],
        'sort_order': 2,
    })


def reverse_seed(apps, schema_editor):
    pass  # لا نحذف البيانات عند التراجع


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0010_add_plan_ai_addon_subscription_tracker'),
    ]

    operations = [
        migrations.RunPython(seed_plans, reverse_seed),
    ]
