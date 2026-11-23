"""🌙💼 الدفعة التالتة: منتجات المواسم — رمضان + هدايا الشركات لبداية السنة.

منفصلة عن PRINT_ITEMS و PRINT_ITEMS_MORE عمداً (نفس سبب الدفعة التانية): لو زوّدنا على القوايم القديمة
اختيارات الزرع بالـ random هتتغيّر والمتاجر القديمة هتتلخبط. هنا بنزوّد بس، بـ random لوحده.

كل منتج: (القسم، الاسم، الوصف، أقل كمية، الاسم EN، الوصف EN، أقل كمية EN، وصف الصورة للمولّد)
أكواد الصور بتكمّل بعد الدفعة التانية (p189، p190، …).
"""
import random

# القسم ⇒ (عربي، أيقونة، إنجليزي)
PRINT_CATEGORIES_SEASONAL = {
    'ramadan': ('منتجات رمضان', 'fa-moon', 'Ramadan products'),
    'corporate': ('هدايا الشركات للسنة الجديدة', 'fa-briefcase', 'New-year corporate gifts'),
}

PRINT_ITEMS_SEASONAL = [
    # ───────── 🌙 رمضان ─────────
    ('ramadan', 'إمساكية رمضان باسم محلك أو عيلتك', 'مواعيد الإمساك والإفطار لمحافظتك، ومعاها اسمك ولوجو محلك — بتفضل متعلّقة في البيت ٣٠ يوم.', '١٠٠ إمساكية',
     'Ramadan imsakiya with your name', 'Suhoor and iftar times for your city with your name and shop logo — it stays on the wall for 30 days.', '100 copies',
     'A printed Ramadan timetable poster on thick matte card with a decorative Islamic geometric border, crescent and lantern illustrations, hanging on a kitchen wall'),
    ('ramadan', 'إمساكية مغناطيس للتلاجة', 'إمساكية صغيرة مغناطيس تتلزق على التلاجة — دعاية لمحلك قدام العيلة كل يوم.', '٢٠٠ مغناطيس',
     'Fridge magnet imsakiya', 'A small magnetic Ramadan timetable for the fridge — your shop in front of the family every day.', '200 magnets',
     'A small rectangular printed fridge magnet with an Islamic pattern and crescent design stuck on a white fridge door'),
    ('ramadan', 'فانوس كرتون مقصوص ليزر', 'فانوس من كرتون مقوّى مقصوص ليزر بيتركّب بالإيد، وممكن عليه اسم الطفل أو لوجو الشركة.', '٥٠ فانوس',
     'Laser-cut cardboard lantern', 'A hand-assembled lantern of thick laser-cut card, with a child’s name or a company logo.', '50 lanterns',
     'A laser-cut card Ramadan lantern with intricate Islamic geometric cut-outs glowing with a warm LED light inside, on a wooden table at night'),
    ('ramadan', 'زينة رمضان مطبوعة (أعلام وهلال ونجوم)', 'سلاسل أعلام وهلال ونجوم مقصوصة بألوان رمضان — للبيوت والمحلات والمدارس.', '١٠ أطقم',
     'Printed Ramadan decorations set', 'Bunting, crescents and stars die-cut in Ramadan colors — for homes, shops and schools.', '10 sets',
     'Printed paper Ramadan bunting with crescents, stars and lanterns hanging across a cozy living room with warm lights'),
    ('ramadan', 'مفرش سفرة إفطار مطبوع', 'مفرش ورق أو بلاستيك مطبوع للسفرة الطويلة، ومعاه مفارش أطباق — لموائد الرحمن والعزومات والمطاعم.', '٥٠ متر',
     'Printed iftar table runner', 'Printed paper or plastic table cover and place mats — for charity iftars, family gatherings and restaurants.', '50 meters',
     'A long iftar table covered with a printed table runner and place mats with an Islamic arabesque pattern, dates, juice and plates at sunset light'),
    ('ramadan', 'بوكس تمر وهدية رمضان مطبوع', 'علبة مقوّاة مطبوعة بتصميم رمضاني فيها تقسيمات للتمر والمكسرات، وكارت إهداء باسمك.', '٥٠ علبة',
     'Printed Ramadan dates gift box', 'A printed rigid box with compartments for dates and nuts, plus a gift card with your name.', '50 boxes',
     'An open premium printed gift box with gold Islamic pattern and compartments full of dates and nuts, a small gift card beside it'),
    ('ramadan', 'كرتونة شنطة رمضان مطبوعة', 'كرتونة شنطة الخير بلوجو الجمعية أو الشركة، بتتشحن مفرودة وتتقفل من غير لزق.', '١٠٠ كرتونة',
     'Printed Ramadan charity carton', 'Ramadan food-box cartons with your charity or company logo, shipped flat and assembled without glue.', '100 cartons',
     'Stacked printed corrugated Ramadan food donation boxes with a crescent and lantern design, one open box showing rice, oil and sugar packs'),
    ('ramadan', 'شنط قماش لتوزيع رمضان', 'شنطة قماش بلوجو الشركة أو الجمعية — بتتوزّع فيها الشنط وبتفضل مع الناس بعد رمضان.', '١٠٠ شنطة',
     'Ramadan tote bags', 'Cloth tote bags with your company or charity logo — used for donations and kept long after Ramadan.', '100 bags',
     'Natural canvas tote bags printed with a simple crescent and lantern design filled with groceries, on a light background'),
    ('ramadan', 'كروت تهنئة رمضان كريم', 'كروت تهنئة شيك بالاسم أو اللوجو، بظرف — للعملاء والموظفين والقرايب.', '١٠٠ كارت',
     'Ramadan greeting cards', 'Elegant greeting cards with your name or logo, with envelopes — for clients, staff and family.', '100 cards',
     'Elegant Ramadan greeting cards with gold foil crescent and lantern on deep navy card, with matching envelopes'),
    ('ramadan', 'بلانر رمضان (ورد قرآن وعادات)', 'أجندة صغيرة لـ ٣٠ يوم: ورد القرآن والصدقات والعادات اليومية — هدية للبنات والأمهات والمدارس.', '٥٠ بلانر',
     'Ramadan planner', 'A 30-day planner for Quran reading, charity and daily habits — a gift for girls, mothers and schools.', '50 planners',
     'An open Ramadan daily planner with checklist pages and pastel Islamic patterns, prayer beads and a cup of tea beside it'),
    ('ramadan', 'منيو إفطار وسحور للمطاعم', 'منيو عروض الإفطار والسحور بتصميم رمضاني — ورق أو كارت ملمّع للترابيزات.', '١٠٠ منيو',
     'Iftar & suhoor menu', 'Iftar and suhoor offer menus with a Ramadan design — paper or laminated table cards.', '100 menus',
     'A Ramadan restaurant menu card with a lantern and crescent design standing on a restaurant table with dates and soup'),
    ('ramadan', 'أكياس وعلب تيك أواي رمضاني', 'أكياس ورق وعلب أكل بلوجو المطعم وتصميم رمضاني — الأوردر يوصل شكله مناسبة.', '٥٠٠ قطعة',
     'Ramadan takeaway bags & boxes', 'Paper bags and food boxes with your logo and a Ramadan design — every order feels festive.', '500 pieces',
     'Printed paper takeaway bags and food boxes with a Ramadan crescent and lantern pattern on a restaurant counter'),
    ('ramadan', 'بانر ورول أب عروض رمضان', 'رول أب أو بانر لعروض رمضان قدام المحل أو جوه السوبر ماركت.', '١ بانر',
     'Ramadan offers roll-up banner', 'A roll-up or banner for Ramadan offers in front of your shop or inside the supermarket.', '1 banner',
     'A Ramadan roll-up banner stand with a lantern and crescent design at a supermarket entrance'),
    ('ramadan', 'ستيكرات رمضان للعلب والأوردرات', 'ستيكرات «رمضان كريم» بلوجو محلك تتلزق على العلب والأكياس والهدايا.', '٥٠٠ ستيكر',
     'Ramadan stickers for packaging', 'Ramadan stickers with your logo for boxes, bags and gifts.', '500 stickers',
     'Round printed Ramadan stickers with crescent and lantern designs on a sheet and sealing kraft gift boxes'),

    # ───────── 💼 هدايا الشركات للسنة الجديدة ─────────
    ('corporate', 'نتيجة حيطة ٢٠٢٧ بلوجو الشركة', 'نتيجة ١٢ شهر بصور منتجاتك أو شغلك — اسم شركتك قدام العميل السنة كلها.', '١٠٠ نتيجة',
     '2027 wall calendar with your logo', 'A 12-month calendar with your products or work — your company name in front of clients all year.', '100 calendars',
     'A premium printed wall calendar with a large photo and monthly grid hanging on an office wall'),
    ('corporate', 'نتيجة مكتب ٢٠٢٧', 'نتيجة مكتب تقف على الترابيزة بقاعدة مقوّاة — أكتر هدية بتتشاف في المكاتب.', '١٠٠ نتيجة',
     '2027 desk calendar', 'A tent desk calendar on a rigid base — the most seen gift in any office.', '100 calendars',
     'A spiral-bound tent desk calendar with colorful pages on a modern office desk next to a laptop and coffee'),
    ('corporate', 'أجندة ٢٠٢٧ جلد بالاسم محفور', 'أجندة سنوية جلد بالاسم أو اللوجو محفور ليزر أو دهب — لكل موظف أو عميل باسمه.', '٢٥ أجندة',
     '2027 leather planner with engraved name', 'A yearly leather planner with a laser- or gold-engraved name or logo — each one personalized.', '25 planners',
     'A premium leather 2027 planner with a gold debossed name on the cover and an elastic strap on a dark wooden desk'),
    ('corporate', 'بوكس هدية الشركة (أجندة + قلم + مج)', 'بوكس شيك فيه أجندة وقلم معدن ومج بلوجو الشركة — هدية جاهزة للموظفين والعملاء.', '٢٠ بوكس',
     'Corporate gift box (planner + pen + mug)', 'A premium box with a planner, a metal pen and a mug with your logo — ready for staff and clients.', '20 boxes',
     'An open black rigid gift box with foam inserts holding a leather planner, a metal pen and a mug with a company logo'),
    ('corporate', 'بوكس ترحيب بالموظفين الجدد', 'ويلكم كيت: تيشيرت وكارنيه ودفتر وقلم وزجاجة مياه — أول يوم الموظف يحس إنه من الفريق.', '١٠ بوكس',
     'New employee welcome kit', 'A welcome kit: T-shirt, ID badge, notebook, pen and water bottle — day one feels like home.', '10 kits',
     'A neatly arranged employee welcome kit box with a folded T-shirt, ID badge, notebook, pen and water bottle all in matching company colors'),
    ('corporate', 'زجاجة مياه حرارية بالاسم محفور', 'زجاجة ستانلس بتحافظ على السخن والساقع، والاسم أو اللوجو محفور ليزر مايتمسحش.', '١٠ زجاجات',
     'Engraved thermal water bottle', 'A stainless bottle that keeps drinks hot or cold, with a laser-engraved name or logo.', '10 bottles',
     'Matte stainless steel thermal water bottles with a laser-engraved company logo standing on an office desk'),
    ('corporate', 'باور بانك بلوجو الشركة', 'باور بانك عملي بلوجو مطبوع أو محفور — هدية VIP بتتشال في الشنطة كل يوم.', '١٠ قطع',
     'Branded power bank', 'A practical power bank with a printed or engraved logo — a VIP gift carried every day.', '10 pieces',
     'A sleek power bank with an engraved company logo charging a smartphone on a desk'),
    ('corporate', 'فلاشة USB بلوجو الشركة', 'فلاشة بلوجو الشركة محفور، في علبة هدية — للمؤتمرات والعملاء.', '٢٥ فلاشة',
     'Branded USB flash drive', 'A flash drive with an engraved company logo in a gift box — for conferences and clients.', '25 drives',
     'Metal USB flash drives with an engraved company logo in small gift boxes'),
    ('corporate', 'كروت تهنئة بالسنة الجديدة للشركات', 'كروت تهنئة بلوجو الشركة وتوقيع المدير، بظرف مطبوع — للعملاء والشركاء.', '١٠٠ كارت',
     'Corporate new-year greeting cards', 'Greeting cards with your logo and the manager’s signature, in printed envelopes — for clients and partners.', '100 cards',
     'Premium new-year corporate greeting cards with gold foil details and matching envelopes on a white desk'),
    ('corporate', 'تيشيرتات وهوديز لإطلاق السنة', 'تيشيرت أو هودي بلوجو الشركة لبداية السنة والتيم بيلدنج — طباعة أو تطريز.', '٢٠ قطعة',
     'Kick-off T-shirts & hoodies', 'T-shirts or hoodies with your logo for the year kick-off and team building — printed or embroidered.', '20 pieces',
     'Folded navy hoodies and T-shirts with an embroidered company logo on a light background'),
    ('corporate', 'كارنيهات موظفين بحامل وشريط', 'كارنيه PVC بالصورة والاسم، وحامل وشريط رقبة بلوجو الشركة — تجديد السنة الجديدة.', '٥٠ كارنيه',
     'Staff ID cards with holder & lanyard', 'PVC photo ID cards with holders and lanyards printed with your logo — the new-year refresh.', '50 cards',
     'Plastic employee ID badges with lanyards printed with a company logo laid out on a desk'),
    ('corporate', 'باكدج الهوية للشركة (فولدر + ورق + أظرف)', 'فولدر وورق رسمي وأظرف وكروت شخصية بنفس الهوية — الشركة تبدأ السنة بشكل محترف.', '١ باكدج',
     'Corporate stationery package', 'Folder, letterhead, envelopes and business cards in one identity — start the year looking professional.', '1 package',
     'A coordinated corporate stationery set with presentation folder, letterhead, envelopes and business cards in matching colors'),
    ('corporate', 'مج سيراميك بلوجو الشركة في بوكس', 'مج بلوجو الشركة في علبة مطبوعة — هدية بسيطة لكل الموظفين بسعر معقول.', '٢٤ مج',
     'Logo mug in a printed box', 'A mug with your logo in a printed box — a simple, affordable gift for every employee.', '24 mugs',
     'A white ceramic mug with a company logo next to its printed gift box on an office desk'),
    ('corporate', 'قلم معدن في علبة هدية بالاسم', 'قلم تقيل بالاسم محفور ليزر في علبة قطيفة — هدية الإدارة والعملاء الكبار.', '٢٥ قلم',
     'Engraved metal pen in a gift case', 'A heavy pen with a laser-engraved name in a velvet case — for management and key clients.', '25 pens',
     'An elegant metal pen with an engraved name resting in an open velvet-lined gift case'),
]

