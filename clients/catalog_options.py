"""🎨 اختيارات منتجات المطابع اللي العميل بيحددها: المقاس / اللون / الشكل / التشطيب.

* الاختيارات حسب نوع المنتج (كارت، كوباية، تيشيرت…) — النوع بيتعرف من اسم المنتج.
* المنتج يقدر يبقى ليه اختيارات خاصة في CatalogItem.options (من الأدمن).
"""
from django.utils.translation import get_language

# المفتاح ⇒ (عربي، إنجليزي، hex)
COLORS = {
    'white': ('أبيض', 'White', '#FFFFFF'), 'black': ('أسود', 'Black', '#1F2937'),
    'red': ('أحمر', 'Red', '#DC2626'), 'navy': ('كحلي', 'Navy', '#1E3A8A'),
    'sky': ('لبني', 'Sky blue', '#38BDF8'), 'green': ('أخضر', 'Green', '#16A34A'),
    'yellow': ('أصفر', 'Yellow', '#FACC15'), 'orange': ('برتقالي', 'Orange', '#F97316'),
    'pink': ('بينك', 'Pink', '#EC4899'), 'purple': ('بنفسجي', 'Purple', '#8B5CF6'),
    'grey': ('رمادي', 'Grey', '#9CA3AF'), 'beige': ('بيج', 'Beige', '#E7D8C0'),
    'kraft': ('كرافت', 'Kraft', '#C69C6D'), 'gold': ('دهبي', 'Gold', '#D4AF37'),
    'silver': ('فضي', 'Silver', '#C0C7CF'), 'maroon': ('نبيتي', 'Maroon', '#7F1D1D'),
}

_ALL = ['white', 'black', 'red', 'navy', 'sky', 'green', 'yellow', 'orange', 'pink', 'purple', 'grey', 'maroon']
_PAPER = ['white', 'beige', 'kraft', 'black']
_METAL = ['silver', 'gold', 'black', 'navy', 'red']

