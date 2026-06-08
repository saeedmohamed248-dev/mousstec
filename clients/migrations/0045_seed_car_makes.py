from django.db import migrations


CAR_MAKES = [
    # (name, slug, sort_order)
    ('BMW',         'bmw',         10),
    ('Mercedes',    'mercedes',    20),
    ('Audi',        'audi',        30),
    ('Mini',        'mini',        40),
    ('Volkswagen',  'volkswagen',  50),
    ('Porsche',     'porsche',     60),
    ('Toyota',      'toyota',      70),
    ('Honda',       'honda',       80),
    ('Hyundai',     'hyundai',     90),
    ('Kia',         'kia',        100),
    ('Nissan',      'nissan',     110),
    ('Mitsubishi',  'mitsubishi', 120),
    ('Ford',        'ford',       130),
    ('Chevrolet',   'chevrolet',  140),
    ('Jeep',        'jeep',       150),
    ('Renault',     'renault',    160),
    ('Peugeot',     'peugeot',    170),
    ('Fiat',        'fiat',       180),
    ('Skoda',       'skoda',      190),
    ('Land Rover',  'land-rover', 200),
    ('Jaguar',      'jaguar',     210),
    ('Lexus',       'lexus',      220),
    ('Mazda',       'mazda',      230),
    ('Suzuki',      'suzuki',     240),
    ('Geely',       'geely',      250),
    ('MG',          'mg',         260),
    ('Chery',       'chery',      270),
    ('غير ذلك',     'other',      999),
]


def seed_car_makes(apps, schema_editor):
    PartCarMake = apps.get_model('clients', 'PartCarMake')
    for name, slug, order in CAR_MAKES:
        PartCarMake.objects.update_or_create(
            slug=slug, defaults={'name': name, 'sort_order': order, 'is_active': True},
        )


def unseed_car_makes(apps, schema_editor):
    PartCarMake = apps.get_model('clients', 'PartCarMake')
    PartCarMake.objects.filter(slug__in=[s for _, s, _ in CAR_MAKES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('clients', '0044_parts_marketplace'),
    ]
    operations = [
        migrations.RunPython(seed_car_makes, unseed_car_makes),
    ]