# متجرين للموسم: كل واحد فيه منتجات قسمه كلها + كام منتج قديم مناسب
# (الاسم، slug، الشعار، الأقسام، أيقونة، لون، المدينة، الاسم EN، الشعار EN، المدينة EN)
PRINT_STORES_SEASONAL = [
    ('فوانيس للمطبوعات الرمضانية', 'fawanees-ramadan-prints', 'كل مطبوعات رمضان لبيتك ومحلك وجمعيتك', ['ramadan', 'packaging', 'events'],
     'fa-moon', '#7C2D12', 'القاهرة', 'Fawanees Ramadan Prints', 'All your Ramadan prints for home, shop and charity', 'Cairo'),
    ('هدايا البزنس', 'business-gifts-2027', 'هدايا شركات تخلّي اسمك مع العميل السنة كلها', ['corporate', 'notebooks', 'pens', 'mugs'],
     'fa-briefcase', '#0F172A', 'التجمع الخامس', 'Business Gifts', 'Corporate gifts that keep your name with clients all year', 'New Cairo'),
]

# أقسام المتاجر القديمة اللي بتاخد منتجات من الموسم
SEASONAL_FOR = {
    'ramadan': {'packaging', 'events', 'cafe', 'gifts', 'stickers', 'banners'},
    'corporate': {'notebooks', 'office', 'gifts', 'pens', 'mugs', 'apparel'},
}


def seasonal_plan(stores):
    """[(slug، قايمة المنتجات)] للمتاجر القديمة — ثابت (random لوحده) ومن غير قاعدة بيانات.

    stores: [(slug، الأقسام)]. كل متجر قسمه يناسب الموسم بياخد من ٣ لـ ٥ منتجات من كل موسم يناسبه.
    """
    rng = random.Random(2028)
    out = []
    for slug, cats in stores:
        picked = []
        for season, fits in SEASONAL_FOR.items():
            if not fits & set(cats):
                continue
            pool = [it for it in PRINT_ITEMS_SEASONAL if it[0] == season]
            rng.shuffle(pool)
            picked += pool[:rng.randint(3, 5)]
        if picked:
            out.append((slug, picked))
    return out


assert all(len(it) == 8 for it in PRINT_ITEMS_SEASONAL)
assert all(len(s) == 10 for s in PRINT_STORES_SEASONAL)
assert {it[0] for it in PRINT_ITEMS_SEASONAL} == set(PRINT_CATEGORIES_SEASONAL)
