"""📈 بنود بورصة أسعار المطابع المبدئية — مواصفة ثابتة لكل بند عشان الأسعار تتقارن.

بتتعدّل/تتزوّد من الأدمن («بنود بورصة أسعار المطابع»). الإعادة مابتكرّرش (code ثابت).
"""
from django.db import migrations

# (code, kind, icon, name, name_en, spec, spec_en, unit, unit_en)
SPECS = [
    ('cards-1000', 'product', 'fa-id-card', 'كروت شخصية', 'Business cards',
     'كوشيه ٣٥٠ جم، وش وضهر، سلوفان مط', '350gsm coated, double-sided, matte lamination', '١٠٠٠ كارت', '1,000 cards'),
    ('banner-flex-m2', 'product', 'fa-flag', 'بنر فليكس', 'Flex banner',
     'فليكس ٤٤٠ جم، طباعة سولفنت، من غير تشطيب', '440gsm flex, solvent print, no finishing', 'المتر المربع', 'per m²'),
    ('rollup-85', 'product', 'fa-scroll', 'ستاند رول أب', 'Roll-up stand',
     '٨٥×٢٠٠ سم، ستاند ألومنيوم + طباعة + شنطة', '85×200 cm aluminium stand + print + bag', 'الواحد', 'each'),
    ('flyer-a5-1000', 'product', 'fa-file-image', 'فلاير A5', 'A5 flyer',
     'كوشيه ١٥٠ جم، وش وضهر', '150gsm coated, double-sided', '١٠٠٠ فلاير', '1,000 flyers'),
    ('flyer-a4-1000', 'product', 'fa-file-lines', 'فلاير A4', 'A4 flyer',
     'كوشيه ١٥٠ جم، وش واحد', '150gsm coated, single-sided', '١٠٠٠ فلاير', '1,000 flyers'),
    ('brochure-a4-3fold', 'product', 'fa-book-open', 'بروشور A4 تلت طيات', 'A4 tri-fold brochure',
     'كوشيه ١٧٠ جم، وش وضهر، طي', '170gsm coated, double-sided, folded', '١٠٠٠ بروشور', '1,000 brochures'),
    ('sticker-vinyl-m2', 'product', 'fa-note-sticky', 'ستيكر فينيل', 'Vinyl sticker',
     'فينيل لامع، طباعة + قص كونتور', 'Glossy vinyl, print + contour cut', 'المتر المربع', 'per m²'),
    ('label-roll-1000', 'product', 'fa-tags', 'ليبل رول', 'Roll labels',
     'ورق لاصق ٥×٥ سم، ٤ لون', '5×5 cm paper labels, 4-colour', '١٠٠٠ ليبل', '1,000 labels'),
    ('letterhead-a4-1000', 'product', 'fa-envelope-open-text', 'ورق مراسلات A4', 'A4 letterhead',
     'ورق ١٠٠ جم، وش واحد ٤ لون', '100gsm paper, single-sided 4-colour', '١٠٠٠ ورقة', '1,000 sheets'),
    ('envelope-dl-1000', 'product', 'fa-envelope', 'ظروف DL', 'DL envelopes',
     'ظرف ١١×٢٢ سم مطبوع لوجو', '11×22 cm, printed logo', '١٠٠٠ ظرف', '1,000 envelopes'),
    ('notepad-a5-100', 'product', 'fa-book', 'بلوك نوت A5', 'A5 notepad',
     '٥٠ ورقة ٨٠ جم، غلاف كوشيه', '50 sheets 80gsm, coated cover', '١٠٠ بلوك', '100 pads'),
    ('mug-print-50', 'product', 'fa-mug-hot', 'طباعة كوبايات', 'Printed mugs',
     'كوباية سيراميك بيضا، طباعة سبليميشن', 'White ceramic mug, sublimation print', '٥٠ كوباية', '50 mugs'),
    ('tshirt-print-50', 'product', 'fa-shirt', 'طباعة تيشيرتات', 'Printed T-shirts',
     'تيشيرت قطن، طباعة DTF وش واحد', 'Cotton T-shirt, single-side DTF print', '٥٠ تيشيرت', '50 shirts'),
    ('invoice-book-a5', 'product', 'fa-receipt', 'دفتر فواتير A5', 'A5 invoice book',
     '٥٠ نسخة × ٢، كربونلس، ترقيم', '50 × 2 carbonless, numbered', '١٠ دفاتر', '10 books'),
    ('pvc-sign-m2', 'product', 'fa-sign-hanging', 'لافتة فوم بورد', 'Foam board sign',
     'فوم ٥ مم + ستيكر مطبوع', '5 mm foam + printed vinyl', 'المتر المربع', 'per m²'),
    # الخامات (سعر الشراء)
    ('paper-coated-350', 'material', 'fa-layer-group', 'ورق كوشيه ٣٥٠ جم', 'Coated paper 350gsm',
     'مقاس ٧٠×١٠٠', '70×100 cm', 'الفرخ', 'per sheet'),
    ('paper-coated-150', 'material', 'fa-layer-group', 'ورق كوشيه ١٥٠ جم', 'Coated paper 150gsm',
     'مقاس ٧٠×١٠٠', '70×100 cm', 'الفرخ', 'per sheet'),
    ('paper-offset-80', 'material', 'fa-layer-group', 'ورق أبيض ٨٠ جم', 'Offset paper 80gsm',
     'مقاس ٧٠×١٠٠', '70×100 cm', 'الرزمة (٥٠٠ فرخ)', 'per ream (500)'),
    ('flex-roll-440', 'material', 'fa-scroll', 'رول فليكس ٤٤٠ جم', 'Flex roll 440gsm',
     'عرض ٣.٢ متر', '3.2 m wide', 'المتر المربع', 'per m²'),
    ('vinyl-roll', 'material', 'fa-scroll', 'رول فينيل لامع', 'Glossy vinyl roll',
     'عرض ١.٢٧ متر', '1.27 m wide', 'المتر المربع', 'per m²'),
    ('ink-solvent-l', 'material', 'fa-droplet', 'حبر سولفنت', 'Solvent ink',
     'أي لون', 'any colour', 'اللتر', 'per litre'),
]


def seed(apps, schema_editor):
    from django.db import connection
    if getattr(connection, 'schema_name', 'public') != 'public':
        return
    PriceSpec = apps.get_model('clients', 'PriceSpec')
    for i, (code, kind, icon, name, name_en, spec, spec_en, unit, unit_en) in enumerate(SPECS):
        PriceSpec.objects.get_or_create(code=code, defaults=dict(
            sector='printing', kind=kind, icon=icon, name=name, name_en=name_en, spec=spec, spec_en=spec_en,
            unit=unit, unit_en=unit_en, sort=i))


class Migration(migrations.Migration):
    dependencies = [('clients', '0130_price_board')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