# نوع المنتج ⇒ الاختيارات. كل اختيار (عربي، إنجليزي).
OPTIONS = {
    'card': {
        'sizes': [('٩×٥ سم (قياسي)', '9×5 cm (standard)'), ('٨.٥×٥.٥ سم', '8.5×5.5 cm'), ('مربع ٦.٥×٦.٥ سم', 'Square 6.5×6.5 cm')],
        'colors': _PAPER + ['navy', 'red', 'gold'],
        'shapes': [('حواف عادية', 'Square corners'), ('حواف مدوّرة', 'Rounded corners'), ('مقصوص على الشكل', 'Die-cut shape')],
        'finishes': [('مط', 'Matte'), ('لامع', 'Gloss'), ('سبوت يوفي', 'Spot UV'), ('تذهيب', 'Gold foil'), ('بارز (إمبوس)', 'Embossed')],
    },
    'sheet': {
        'sizes': [('A5', 'A5'), ('A4', 'A4'), ('A3', 'A3'), ('A2', 'A2')],
        'colors': ['white', 'beige', 'kraft'],
        'shapes': [('وش واحد', 'Single-sided'), ('وشين', 'Double-sided')],
        'finishes': [('كوشيه لامع', 'Gloss coated'), ('كوشيه مط', 'Matte coated'), ('سلوفان', 'Laminated')],
    },
    'brochure': {
        'sizes': [('A4 (٣ طيات)', 'A4 (tri-fold)'), ('A4 (طيتين)', 'A4 (bi-fold)'), ('A3 (طيتين)', 'A3 (bi-fold)')],
        'colors': ['white', 'beige'],
        'finishes': [('لامع', 'Gloss'), ('مط', 'Matte')],
    },
    'notebook': {
        'sizes': [('A6', 'A6'), ('A5', 'A5'), ('B5', 'B5'), ('A4', 'A4')],
        'colors': ['black', 'navy', 'maroon', 'kraft', 'green', 'grey', 'red'],
        'shapes': [('سلك', 'Spiral'), ('خياطة', 'Sewn'), ('جلد بكبسون', 'Leather with clasp')],
        'finishes': [('طباعة', 'Printed'), ('حفر ليزر', 'Laser engraved'), ('تذهيب', 'Foil')],
    },
    'calendar': {
        'sizes': [('مكتب', 'Desk'), ('حيطة A3', 'Wall A3'), ('حيطة A2', 'Wall A2')],
        'colors': ['white', 'kraft', 'black'],
        'shapes': [('١٢ ورقة', '12 pages'), ('٦ ورقات', '6 pages'), ('ورقة واحدة', 'Single sheet')],
    },
    'mug': {
        'sizes': [('١١ أونص', '11 oz'), ('١٥ أونص', '15 oz')],
        'colors': ['white', 'black', 'red', 'navy', 'pink', 'yellow', 'green', 'sky'],
        'shapes': [('كلاسيك', 'Classic'), ('مخروطي', 'Conical'), ('مج سحري', 'Magic mug')],
        'finishes': [('لامع', 'Gloss'), ('مط', 'Matte')],
    },
    'tumbler': {
        'sizes': [('٣٥٠ مل', '350 ml'), ('٥٠٠ مل', '500 ml'), ('٧٥٠ مل', '750 ml')],
        'colors': ['silver', 'black', 'navy', 'white', 'pink', 'green'],
        'finishes': [('حفر ليزر', 'Laser engraved'), ('طباعة UV', 'UV print')],
    },
    'pen': {
        'colors': ['navy', 'black', 'red', 'white', 'silver', 'gold', 'green', 'orange', 'sky'],
        'shapes': [('بلاستيك', 'Plastic'), ('معدن', 'Metal'), ('تاتش', 'Stylus')],
        'finishes': [('طباعة', 'Printed'), ('حفر ليزر', 'Laser engraved')],
    },
    'keychain': {
        'colors': ['black', 'kraft', 'silver', 'gold', 'red', 'navy'],
        'shapes': [('دايرة', 'Round'), ('مستطيل', 'Rectangle'), ('على شكل اللوجو', 'Logo shape')],
        'finishes': [('جلد', 'Leather'), ('أكريليك', 'Acrylic'), ('معدن', 'Metal')],
    },
    'gadget': {
        'colors': ['white', 'black', 'navy', 'silver', 'red'],
        'shapes': [('مستطيل', 'Rectangle'), ('على شكل كارت', 'Card shape')],
    },
    'shield': {
        'sizes': [('١٥ سم', '15 cm'), ('٢٠ سم', '20 cm'), ('٣٠ سم', '30 cm')],
        'colors': ['navy', 'black', 'maroon', 'gold', 'silver'],
        'finishes': [('أكريليك', 'Acrylic'), ('خشب', 'Wood'), ('كريستال', 'Crystal')],
    },
    'giftbox': {
        'sizes': [('صغير', 'Small'), ('وسط', 'Medium'), ('كبير', 'Large')],
        'colors': ['black', 'navy', 'kraft', 'maroon', 'white', 'gold'],
        'finishes': [('شريطة ساتان', 'Satin ribbon'), ('تذهيب اللوجو', 'Foil logo')],
    },
    'tshirt': {
        'sizes': [('S', 'S'), ('M', 'M'), ('L', 'L'), ('XL', 'XL'), ('2XL', '2XL'), ('3XL', '3XL')],
        'colors': _ALL,
        'shapes': [('قصة عادية', 'Regular fit'), ('أوفر سايز', 'Oversized'), ('بولو', 'Polo'), ('هودي', 'Hoodie')],
        'finishes': [('طباعة DTF', 'DTF print'), ('سيلك سكرين', 'Screen print'), ('تطريز', 'Embroidery')],
    },
    'cap': {
        'colors': ['black', 'navy', 'white', 'red', 'beige', 'green', 'grey'],
        'shapes': [('كاب عادي', 'Baseball cap'), ('تراكر (شبك)', 'Trucker mesh'), ('باكت', 'Bucket hat')],
        'finishes': [('تطريز', 'Embroidery'), ('طباعة', 'Print')],
    },
    'tote': {
        'sizes': [('٣٥×٤٠ سم', '35×40 cm'), ('٤٠×٤٥ سم', '40×45 cm')],
        'colors': ['beige', 'white', 'black', 'navy', 'red', 'green'],
        'finishes': [('سيلك سكرين', 'Screen print'), ('DTF', 'DTF')],
    },
    'box': {
        'sizes': [('صغير', 'Small'), ('وسط', 'Medium'), ('كبير', 'Large'), ('مقاس خاص', 'Custom size')],
        'colors': ['white', 'kraft', 'black', 'red', 'navy', 'pink'],
        'shapes': [('علبة مربعة', 'Square box'), ('علبة بغطا', 'Lid box'), ('درج', 'Drawer box')],
        'finishes': [('لامع', 'Gloss'), ('مط', 'Matte'), ('تذهيب', 'Foil')],
    },
    'bag': {
        'sizes': [('صغير', 'Small'), ('وسط', 'Medium'), ('كبير', 'Large')],
        'colors': ['kraft', 'white', 'black', 'navy', 'red', 'pink'],
        'shapes': [('يد مبرومة', 'Twisted handle'), ('يد مقصوصة', 'Die-cut handle'), ('يد شريطة', 'Ribbon handle')],
    },
    'sticker': {
        'sizes': [('٥ سم', '5 cm'), ('٧ سم', '7 cm'), ('١٠ سم', '10 cm'), ('مقاس خاص', 'Custom size')],
        'colors': ['white', 'silver', 'gold', 'black', 'yellow'],
        'shapes': [('دايرة', 'Circle'), ('مربع', 'Square'), ('على الشكل (دايكت)', 'Die-cut'), ('رول', 'Roll')],
        'finishes': [('لامع', 'Gloss'), ('مط', 'Matte'), ('شفاف', 'Clear'), ('هولوجرام', 'Holographic')],
    },
    'rollup': {
        'sizes': [('٨٥×٢٠٠ سم', '85×200 cm'), ('١٠٠×٢٠٠ سم', '100×200 cm'), ('١٢٠×٢٠٠ سم', '120×200 cm'), ('مقاس خاص', 'Custom size')],
        'colors': ['white', 'navy', 'black', 'red', 'green', 'orange'],
        'shapes': [('رول أب', 'Roll-up'), ('بنر فليكس', 'Flex banner'), ('باك دروب', 'Backdrop')],
    },
    'sign': {
        'sizes': [('١ متر', '1 m'), ('٢ متر', '2 m'), ('٣ متر', '3 m'), ('مقاس خاص', 'Custom size')],
        'colors': ['navy', 'black', 'red', 'white', 'green', 'gold'],
        'shapes': [('لايت بوكس', 'Light box'), ('حروف بارزة', 'Raised letters'), ('لافتة أكريليك', 'Acrylic plate')],
        'finishes': [('إضاءة LED', 'LED lit'), ('من غير إضاءة', 'Unlit')],
    },
    'badge': {
        'colors': ['white', 'navy', 'black', 'red', 'green'],
        'shapes': [('بادج بشريطة', 'Lanyard badge'), ('بادج دبوس', 'Pin badge')],
    },
    'screen': {
        'sizes': [('بوست مربع', 'Square post'), ('ستوري', 'Story'), ('غلاف', 'Cover'), ('لوجو + هوية', 'Logo + identity')],
        'colors': ['navy', 'black', 'red', 'green', 'purple', 'orange', 'pink', 'sky'],
        'shapes': [('مودرن', 'Modern'), ('كلاسيك', 'Classic'), ('مرح', 'Playful'), ('فخم', 'Luxury')],
    },
    'invite': {
        'sizes': [('١٥×٢١ سم', '15×21 cm'), ('مربع ١٥ سم', 'Square 15 cm'), ('١٠×١٥ سم', '10×15 cm')],
        'colors': ['white', 'beige', 'pink', 'navy', 'black', 'gold'],
        'finishes': [('تذهيب', 'Gold foil'), ('ليزر كت', 'Laser cut'), ('شمع', 'Wax seal')],
    },
    'folder': {
        'sizes': [('A4', 'A4'), ('A5', 'A5')],
        'colors': ['navy', 'black', 'white', 'kraft', 'red'],
        'finishes': [('لامع', 'Gloss'), ('مط', 'Matte'), ('سبوت يوفي', 'Spot UV')],
    },
    'stamp': {
        'sizes': [('دايرة ٤ سم', 'Round 4 cm'), ('مستطيل ٦×٢ سم', 'Rectangle 6×2 cm')],
        'colors': ['navy', 'red', 'black', 'green', 'purple'],
        'shapes': [('أوتوماتيك', 'Self-inking'), ('خشب', 'Wooden handle')],
    },
    # ───── 🆕 أنواع منتجات الدفعة التانية (clients/catalog_seed_more.py) ─────
    'photo': {
        'sizes': [('٢٠×٣٠ سم', '20×30 cm'), ('٣٠×٤٠ سم', '30×40 cm'), ('٤٠×٦٠ سم', '40×60 cm'), ('٥٠×٧٠ سم', '50×70 cm'),
                  ('٦٠×٩٠ سم', '60×90 cm'), ('مقاس خاص', 'Custom size')],
        'colors': ['black', 'white', 'kraft', 'gold', 'silver'],
        'shapes': [('كانفاس', 'Canvas'), ('برواز خشب', 'Wood frame'), ('أكريليك', 'Acrylic'), ('خشب طبيعي', 'Natural wood'),
                   ('ورق فوتو', 'Photo paper')],
        'finishes': [('لامع', 'Gloss'), ('مط', 'Matte')],
    },
    'album': {
        'sizes': [('٢٠×٢٠ سم', '20×20 cm'), ('٢٠×٣٠ سم', '20×30 cm'), ('٣٠×٣٠ سم', '30×30 cm')],
        'colors': ['white', 'black', 'beige', 'navy', 'maroon'],
        'shapes': [('٢٠ صفحة', '20 pages'), ('٤٠ صفحة', '40 pages'), ('٦٠ صفحة', '60 pages')],
        'finishes': [('غلاف مقوى مطبوع', 'Printed hardcover'), ('غلاف جلد', 'Leather cover'), ('غلاف قطيفة', 'Velvet cover')],
    },
    'cup': {
        'sizes': [('٤ أونص', '4 oz'), ('٨ أونص', '8 oz'), ('١٢ أونص', '12 oz'), ('١٦ أونص', '16 oz')],
        'colors': ['white', 'kraft', 'black', 'red', 'navy', 'green'],
        'shapes': [('ورق سنجل وول', 'Single-wall paper'), ('ورق دابل وول', 'Double-wall paper'), ('بلاستيك شفاف', 'Clear plastic')],
        'finishes': [('بغطا', 'With lid'), ('من غير غطا', 'No lid')],
    },
    'wrap': {
        'sizes': [('٢٥×٢٥ سم', '25×25 cm'), ('٣٥×٣٥ سم', '35×35 cm'), ('٥٠×٧٠ سم', '50×70 cm')],
        'colors': ['white', 'kraft', 'black', 'pink', 'gold'],
        'finishes': [('ورق زبدة', 'Greaseproof'), ('تيشو', 'Tissue'), ('ورق هدايا', 'Gift wrap')],
    },
    'napkin': {
        'sizes': [('كوكتيل ٢٥ سم', 'Cocktail 25 cm'), ('سفرة ٣٣ سم', 'Dinner 33 cm'), ('سفرة ٤٠ سم', 'Dinner 40 cm')],
        'colors': ['white', 'black', 'kraft', 'red', 'navy'],
        'shapes': [('طبقة واحدة', '1 ply'), ('طبقتين', '2 ply')],
    },
    'stand': {
        'sizes': [('A6', 'A6'), ('A5', 'A5'), ('A4', 'A4')],
        'colors': ['white', 'black', 'silver', 'gold'],
        'shapes': [('ستاند T', 'T-stand'), ('ستاند L', 'L-stand'), ('مطوي ورق', 'Folded card')],
    },
    'flag': {
        'sizes': [('٢.٥ متر', '2.5 m'), ('٣.٥ متر', '3.5 m'), ('٤.٥ متر', '4.5 m')],
        'colors': ['red', 'navy', 'black', 'green', 'orange', 'yellow', 'white'],
        'shapes': [('ريشة', 'Feather'), ('قطرة', 'Teardrop'), ('مستطيل', 'Rectangle')],
        'finishes': [('قاعدة صليب', 'Cross base'), ('قاعدة مية', 'Water base'), ('خابور أرض', 'Ground spike')],
    },
    'neon': {
        'sizes': [('٥٠ سم', '50 cm'), ('٨٠ سم', '80 cm'), ('١٢٠ سم', '120 cm')],
        'colors': ['pink', 'sky', 'white', 'red', 'yellow', 'green', 'purple', 'orange'],
        'finishes': [('داخلي', 'Indoor'), ('خارجي ضد المية', 'Outdoor waterproof')],
    },
    'display': {
        'sizes': [('كاونتر صغير', 'Small counter'), ('أرضي ١٥٠ سم', 'Floor 150 cm'), ('مقاس خاص', 'Custom size')],
        'colors': ['white', 'kraft', 'black', 'red', 'navy'],
        'shapes': [('برف واحد', '1 shelf'), ('٣ أرفف', '3 shelves'), ('كتلة (دمي)', 'Cut-out')],
    },
    'magnet': {
        'sizes': [('٥×٧ سم', '5×7 cm'), ('٧×١٠ سم', '7×10 cm'), ('مقاس خاص', 'Custom size')],
        'colors': ['white', 'black', 'red', 'navy', 'yellow'],
        'shapes': [('مستطيل', 'Rectangle'), ('دايرة', 'Circle'), ('على الشكل', 'Die-cut')],
        'finishes': [('لامع', 'Gloss'), ('مط', 'Matte')],
    },
    'clock': {
        'sizes': [('٢٥ سم', '25 cm'), ('٣٠ سم', '30 cm'), ('٤٠ سم', '40 cm')],
        'colors': ['white', 'black', 'silver', 'kraft', 'navy'],
        'shapes': [('دايرة', 'Round'), ('مربع', 'Square')],
        'finishes': [('بلاستيك', 'Plastic'), ('خشب', 'Wood'), ('معدن', 'Metal')],
    },
    'umbrella': {
        'sizes': [('شمسية يد', 'Hand umbrella'), ('شمسية جولف كبيرة', 'Large golf umbrella'), ('شمسية بحر', 'Beach umbrella')],
        'colors': ['black', 'navy', 'red', 'white', 'yellow', 'green'],
    },
    'cushion': {
        'sizes': [('٤٠×٤٠ سم', '40×40 cm'), ('٤٥×٤٥ سم', '45×45 cm'), ('٣٠×٥٠ سم', '30×50 cm')],
        'colors': ['white', 'beige', 'grey', 'black', 'pink', 'navy'],
        'finishes': [('قطيفة', 'Velvet'), ('كتان', 'Linen'), ('ساتان', 'Satin')],
    },
    'puzzle': {
        'sizes': [('١٢٠ قطعة', '120 pieces'), ('٢٥٢ قطعة', '252 pieces'), ('٥٠٠ قطعة', '500 pieces'), ('١٠٠٠ قطعة', '1000 pieces')],
        'colors': ['white', 'black', 'kraft'],
        'shapes': [('كرتون', 'Cardboard'), ('خشب', 'Wood')],
    },
    'backpack': {
        'colors': ['black', 'grey', 'navy', 'maroon', 'beige'],
        'shapes': [('شنطة ظهر', 'Backpack'), ('شنطة لاب توب', 'Laptop bag'), ('جراب لاب توب', 'Laptop sleeve')],
        'finishes': [('تطريز', 'Embroidery'), ('طباعة', 'Print'), ('لوجو جلد', 'Leather patch')],
    },
    'apron': {
        'sizes': [('مقاس واحد', 'One size'), ('M', 'M'), ('L', 'L'), ('XL', 'XL'), ('2XL', '2XL')],
        'colors': ['black', 'white', 'navy', 'maroon', 'beige', 'grey'],
        'shapes': [('مريلة', 'Apron'), ('جاكت شيف', 'Chef jacket'), ('طاقية شيف', 'Chef hat')],
        'finishes': [('تطريز', 'Embroidery'), ('طباعة', 'Print')],
    },
    'jacket': {
        'sizes': [('S', 'S'), ('M', 'M'), ('L', 'L'), ('XL', 'XL'), ('2XL', '2XL'), ('3XL', '3XL')],
        'colors': ['black', 'navy', 'grey', 'maroon', 'green', 'beige'],
        'shapes': [('جاكت شتوي', 'Winter jacket'), ('بليزر', 'Blazer'), ('فيست', 'Gilet')],
        'finishes': [('تطريز', 'Embroidery'), ('طباعة', 'Print')],
    },
    'beanie': {
        'colors': ['black', 'navy', 'grey', 'maroon', 'beige', 'red', 'green'],
        'shapes': [('طاقية تريكو', 'Knitted beanie'), ('طاقية بكورة', 'Pom-pom beanie'), ('كوفية', 'Scarf')],
        'finishes': [('تطريز', 'Embroidery'), ('باتش', 'Patch')],
    },
    'booklet': {
        'sizes': [('A5', 'A5'), ('A4', 'A4')],
        'colors': ['white', 'beige'],
        'shapes': [('دبوس', 'Stapled'), ('سلك', 'Spiral'), ('كعب لزق', 'Perfect bound'), ('تجليد جلد', 'Leather binding')],
        'finishes': [('ألوان', 'Full color'), ('أبيض وأسود', 'Black & white')],
    },
    'certificate': {
        'sizes': [('A4', 'A4'), ('A5', 'A5'), ('A3', 'A3')],
        'colors': ['white', 'beige', 'gold'],
        'finishes': [('تذهيب', 'Gold foil'), ('ختم بارز', 'Embossed seal'), ('برواز', 'Framed'), ('من غير', 'Plain')],
    },
    'idcard': {
        'colors': ['white', 'navy', 'red', 'green', 'black', 'sky'],
        'shapes': [('عرضي', 'Landscape'), ('طولي', 'Portrait')],
        'finishes': [('بشريطة وجراب', 'With lanyard & holder'), ('كارت بس', 'Card only')],
    },
    'envelope': {
        'sizes': [('DL خطاب', 'DL letter'), ('A5 (C5)', 'A5 (C5)'), ('A4 (C4)', 'A4 (C4)')],
        'colors': ['white', 'kraft', 'beige', 'black'],
        'shapes': [('بشباك', 'Window'), ('من غير شباك', 'No window')],
    },
    'carbon': {
        'sizes': [('A6', 'A6'), ('A5', 'A5'), ('A4', 'A4')],
        'colors': ['white', 'yellow', 'pink', 'sky'],
        'shapes': [('نسختين', '2 copies'), ('٣ نسخ', '3 copies')],
        'finishes': [('بالترقيم', 'Numbered'), ('من غير ترقيم', 'Unnumbered')],
    },
    'notepad': {
        'sizes': [('٧.٥×٧.٥ سم', '7.5×7.5 cm'), ('A6', 'A6'), ('A5', 'A5')],
        'colors': ['yellow', 'white', 'pink', 'sky', 'green'],
        'shapes': [('ستيكي نوت', 'Sticky notes'), ('بلوك نوت عادي', 'Regular notepad')],
    },
    'tape': {
        'sizes': [('٤٨ مم', '48 mm'), ('٧٢ مم', '72 mm')],
        'colors': ['kraft', 'white', 'black', 'red', 'navy'],
    },
}

