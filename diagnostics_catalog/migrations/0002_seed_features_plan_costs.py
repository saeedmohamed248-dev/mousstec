"""
Seed initial data:
  - Feature catalog entries for the 4 diagnostics features
  - Premium Diagnostics Plan (6000 EGP/month, automotive)
  - APICostRate defaults
  - A handful of generic DTC definitions (open-source community data)
"""
from django.db import migrations


DTC_SEED = [
    # (code, system, severity, short, full, likely_oem_parts)
    ('P0171', 'P', 'medium', 'System Too Lean (Bank 1)',
     'مزيج الوقود/الهواء فقير في البنك الأول — تحقق من حساس MAF وتسريبات الهواء.',
     ['13627585493', '13627566984']),
    ('P0301', 'P', 'high', 'Cylinder 1 Misfire Detected',
     'اختلال احتراق في الأسطوانة الأولى — تحقق من البوجيهات والكويلز.',
     ['12137594937', '12120037244']),
    ('P0420', 'P', 'medium', 'Catalyst System Efficiency Below Threshold (Bank 1)',
     'كفاءة المحول الحفاز أقل من الحد — احتمال تلف المحول أو خلل في الحساسات.',
     ['11787589121', '18307812278']),
    ('P0128', 'P', 'low', 'Coolant Thermostat Below Regulating Temperature',
     'الثرموستات لا يصل لدرجة حرارة التشغيل — قد يكون عالقاً مفتوحاً.',
     ['11537549476']),
    ('U0100', 'U', 'critical', 'Lost Communication With ECM/PCM "A"',
     'انقطاع التواصل مع وحدة التحكم في المحرك — فحص فوري مطلوب.', []),
    ('B1000', 'B', 'medium', 'ECU Malfunction',
     'خلل عام في وحدة تحكم الجسم.', []),
    ('C0035', 'C', 'high', 'Left Front Wheel Speed Sensor Circuit Malfunction',
     'خلل في حساس سرعة العجلة الأمامية اليسرى.',
     ['34526870076']),
]


def seed(apps, schema_editor):
    Feature = apps.get_model('clients', 'Feature')
    Plan = apps.get_model('clients', 'Plan')
    DTCDefinition = apps.get_model('diagnostics_catalog', 'DTCDefinition')
    APICostRate = apps.get_model('diagnostics_catalog', 'APICostRate')

    # 1. Features
    features = [
        ('diagnostics_live_data', 'Live Data Streaming', 'البيانات الحية اللحظية',
         'integrations', False, ''),
        ('diagnostics_guided_tests', 'Guided Test Plans (ISTA)', 'خطط الفحص الموجَّه',
         'workshop', False, ''),
        ('diagnostics_smart_parts_finder', 'Smart Parts Finder', 'البحث الذكي عن قطع الغيار',
         'workshop', False, ''),
        ('diagnostics_external_api_scans', 'External DTC API Scans', 'فحوصات API الخارجية',
         'integrations', True, 'فحص/شهر'),
    ]
    for code, name_en, name_ar, cat, quant, unit in features:
        Feature.objects.update_or_create(
            code=code,
            defaults={
                'name_en': name_en,
                'name_ar': name_ar,
                'category': cat,
                'is_quantitative': quant,
                'unit_label_ar': unit,
                'is_active': True,
                'description': f'Smart Diagnostics module: {name_en}',
            },
        )

    # 2. Premium Plan
    Plan.objects.update_or_create(
        slug='premium_diagnostics',
        defaults={
            'name': 'Premium Diagnostics — التشخيص الذكي',
            'industry': 'automotive',
            'monthly_price': 6000,
            'max_branches': 3,
            'max_users': 10,
            'max_treasuries': 2,
            'features': [
                'بيانات لحظية من جهاز OBD2',
                'خطط فحص موجَّهة (ISTA)',
                'البحث الذكي عن قطع الغيار الأصلية',
                'حصة شهرية: 200 فحص API خارجي',
            ],
            'entitlements': {
                'diagnostics_live_data': {'enabled': True},
                'diagnostics_guided_tests': {'enabled': True},
                'diagnostics_smart_parts_finder': {'enabled': True},
                'diagnostics_external_api_scans': {'enabled': True, 'monthly_limit': 200},
            },
            'is_active': True,
            'sort_order': 100,
        },
    )

    # 3. API cost rates
    APICostRate.objects.update_or_create(
        provider='carmd', endpoint='dtc_lookup',
        defaults={'cost_usd': '0.05', 'is_active': True, 'note': 'Pay-per-call DTC lookup'},
    )
    APICostRate.objects.update_or_create(
        provider='nhtsa', endpoint='vin_decode',
        defaults={'cost_usd': '0.00', 'is_active': True, 'note': 'Free public NHTSA vPIC'},
    )
    APICostRate.objects.update_or_create(
        provider='mock', endpoint='dtc_lookup',
        defaults={'cost_usd': '0.05', 'is_active': True, 'note': 'Dev/test mock provider'},
    )

    # 4. DTC seed
    for code, system, severity, short, full, parts in DTC_SEED:
        DTCDefinition.objects.update_or_create(
            code=code,
            defaults={
                'system': system,
                'severity': severity,
                'short_description': short,
                'full_description': full,
                'likely_oem_parts': parts,
                'source': 'community',
                'is_generic': True,
                'guided_steps': [
                    {'step': 1, 'title': 'فحص بصري', 'action': 'افحص توصيلات الحساس المرتبط بالكود.', 'expected': 'لا يوجد قطع/تآكل.'},
                    {'step': 2, 'title': 'قراءة الـ Live Data', 'action': 'راقب القراءة المرتبطة وقت الـ idle.', 'expected': 'القيمة ضمن النطاق الطبيعي.'},
                    {'step': 3, 'title': 'استبدال محتمل', 'action': 'لو القراءة شاذة، جرّب استبدال أقرب OEM part.', 'expected': 'الكود لا يعود بعد الإصلاح.'},
                ],
            },
        )


def unseed(apps, schema_editor):
    Feature = apps.get_model('clients', 'Feature')
    Plan = apps.get_model('clients', 'Plan')
    DTCDefinition = apps.get_model('diagnostics_catalog', 'DTCDefinition')
    APICostRate = apps.get_model('diagnostics_catalog', 'APICostRate')

    Plan.objects.filter(slug='premium_diagnostics').delete()
    Feature.objects.filter(code__startswith='diagnostics_').delete()
    APICostRate.objects.filter(provider__in=['carmd', 'nhtsa', 'mock']).delete()
    DTCDefinition.objects.filter(source='community').delete()


class Migration(migrations.Migration):
    dependencies = [
        ('diagnostics_catalog', '0001_initial'),
        ('clients', '0037_tenantsubscription_diag_api_last_refill_at_and_more'),
    ]
    operations = [migrations.RunPython(seed, unseed)]