# 🆕 بالقسم الأول (أدق من الكلمة): قسم + كلمة (أو None) ⇒ نوع
_CATEGORY_RULES = [
    ('design', None, 'screen'), ('social', None, 'screen'),
    ('photo', 'ألبوم', 'album'), ('photo', None, 'photo'),
    ('cafe', 'أكواب', 'cup'), ('cafe', 'مناديل', 'napkin'), ('cafe', 'تيبل تنت', 'stand'), ('cafe', 'QR', 'stand'),
    ('cafe', 'تغليف', 'wrap'), ('cafe', 'شنط', 'bag'), ('cafe', 'علب', 'box'), ('cafe', None, 'sheet'),
]

# كلمة في اسم المنتج (عربي) ⇒ نوع المنتج. الأول اللي يطابق بيكسب.
_RULES = [
    # 🆕 الدفعة التانية (قبل القديم عشان «إعلانية/أكريليك/علبة» مايسحبوش النوع الغلط)
    ('أفيش', 'sheet'), ('بوكلت', 'booklet'), ('ملازم', 'booklet'), ('مشاريع تخرج', 'booklet'), ('شهادات تخرج', 'certificate'), ('شهادات وتقارير', 'certificate'),
    ('كارنيه', 'idcard'), ('ظروف مطبوعة', 'envelope'), ('كربون', 'carbon'), ('سندات', 'carbon'), ('استلام وتسليم', 'carbon'),
    ('بلوك نوت', 'notepad'), ('أقلام', 'pen'), ('ماجنت', 'magnet'), ('ساعات حيطة', 'clock'), ('شماسي', 'umbrella'),
    ('وسادات', 'cushion'), ('بازل', 'puzzle'), ('شنطة لاب توب', 'backpack'), ('كاسات', 'shield'), ('مرايل', 'apron'),
    ('جواكت', 'jacket'), ('طواقي', 'beanie'), ('علم', 'flag'), ('نيون', 'neon'), ('حروف بارزة أكريليك', 'sign'),
    ('لوحات أكريليك', 'sign'), ('ستاند عرض', 'display'), ('ورق تغليف', 'wrap'), ('شريط لاصق', 'tape'),
    ('زجاجة', 'tumbler'), ('مج سفر', 'tumbler'), ('حامل موبايل', 'gadget'), ('بانر', 'rollup'), ('خلفيات', 'rollup'),
    # الأدق الأول: «نتيجة… بالتصميم» نتيجة، «طقم كوبايات في علبة» كوباية، «ستيكر ختم» ستيكر
    ('نتيجة', 'calendar'), ('هودي', 'tshirt'), ('كوباي', 'mug'), ('قلم', 'pen'), ('ستيكر', 'sticker'),
    ('ختم', 'stamp'), ('أختام', 'stamp'), ('فولدر', 'folder'), ('ملفات', 'folder'), ('ليترهيد', 'sheet'),
    ('روشت', 'sheet'), ('ضمان', 'card'), ('بادج', 'badge'), ('دعوات', 'invite'), ('دعوة', 'invite'),
    ('توزيعات', 'sticker'), ('ستوري', 'screen'), ('سوشيال', 'screen'), ('فيسبوك', 'screen'), ('إعلان', 'screen'),
    ('لوجو احترافي', 'screen'), ('هوية', 'screen'), ('تصميم', 'screen'), ('فيكتور', 'screen'),
    ('أكياس', 'bag'), ('شنط ورق', 'bag'), ('علب', 'box'), ('ستيكر', 'sticker'), ('لابلز', 'sticker'),
    ('رول أب', 'rollup'), ('بنر', 'rollup'), ('باك دروب', 'rollup'), ('يافطة', 'sign'), ('لافتات', 'sign'),
    ('فينيل', 'sticker'), ('فيست', 'tshirt'), ('سكراب', 'tshirt'), ('هودي', 'tshirt'), ('يونيفورم', 'tshirt'),
    ('تيشيرت', 'tshirt'), ('كاب', 'cap'), ('شنط قماش', 'tote'), ('حفر ليزر', 'shield'), ('دروع', 'shield'),
    ('بوكس', 'giftbox'), ('ماوس باد', 'gadget'), ('فلاشات', 'gadget'), ('باور بانك', 'gadget'),
    ('ميداليات', 'keychain'), ('قلم', 'pen'), ('أقلام', 'pen'), ('ترمس', 'tumbler'), ('كوباي', 'mug'),
    ('مج', 'mug'), ('نتيجة', 'calendar'), ('كراسات', 'notebook'), ('دفاتر', 'notebook'), ('نوت بوك', 'notebook'),
    ('أجندة', 'notebook'), ('كشكول', 'notebook'), ('بوستر', 'sheet'), ('كتالوج', 'brochure'), ('بروشور', 'brochure'),
    ('منيو', 'sheet'), ('فلاير', 'sheet'), ('كروت', 'card'),
]
_CATEGORY_FALLBACK = {
    'cards': 'card', 'flyers': 'sheet', 'notebooks': 'notebook', 'mugs': 'mug', 'pens': 'pen', 'gifts': 'giftbox',
    'apparel': 'tshirt', 'banners': 'rollup', 'stickers': 'sticker', 'packaging': 'box', 'design': 'screen',
    'social': 'screen', 'events': 'invite', 'office': 'sheet', 'photo': 'photo', 'school': 'sheet', 'cafe': 'sheet',
}

def product_type(title, category):
    for cat, word, kind in _CATEGORY_RULES:
        if cat == category and (word is None or word in (title or '')):
            return kind
    for word, kind in _RULES:
        if word in (title or ''):
            return kind
    return _CATEGORY_FALLBACK.get(category, 'card')


def _is_en():
    return (get_language() or 'ar').startswith('en')


def item_options(item):
    """الاختيارات باللغة الحالية: {'sizes': [(قيمة، عرض)], 'colors': [(مفتاح، اسم، hex)], ...}.
    القيمة اللي بتتبعت دايماً بالعربي (عشان صاحب المنصة يقراها)، والعرض حسب اللغة."""
    kind = product_type(item.title, item.category)
    raw = dict(OPTIONS.get(kind, {}))
    raw.update({k: v for k, v in (item.options or {}).items() if v})
    en = _is_en()
    out = {}
    for group in ('sizes', 'shapes', 'finishes'):
        out[group] = [(ar, en_ if en else ar) for ar, en_ in raw.get(group, [])]
    out['colors'] = [(k, COLORS[k][1] if en else COLORS[k][0], COLORS[k][2]) for k in raw.get('colors', []) if k in COLORS]
    return out
