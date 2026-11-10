"""🌱 محتوى كتالوج السوق المبدئي (بالعامية المصرية) — متاجر بتنفّذها Mouss Tec.

بيتنادى من المهاجرة 0114 ومن أمر `seed_marketplace_catalog` — idempotent (بالـ slug + اسم المنتج).
الأسعار مش موجودة عمداً: العميل بيطلب سعر وصاحب المنصة بيرد.
"""
import random

# ─────────────────────────────── 🖨️ المطابع والتصميم ───────────────────────────────
# القسم ⇒ (اسم القسم للعرض، الأيقونة)
PRINT_CATEGORIES = {
    'cards': ('كروت شخصية', 'fa-id-card'),
    'flyers': ('فلايرات وبروشورات ومنيو', 'fa-file-image'),
    'notebooks': ('دفاتر وأجندات ونتائج', 'fa-book'),
    'mugs': ('كوبايات ومجات', 'fa-mug-hot'),
    'pens': ('أقلام', 'fa-pen'),
    'gifts': ('هدايا ودعاية', 'fa-gift'),
    'apparel': ('تيشيرتات ويونيفورم', 'fa-shirt'),
    'banners': ('بنرات ويفط', 'fa-flag'),
    'stickers': ('ستيكرات ولابلز', 'fa-note-sticky'),
    'packaging': ('علب وتغليف وشنط', 'fa-box-open'),
    'design': ('تصميم لوجو وهوية', 'fa-pen-nib'),
    'social': ('تصميمات سوشيال ميديا', 'fa-share-nodes'),
    'events': ('دعوات ومناسبات', 'fa-envelope-open-text'),
    'office': ('مطبوعات الشركات والعيادات', 'fa-file-invoice'),
}

# (القسم، الاسم، الوصف، أقل كمية)
PRINT_ITEMS = [
    ('cards', 'كروت شخصية كوشيه ٣٥٠ جرام', 'كارت تقيل وشيك، وش وضهر، سلوفان مط أو لامع زي ما تحب. بنظبطلك التصميم ببلاش لو معاك اللوجو.', '١٠٠ كارت'),
    ('cards', 'كروت بيزنس بطبعة سبوت يوفي', 'اللوجو بيلمع لوحده فوق الكارت المط — شكل فخم يفرق مع أي عميل.', '٢٥٠ كارت'),
    ('cards', 'كروت بحروف بارزة (إمبوس)', 'اسمك أو اللوجو بارز تحس بيه بإيدك. مناسبة للمحامين والدكاترة وأصحاب الشركات.', '٢٥٠ كارت'),
    ('cards', 'كروت بتذهيب (فويل دهبي/فضي)', 'تذهيب دهبي أو فضي على اللوجو أو الاسم — الكارت يبان غالي من أول نظرة.', '٢٥٠ كارت'),
    ('cards', 'كروت شخصية بلاستيك شفاف', 'كارت PVC شفاف مش بيتقطع ولا بيتبل — مختلف ومحدش هيرميه.', '١٠٠ كارت'),
    ('cards', 'كروت ولاء وخصم للمحلات', 'كارت بأختام أو نقط عشان الزبون يرجعلك تاني. ينفع بترقيم.', '٥٠٠ كارت'),
    ('flyers', 'فلاير A5 وشين', 'فلاير للدعاية والعروض، ورق كوشيه ١٥٠ جرام، ألوان صريحة.', '١٠٠٠ فلاير'),
    ('flyers', 'بروشور تري فولد (٣ طيات)', 'بروشور بيشرح خدماتك كلها في ورقة واحدة متطبقة شيك.', '٥٠٠ بروشور'),
    ('flyers', 'منيو مطعم مقوى ومتغلف', 'منيو مش بيتبهدل من الأكل والمية، سلوفان من الناحيتين.', '٢٠ منيو'),
    ('flyers', 'منيو دليفري ورق خفيف', 'منيو بتتوزع مع الأوردرات وعلى البيوت — رخيصة وبتجيب طلبات.', '٢٠٠٠ ورقة'),
    ('flyers', 'كتالوج منتجات مدبّس', 'كتالوج ١٢ لـ ٤٨ صفحة لمنتجاتك، غلاف تقيل وجوه كوشيه.', '١٠٠ نسخة'),
    ('flyers', 'بوستر A3 و A2', 'بوسترات للمحلات والعيادات والمدارس، طباعة ديجيتال بجودة عالية.', '١٠ بوستر'),
    ('notebooks', 'كشكول سلك بغلاف مطبوع باللوجو', 'كشكول ١٠٠ ورقة سلك، الغلاف تصميمك — هدية للموظفين والعملاء.', '٥٠ كشكول'),
    ('notebooks', 'أجندة جلد باسم الشركة', 'أجندة سنوية جلد طبيعي أو صناعي، الاسم محفور أو مطبوع على الغلاف.', '٢٥ أجندة'),
    ('notebooks', 'نوت بوك صغيرة A6 هدايا', 'نوت بوك صغيرة تتحط في الشنطة، مثالية للمعارض والإيفنتات.', '١٠٠ نوت'),
    ('notebooks', 'نتيجة حيطة ومكتب بالتصميم بتاعك', 'نتيجة السنة الجديدة بصور منتجاتك أو شغلك — بتفضل قدام العميل ١٢ شهر.', '١٠٠ نتيجة'),
    ('notebooks', 'دفاتر فواتير وإيصالات بالكربون', 'دفاتر نسختين أو ٣ نسخ، مترقمة، باسم المحل والرقم الضريبي.', '١٠ دفاتر'),
    ('notebooks', 'كراسات تلوين وأنشطة للأطفال', 'كراسات تعليمية بتصميمك للحضانات والمدارس ومراكز الدروس.', '١٠٠ كراسة'),
    ('mugs', 'كوباية سيراميك بيضا مطبوعة', 'كوباية ١١ أونص، الطباعة مش بتروح مع الغسيل. صورة أو لوجو أو كلام.', '١ كوباية'),
    ('mugs', 'مج سحري بيغير لونه مع السخن', 'المج أسود ولما تحط فيه حاجة سخنة الصورة تظهر — هدية بتعمل مفاجأة.', '١ مج'),
    ('mugs', 'كوباية بيد ملونة وجوه ملون', 'ألوان كتير من جوه وفي الإيد، تمشي مع ألوان البراند بتاعك.', '١٢ كوباية'),
    ('mugs', 'ترمس استانلس بالليزر', 'ترمس بيحافظ على السخن والساقع، الاسم أو اللوجو محفور ليزر مش بيتمسح.', '١٠ ترمس'),
    ('mugs', 'مج قزاز فروستد', 'مج قزاز شيك بطبعة ناعمة — حلو للكافيهات والهدايا.', '١٢ مج'),
    ('mugs', 'طقم كوبايات هدايا في علبة', 'كوبايتين أو ٤ في علبة مطبوعة — جاهزة للإهداء على طول.', '١٠ أطقم'),
    ('pens', 'أقلام بلاستيك مطبوعة باللوجو', 'قلم جاف دعاية، ألوان كتير، الطباعة على الجسم أو الكليب.', '١٠٠ قلم'),
    ('pens', 'أقلام معدن محفورة ليزر', 'قلم تقيل شيك، الاسم محفور ليزر — هدية للعملاء الكبار والمؤتمرات.', '٢٥ قلم'),
    ('pens', 'طقم قلم وميدالية في علبة', 'طقم هدية متغلف، ينفع بالاسم أو اللوجو.', '٢٥ طقم'),
    ('pens', 'أقلام سبورة وهايلايتر بالاسم', 'للمدارس ومراكز الدروس والشركات — اسمك على كل قلم.', '١٠٠ قلم'),
    ('pens', 'قلم تاتش بروجكتور', 'قلم بيكتب وبيشتغل على شاشات الموبايل، اللوجو مطبوع.', '٥٠ قلم'),
    ('gifts', 'ميداليات مفاتيح جلد وأكريليك', 'ميدالية باللوجو أو الاسم، أشكال كتير حسب طلبك.', '٥٠ ميدالية'),
    ('gifts', 'باور بانك مطبوع باللوجو', 'هدية بيستعملها العميل كل يوم واسمك قدامه.', '٢٠ قطعة'),
    ('gifts', 'فلاشات USB بالاسم', 'فلاشة ١٦ أو ٣٢ جيجا بشكل الكارت أو الخشب بالحفر.', '٢٥ فلاشة'),
    ('gifts', 'دروع وشهادات تقدير', 'دروع أكريليك أو خشب بالحفر للتكريمات والمناسبات.', '١ درع'),
    ('gifts', 'بوكس هدايا للشركات', 'بوكس فيه قلم وأجندة ومج وميدالية — كله بالبراند بتاعك.', '١٠ بوكس'),
    ('gifts', 'ماوس باد مطبوع', 'ماوس باد بصورتك أو تصميمك، تحت مطاط مش بيزحلق.', '٢٥ قطعة'),
    ('gifts', 'كاب وطاقية بالتطريز', 'كاب قطن والتطريز باللوجو — للفرق والشركات والرحلات.', '٢٥ كاب'),
    ('gifts', 'شنط قماش (توت باج) مطبوعة', 'شنطة قماش قطن تشيل كتير، الطباعة سيلك أو DTF.', '٥٠ شنطة'),
    ('gifts', 'حفر ليزر على خشب وجلد', 'أي تصميم بيتحفر على خشب أو جلد أو أكريليك — قطعة واحدة أو كمية.', '١ قطعة'),
    ('apparel', 'تيشيرتات قطن مطبوعة DTF', 'تيشيرت قطن ١٠٠٪، الطباعة DTF ألوان كاملة ومش بتتشقق.', '١٠ تيشيرت'),
    ('apparel', 'يونيفورم مطاعم وكافيهات', 'تيشيرت أو بولو وكاب ومريلة بلوجو المكان — شكل موحد ومحترم.', '١٠ أطقم'),
    ('apparel', 'يونيفورم شركات (قمصان بولو)', 'بولو بيكيه بالتطريز على الصدر، ألوان الشركة.', '٢٠ قطعة'),
    ('apparel', 'سكراب ويونيفورم طبي', 'سكراب للدكاترة والتمريض بالاسم والتخصص مطرزين.', '٥ أطقم'),
    ('apparel', 'سويت شيرت وهودي بالتصميم', 'هودي قطن تقيل للشتا، طباعة أو تطريز.', '١٠ قطع'),
    ('apparel', 'يونيفورم مدارس وحضانات', 'زي مدرسي كامل بالشعار مطرز — مقاسات من الحضانة للثانوي.', '٢٠ طقم'),
    ('apparel', 'تيشيرتات إيفنتات وماراثون', 'تيشيرت دراي فيت خفيف بأسامي الرعاة، للماراثونات والمؤتمرات.', '٥٠ تيشيرت'),
    ('apparel', 'سيفتي فيست عاكس مطبوع', 'فيست عاكس للمواقع والمصانع والجراجات، الاسم مطبوع على الضهر.', '٢٠ فيست'),
    ('banners', 'رول أب استاند', 'رول أب ٨٥×٢٠٠ مع الشنطة، بتفرده في ثانية في أي معرض أو فرع.', '١ رول أب'),
    ('banners', 'بنر فليكس للمحلات', 'بنر فليكس بالمقاس اللي تحتاجه، بحلقات معدن، يستحمل الشمس.', '١ بنر'),
    ('banners', 'يافطة محل مضيئة', 'يافطة لايت بوكس أو حروف بارزة بإضاءة LED — المحل يبان بالليل.', '١ يافطة'),
    ('banners', 'استيكر فينيل للعربيات والواجهات', 'تغليف عربية أو واجهة زجاج بالاستيكر المطبوع.', '١ متر'),
    ('banners', 'لافتات إرشادية للشركات والعيادات', 'لافتات أبواب ومكاتب وأسهم إرشادية أكريليك أو فوم.', '٥ لافتات'),
    ('banners', 'باك دروب للمؤتمرات والتصوير', 'خلفية كبيرة بالشعارات للتصوير والإيفنتات.', '١ قطعة'),
    ('stickers', 'ستيكرات لوجو دايكت (مقصوصة على الشكل)', 'ستيكر فينيل مقصوص على شكل اللوجو، مقاوم للمية.', '١٠٠ ستيكر'),
    ('stickers', 'لابلز منتجات (رول)', 'ليبل على رول للعلب والبرطمانات والمنتجات اليدوية.', '٥٠٠ ليبل'),
    ('stickers', 'ستيكرات أطفال وتعليمية', 'شيتات ستيكرات بتصميمك للحضانات والمكافآت.', '٥٠ شيت'),
    ('stickers', 'ستيكر ختم الأمان (هولوجرام)', 'ستيكر بيتقطع لو اتفتح — لحماية الكراتين والضمان.', '٥٠٠ ستيكر'),
    ('packaging', 'علب كرتون مطبوعة للمنتجات', 'علبة بمقاس منتجك وتصميمك — الشكل بيبيع قبل المنتج.', '١٠٠ علبة'),
    ('packaging', 'شنط ورق كرافت باللوجو', 'شنط ورق للمحلات والصيدليات والهدوم، بيد مبرومة أو مقصوصة.', '١٠٠ شنطة'),
    ('packaging', 'علب بيتزا وأكل مطبوعة', 'علب بيتزا وبرجر وساندوتشات باسم المطعم.', '٥٠٠ علبة'),
    ('packaging', 'علب هدايا مقوى بمغناطيس', 'علبة فخمة بقفلة مغناطيس للعطور والإكسسوارات والهدايا.', '٥٠ علبة'),
    ('packaging', 'أكياس بلاستيك مطبوعة', 'أكياس بالشعار للمحلات والسوبر ماركت.', '١٠٠٠ كيس'),
    ('design', 'تصميم لوجو احترافي', '٣ أفكار مختلفة وتعديلات لحد ما ترضى، والملفات كلها مفتوحة.', 'تصميم واحد'),
    ('design', 'هوية بصرية كاملة', 'لوجو وألوان وخطوط وكارت وورق رسمي وظرف — كل حاجة بنفس الروح.', 'باكدج'),
    ('design', 'تصميم باكدج وعلب منتجات', 'تصميم العلبة والليبل جاهز للطباعة بالمقاسات.', 'تصميم واحد'),
    ('design', 'تصميم منيو مطعم أو كافيه', 'منيو شكلها حلو وسهلة القراية، عربي وإنجليزي.', 'تصميم واحد'),
    ('design', 'تعديل وتحويل لوجو قديم لفيكتور', 'لوجو قديم مش واضح؟ بنرسمه من جديد بجودة عالية لأي طباعة.', 'لوجو واحد'),
    ('social', 'باقة تصميمات سوشيال شهرية', '١٢ أو ٢٠ بوست في الشهر بألوان البراند، مع الكابشن لو حابب.', 'باقة شهرية'),
    ('social', 'تصميم غلاف فيسبوك وصورة بروفايل', 'كفر وبروفايل متظبطين على مقاسات كل منصة.', 'تصميم واحد'),
    ('social', 'تصميم ستوري وريلز', 'قوالب ستوري وغلاف ريلز ثابتة تستعملها كل مرة.', '١٠ تصميمات'),
    ('social', 'تصميم إعلان ممول', 'تصميم إعلان بيلفت النظر ومظبوط على قواعد فيسبوك وانستجرام.', 'تصميم واحد'),
    ('events', 'كروت دعوة فرح', 'دعوات فرح بتصميمات كتير، ورق فاخر وظرف، وتذهيب لو حابب.', '١٠٠ دعوة'),
    ('events', 'دعوات خطوبة وكتب كتاب وعقيقة', 'كروت مناسبات بأشكال مختلفة، ينفع معاها توزيعات.', '٥٠ دعوة'),
    ('events', 'توزيعات وتاجات مناسبات', 'تاجات وستيكرات للشوكولاتة والتوزيعات بالأسامي والتاريخ.', '١٠٠ قطعة'),
    ('events', 'بادجات مؤتمرات وكروت تعليق', 'بادج بالاسم والصورة مع الشريط — للمؤتمرات والمعارض.', '٥٠ بادج'),
    ('office', 'ورق رسمي (ليترهيد) وظروف', 'ورق مراسلات وظروف باسم الشركة والعنوان.', '٥٠٠ ورقة'),
    ('office', 'روشتات وكروت مواعيد للعيادات', 'روشتة باسم الدكتور والتخصص ودفاتر مواعيد.', '١٠ دفاتر'),
    ('office', 'ملفات وفولدرات بجيب', 'فولدر مقوى بجيب للعروض والعقود، باللوجو.', '١٠٠ فولدر'),
    ('office', 'كروت ضمان وشهادات للمنتجات', 'كارت ضمان مترقم أو شهادة جودة تتحط مع المنتج.', '٥٠٠ كارت'),
    ('office', 'أختام وأوتوماتيك باسم الشركة', 'ختم أوتوماتيك بالبيانات والرقم الضريبي.', '١ ختم'),
]

# (الاسم، الـ slug، الجملة التعريفية، الأقسام، الأيقونة، اللون، المدينة)
PRINT_STORES = [
    ('مطبعة النيل للكروت', 'nile-cards', 'كروتك بتقول عليك قبل ما تتكلم', ['cards', 'office', 'stickers'], 'fa-id-card', '#1E2A57', 'القاهرة'),
    ('كوباية وحكاية', 'koubaya-w-hekaya', 'كل كوباية وراها حكاية — قولّنا حكايتك', ['mugs', 'gifts'], 'fa-mug-hot', '#B45309', 'الجيزة'),
    ('قلمك معاك', 'qalamak-maak', 'أقلام دعاية تفضل في إيد عميلك', ['pens', 'gifts'], 'fa-pen', '#0F766E', 'القاهرة'),
    ('دفترجي', 'daftargy', 'دفاتر وأجندات ونتائج بالبراند بتاعك', ['notebooks', 'office', 'pens'], 'fa-book', '#7C3AED', 'الإسكندرية'),
    ('يونيفورم مصر', 'uniform-masr', 'يونيفورم يخلّي فريقك يبان فريق', ['apparel', 'gifts'], 'fa-user-tie', '#1D4ED8', 'القاهرة'),
    ('تيشيرت ستوديو', 'tshirt-studio', 'اطبع اللي في دماغك على تيشيرت', ['apparel', 'gifts'], 'fa-shirt', '#DB2777', 'المعادي'),
    ('بنرات الجيزة', 'banners-giza', 'بنرات ورول أب ويفط في نفس اليوم', ['banners', 'stickers', 'flyers'], 'fa-flag', '#DC2626', 'الجيزة'),
    ('ستيكر تاون', 'sticker-town', 'ستيكرات ولابلز بأي شكل وأي مقاس', ['stickers', 'packaging', 'cards'], 'fa-note-sticky', '#CA8A04', 'مدينة نصر'),
    ('علبة وتغليف', 'elba-w-taghleef', 'التغليف الحلو بيبيع لوحده', ['packaging', 'stickers', 'design'], 'fa-box-open', '#92400E', 'العاشر من رمضان'),
    ('هدايا الشركات', 'corporate-gifts', 'هدايا بالبراند لعملائك وموظفينك', ['gifts', 'pens', 'mugs'], 'fa-gift', '#BE123C', 'التجمع الخامس'),
    ('ستوديو لوجو', 'studio-logo', 'لوجو يعيش معاك سنين', ['design', 'social', 'cards'], 'fa-pen-nib', '#4F46E5', 'أونلاين'),
    ('سوشيال برو', 'social-pro', 'بوستات بتجيب تفاعل وبيع', ['social', 'design', 'flyers'], 'fa-share-nodes', '#0284C7', 'أونلاين'),
    ('فرحة للدعوات', 'farha-invitations', 'دعوات وتوزيعات لأحلى مناسباتك', ['events', 'gifts'], 'fa-envelope-open-text', '#C026D3', 'المنصورة'),
    ('أوفست الإسكندرية', 'offset-alex', 'كميات كبيرة بسعر الجملة', ['flyers', 'office', 'notebooks'], 'fa-print', '#334155', 'الإسكندرية'),
    ('ديجيتال برنت الدقي', 'digital-print-dokki', 'طباعة سريعة على قد طلبك', ['flyers', 'cards', 'stickers'], 'fa-bolt', '#0EA5E9', 'الدقي'),
    ('يافطة وأكتر', 'yafta-w-aktar', 'خلّي محلك يبان من أول الشارع', ['banners', 'office', 'stickers'], 'fa-sign-hanging', '#EA580C', 'شبرا'),
    ('تطريز الوادي', 'tatreez-elwadi', 'تطريز بالكمبيوتر بأدق التفاصيل', ['apparel', 'gifts'], 'fa-scissors', '#15803D', 'أسيوط'),
    ('مدرستي للزي المدرسي', 'madrasty-uniform', 'زي مدرسي وكراسات بشعار المدرسة', ['apparel', 'notebooks'], 'fa-graduation-cap', '#2563EB', 'طنطا'),
    ('مطبعة المنصورة الحديثة', 'mansoura-modern-press', 'كل مطبوعاتك تحت سقف واحد', ['flyers', 'notebooks', 'office', 'cards'], 'fa-industry', '#475569', 'المنصورة'),
    ('جرافيك هاوس', 'graphic-house', 'تصميم وتغليف يفرّقك عن المنافسين', ['design', 'packaging', 'social'], 'fa-palette', '#9333EA', 'الشيخ زايد'),
    ('كاب وشنطة', 'cap-w-shanta', 'كابات وشنط وتيشيرتات للفرق والرحلات', ['gifts', 'apparel'], 'fa-bag-shopping', '#0D9488', 'العبور'),
    ('ليزر كرافت', 'laser-craft', 'حفر ليزر على أي خامة', ['gifts', 'banners'], 'fa-wand-magic-sparkles', '#B91C1C', 'حلوان'),
    ('منيو ومطاعم', 'menu-w-mataem', 'كل مطبوعات المطعم من المنيو للعلبة', ['flyers', 'packaging', 'stickers'], 'fa-utensils', '#D97706', 'مصر الجديدة'),
    ('كاليندر برنت', 'calendar-print', 'نتايج وأجندات تفضل قدام عميلك طول السنة', ['notebooks', 'gifts'], 'fa-calendar-days', '#0369A1', 'الزقازيق'),
    ('ميديكال برنت', 'medical-print', 'روشتات وملفات وسكرابات للعيادات والمستشفيات', ['office', 'apparel'], 'fa-stethoscope', '#059669', 'المهندسين'),
    ('برنت إكسبريس الهرم', 'print-express-haram', 'اطلب الصبح واستلم بالليل', ['cards', 'flyers', 'banners', 'stickers'], 'fa-truck-fast', '#E11D48', 'الهرم'),
]

# ─────────────────────────────── 🚗 قطع الغيار ───────────────────────────────
PARTS_CATEGORIES = {
    'ignition': ('بوجيهات وكهربا', 'fa-bolt'),
    'brakes': ('فرامل', 'fa-circle-stop'),
    'suspension': ('عفشة ومساعدين', 'fa-car-burst'),
    'filters': ('زيوت وفلاتر', 'fa-oil-can'),
    'tires': ('كاوتش وجنوط', 'fa-circle-dot'),
    'battery': ('بطاريات', 'fa-car-battery'),
    'cooling': ('تبريد وتكييف', 'fa-snowflake'),
    'engine': ('موتور وسيور', 'fa-gears'),
    'lights': ('فوانيس وإضاءة', 'fa-lightbulb'),
    'body': ('بودي وإكسسوارات', 'fa-car-side'),
}

# (القسم، الاسم {make}/{models}، الوصف، النوع)
PARTS_ITEMS = [
    ('ignition', 'بوجيهات إيريديوم - {make}', 'بوجيهات إيريديوم بتعيش ٦٠ ألف كيلو، بتحسن الفتيلة وبتوفر بنزين. مناسبة لـ{models}.', 'جديد أصلي'),
    ('ignition', 'بوجيهات عادي (نيكل) - {make}', 'طقم بوجيهات اقتصادي للصيانة الدورية — {models}.', 'جديد'),
    ('ignition', 'موبينة (كويل) - {make}', 'موبينة جديدة لو العربية بتنتّع أو لمبة الموتور منورة — {models}.', 'جديد أصلي'),
    ('ignition', 'دينامو - {make}', 'دينامو بيشحن مظبوط، مضمون — {models}.', 'جديد أو استيراد'),
    ('ignition', 'مارش - {make}', 'مارش قوي للعربية اللي بتتقل في الدوران — {models}.', 'جديد أو استيراد'),
    ('ignition', 'حساسات (أكسجين / كرنك / كام) - {make}', 'كل أنواع الحساسات، قول لنا رقم القطعة أو كود العطل وإحنا نحدد — {models}.', 'أصلي'),
    ('brakes', 'تيل فرامل أمامي - {make}', 'تيل سيراميك بيوقف كويس ومش بيعمل صوت ولا تراب كتير — {models}.', 'جديد'),
    ('brakes', 'تيل فرامل خلفي - {make}', 'تيل خلفي أو فحمات طنبورة حسب الموديل — {models}.', 'جديد'),
    ('brakes', 'طنابير (ديسكات) فرامل - {make}', 'طنابير جديدة بدل اللي اتاكلت أو بتخبط في الدواسة — {models}.', 'جديد'),
    ('brakes', 'زيت فرامل DOT4', 'زيت فرامل أصلي، يتغير كل سنتين عشان الفرامل تفضل حادة.', 'جديد'),
    ('suspension', 'مساعدين أمامي - {make}', 'مساعدين زيت أو غاز، العربية ترجع ثابتة على المطبات — {models}.', 'جديد'),
    ('suspension', 'مساعدين خلفي - {make}', 'طقم مساعدين خلفي مع الكاوتش والكراسي — {models}.', 'جديد'),
    ('suspension', 'مقصات وجلب - {make}', 'مقصات كاملة أو جلب بس، لو سامع تكتكة في الدركسيون — {models}.', 'جديد'),
    ('suspension', 'بلي عجل (رولمان بلي) - {make}', 'لو فيه زنة بتزيد مع السرعة يبقى البلي — {models}.', 'جديد'),
    ('suspension', 'بارات وعمود توازن - {make}', 'بارات أمامي وخلفي وجلب عمود التوازن — {models}.', 'جديد'),
    ('filters', 'فلتر زيت - {make}', 'فلتر زيت أصلي على مقاس موتورك — {models}.', 'أصلي'),
    ('filters', 'فلتر هوا - {make}', 'فلتر هوا نضيف = عزم أحسن وبنزين أقل — {models}.', 'أصلي'),
    ('filters', 'فلتر تكييف (كابينة) - {make}', 'فلتر تكييف كربون بيقفل على التراب والريحة — {models}.', 'جديد'),
    ('filters', 'فلتر بنزين - {make}', 'فلتر بنزين يحمي الطرمبة والبخاخات — {models}.', 'أصلي'),
    ('filters', 'زيت موتور فول سينثتك', 'زيت ٥W-30 أو ٥W-40 من ماركات مضمونة، جالون ٤ أو ٥ لتر.', 'جديد'),
    ('filters', 'زيت فتيس أوتوماتيك', 'زيت فتيس ATF على مواصفات المصنع، بنغيره بالماكينة لو حابب.', 'جديد'),
    ('tires', 'كاوتش {make} بالمقاسات الأصلية', 'كاوتش ميشلان وبريدجستون وهانكوك وماركات اقتصادية، بالمقاس الأصلي لـ{models}.', 'جديد'),
    ('tires', 'جنوط سبور - {make}', 'جنوط ألومنيوم بمقاسات ١٥ لـ ١٩ بوصة — {models}.', 'جديد'),
    ('tires', 'استبن وعدة', 'استبن بمقاس العربية مع الكريك والمفتاح.', 'جديد'),
    ('battery', 'بطارية - {make}', 'بطارية بأمبير مظبوط على العربية، بضمان — {models}.', 'جديد'),
    ('battery', 'بطارية AGM للعربيات بتاعة ستارت-ستوب', 'للعربيات اللي بتطفي لوحدها في الإشارة — لازم AGM.', 'جديد'),
    ('cooling', 'رادياتير - {make}', 'رادياتير ألومنيوم جديد لو العربية بتسخن — {models}.', 'جديد'),
    ('cooling', 'طرمبة مية - {make}', 'طرمبة مية مع الجوان — {models}.', 'جديد أصلي'),
    ('cooling', 'كومبروسر تكييف - {make}', 'كومبروسر جديد أو استيراد بحالة ممتازة — {models}.', 'جديد أو استيراد'),
    ('cooling', 'مروحة رادياتير وثرموستات - {make}', 'مروحة كهربا وثرموستات وحساس حرارة — {models}.', 'جديد'),
    ('cooling', 'مية رادياتير (كولانت)', 'كولانت جاهز للاستعمال، بيمنع الصدا والتسخين.', 'جديد'),
    ('engine', 'طقم سير كاتينة - {make}', 'سير الكاتينة مع البلي والشداد — يتغير في معاده عشان الموتور ميتخبطش — {models}.', 'أصلي'),
    ('engine', 'سير مجموعة (دينامو/تكييف) - {make}', 'سير مجموعة مع الشداد — {models}.', 'جديد'),
    ('engine', 'كاتينة وطقم توقيت - {make}', 'كاتينة وجزر وشداد للموتورات اللي بكاتينة — {models}.', 'أصلي'),
    ('engine', 'جوانات وطقم وش سلندر - {make}', 'جوان وش سلندر وغطا البلوف وكل الجوانات — {models}.', 'أصلي'),
    ('engine', 'كراسي موتور وفتيس - {make}', 'لو فيه رعشة في العربية وهي واقفة يبقى الكراسي — {models}.', 'جديد'),
    ('engine', 'طرمبة بنزين - {make}', 'طرمبة بنزين كاملة بالعوامة — {models}.', 'جديد'),
    ('engine', 'بخاخات (رشاشات) - {make}', 'بخاخات جديدة أو بنضّف القديمة بالموجات — {models}.', 'أصلي'),
    ('lights', 'فانوس أمامي - {make}', 'فانوس كامل يمين أو شمال، عادي أو LED — {models}.', 'جديد أو استيراد'),
    ('lights', 'فانوس خلفي (اسطوب) - {make}', 'اسطوب يمين وشمال — {models}.', 'جديد'),
    ('lights', 'لمبات LED وزينون', 'لمبات LED بيضا قوية لكل المقاسات (H1, H4, H7, H11…).', 'جديد'),
    ('lights', 'كشافات ضباب - {make}', 'كشافات ضباب مع الإطار والسويتش — {models}.', 'جديد'),
    ('body', 'إكصدام أمامي وخلفي - {make}', 'إكصدام جديد جاهز للدهان — {models}.', 'جديد أو استيراد'),
    ('body', 'مرايات جانبية - {make}', 'مراية كاملة كهربا أو عادي، بالإشارة لو موجودة — {models}.', 'جديد'),
    ('body', 'مساحات زجاج - {make}', 'طقم مساحات بمقاسات العربية، بتمسح من غير صوت — {models}.', 'جديد'),
    ('body', 'دواسات أرضية وتلبيسة كراسي - {make}', 'دواسات ٥D على مقاس العربية بالظبط وتلبيسة جلد — {models}.', 'جديد'),
    ('body', 'شاشة أندرويد وكاميرا خلفي - {make}', 'شاشة بإطار على مقاس التابلوه مع كاميرا ريفيرس — {models}.', 'جديد'),
]

# (الاسم، الـ slug، الجملة التعريفية، الماركة، الموديلات، الأقسام أو None للكل، الأيقونة، اللون، المدينة)
PARTS_STORES = [
    ('الصيني الأصلي', 'el-seeny-el-asly', 'قطع شيري وجيلي الأصلية من غير لف', 'شيري', 'تيجو ٣، تيجو ٧، أريزو ٥، أريزو ٦', None, 'fa-car', '#DC2626', 'الحرفيين'),
    ('بي واي دي سنتر', 'byd-center', 'كل اللي تحتاجه لعربيتك البي واي دي', 'BYD', 'F3، L3، S5، سونج', None, 'fa-car', '#0EA5E9', 'مدينة نصر'),
    ('إم جي بارتس', 'mg-parts', 'قطع غيار MG أصلية وبديل مضمون', 'MG', 'MG5، MG6، ZS، RX5', None, 'fa-car', '#B91C1C', 'التجمع'),
    ('جيلي وبروتون', 'geely-proton', 'متخصصين في جيلي وبروتون من زمان', 'جيلي', 'إمجراند، كولراي، بروتون ساجا، جين ٢', None, 'fa-car', '#1D4ED8', 'شبرا'),
    ('شانجان وهافال', 'changan-haval', 'قطع الجيل الجديد من العربيات الصيني', 'شانجان', 'CS35، CS55، ألسفين، هافال H6، جولي', None, 'fa-car', '#7C3AED', 'العبور'),
    ('بافاريا بارتس', 'bavaria-parts', 'قطع BMW أصلي وOEM بأسعار معقولة', 'BMW', 'E90، F30، G20، X1، X3، X5', None, 'fa-car-side', '#1E3A8A', 'المهندسين'),
    ('مرسيدس هاوس', 'mercedes-house', 'كل قطع المرسيدس من C لحد S', 'مرسيدس', 'C180، C200، E200، E250، GLC', None, 'fa-car-side', '#334155', 'مصر الجديدة'),
    ('فولكس وسكودا', 'vw-skoda', 'جولف وأوكتافيا وباسات — قطع أصلي', 'فولكس فاجن / سكودا', 'جولف ٦ و٧، باسات، أوكتافيا A5 وA7، سوبرب', None, 'fa-car-side', '#0369A1', 'الدقي'),
    ('أوبل جيرماني', 'opel-germany', 'قطع أوبل أسترا وكورسا وفيكترا', 'أوبل', 'أسترا H وJ، كورسا، فيكترا، إنسيجنيا', None, 'fa-car-side', '#CA8A04', 'الجيزة'),
    ('تويوتا الأصلي', 'toyota-original', 'قطع تويوتا اللي مبتخذلكش', 'تويوتا', 'كورولا، ياريس، كامري، راف فور، فورتشنر', None, 'fa-car', '#DC2626', 'الحرفيين'),
    ('نيسان والصني', 'nissan-sunny', 'صني وسنترا وقشقاي — كله موجود', 'نيسان', 'صني، سنترا، قشقاي، جوك، إكس تريل', None, 'fa-car', '#475569', 'شبرا الخيمة'),
    ('ميتسوبيشي لانسر سنتر', 'mitsubishi-lancer', 'لانسر وباجيرو وأتراج', 'ميتسوبيشي', 'لانسر، أتراج، باجيرو، إكليبس كروس', None, 'fa-car', '#B91C1C', 'الإسكندرية'),
    ('سوزوكي سويفت', 'suzuki-swift', 'قطع سوزوكي الصغيرة اللي بتعيش', 'سوزوكي', 'سويفت، ألتو، سياز، فيتارا، إرتيجا', None, 'fa-car', '#15803D', 'المنصورة'),
    ('هيونداي فيرنا وإلنترا', 'hyundai-verna-elantra', 'أشهر عربيات في مصر وقطعها عندنا', 'هيونداي', 'فيرنا، أكسنت، إلنترا HD وAD وCN7، توسان', None, 'fa-car', '#1D4ED8', 'مدينة نصر'),
    ('كيا سيراتو وسبورتاج', 'kia-cerato-sportage', 'كيا بكل موديلاتها', 'كيا', 'سيراتو، ريو، بيكانتو، سبورتاج، سيلتوس', None, 'fa-car', '#BE123C', 'الهرم'),
    ('بيجو ورينو', 'peugeot-renault', 'الفرنساوي محتاج حد فاهمه', 'بيجو / رينو', 'بيجو ٣٠١ و٥٠٨ و٢٠٠٨، رينو لوجان وميجان وداستر', None, 'fa-car', '#0F766E', 'الإسكندرية'),
    ('فيات ٢٠٠٠', 'fiat-2000', 'فيات تيبو و١٢٨ وشاهين وقطعهم', 'فيات', 'تيبو، ١٢٨، شاهين، سيينا، بونتو', None, 'fa-car', '#92400E', 'السبتية'),
    ('شيفروليه أفيو ولانوس', 'chevrolet-aveo-lanos', 'شيفروليه ودايو — القطع اللي بتدوّر عليها', 'شيفروليه', 'أفيو، لانوس، أوبترا، كروز، نوبيرا', None, 'fa-car', '#CA8A04', 'السبتية'),
    ('جيب وفورد', 'jeep-ford', 'قطع الأمريكاني الأصلي', 'جيب / فورد', 'جراند شيروكي، رانجلر، فورد فوكس، فييستا، كوجا', None, 'fa-truck-monster', '#1E2A57', 'التجمع'),
    ('دنيا الكاوتش', 'donia-el-kawetsh', 'كاوتش وجنوط لكل العربيات', 'كل الماركات', 'كل الموديلات — قول لنا المقاس المكتوب على الكاوتش', ['tires'], 'fa-circle-dot', '#111827', 'الحرفيين'),
    ('بطارية بلس', 'battery-plus', 'بطاريات بضمان وتركيب لحد عندك', 'كل الماركات', 'كل الموديلات — قول لنا الموديل والسنة', ['battery'], 'fa-car-battery', '#16A34A', 'المعادي'),
    ('زيوت وفلاتر المعادي', 'oils-filters-maadi', 'غيّر زيتك وفلاترك في معادهم', 'كل الماركات', 'كل الموديلات', ['filters'], 'fa-oil-can', '#D97706', 'المعادي'),
    ('فرامل سيفتي', 'brakes-safety', 'الفرامل مش لعبة — قطع مضمونة', 'كل الماركات', 'كل الموديلات الصيني والكوري والياباني والأوروبي', ['brakes'], 'fa-circle-stop', '#DC2626', 'مدينة نصر'),
    ('عفشة الوادي', 'afsha-elwadi', 'مساعدين ومقصات وجلب لكل العربيات', 'كل الماركات', 'كل الموديلات', ['suspension'], 'fa-car-burst', '#7C2D12', 'شبرا'),
    ('كهربا وبوجيهات أبو العز', 'abou-el-ezz-electric', 'بوجيهات ودينامو ومارش وحساسات', 'كل الماركات', 'كل الموديلات', ['ignition'], 'fa-bolt', '#EAB308', 'السبتية'),
    ('كشافات وفوانيس', 'lights-store', 'فوانيس ولمبات LED لكل العربيات', 'كل الماركات', 'كل الموديلات', ['lights', 'body'], 'fa-lightbulb', '#0EA5E9', 'الهرم'),
]


# أشهر الماركات في مصر — للمتاجر المتخصصة في نوع قطعة واحد
POPULAR_MAKES = [
    ('هيونداي', 'فيرنا، أكسنت، إلنترا، توسان'), ('كيا', 'سيراتو، ريو، سبورتاج'),
    ('تويوتا', 'كورولا، ياريس، فورتشنر'), ('نيسان', 'صني، سنترا، قشقاي'),
    ('شيري', 'تيجو، أريزو'), ('MG', 'MG5، ZS، RX5'), ('شيفروليه', 'أفيو، لانوس، أوبترا'),
    ('BMW', 'الفئة الثالثة والخامسة، X1، X3'), ('مرسيدس', 'C وE وGLC'),
    ('فولكس فاجن', 'جولف، باسات'), ('سكودا', 'أوكتافيا، سوبرب'), ('بيجو', '٣٠١، ٥٠٨، ٢٠٠٨'),
    ('رينو', 'لوجان، ميجان، داستر'), ('فيات', 'تيبو، ١٢٨'), ('ميتسوبيشي', 'لانسر، أتراج'),
    ('سوزوكي', 'سويفت، ألتو'), ('BYD', 'F3، L3'), ('جيلي', 'إمجراند، كولراي'),
]


def _upsert_store(Store, sector, sort, name, slug, tagline, icon, color, city, description, en):
    """🐛 [FIX]: كان update_or_create ⇒ أي إعادة زرع بتكتب فوق اللي صاحب المنصة عدّله من الأدمن.
    دلوقتي بننشئ بس، والإنجليزي بيتملا لو لسه فاضي."""
    store, created = Store.objects.get_or_create(
        slug=slug,
        defaults=dict(sector=sector, name=name, tagline=tagline, icon=icon, color=color,
                      city=city, sort=sort, description=description, is_active=True),
    )
    if not store.name_en:
        store.name_en, store.tagline_en, store.city_en, store.description_en = en
        store.save(update_fields=['name_en', 'tagline_en', 'city_en', 'description_en'])
    return store


def _upsert_item(Item, store, title, defaults, en):
    item, created = Item.objects.get_or_create(store=store, title=title, defaults=defaults)
    if not item.title_en:
        for k, v in en.items():
            setattr(item, k, v)
        item.save(update_fields=list(en))
    return created


def seed_catalog(Store, Item):
    """يزرع المتاجر والمنتجات (من غير ما يغيّر حاجة اتعدّلت من الأدمن). بيرجّع (متاجر، منتجات جديدة)."""
    rng = random.Random(2026)   # نفس النتيجة كل مرة
    new_items = 0
    print_en = {it[1]: en for it, en in zip(PRINT_ITEMS, PRINT_ITEMS_EN)}
    parts_en = {it[1]: en for it, en in zip(PARTS_ITEMS, PARTS_ITEMS_EN)}

    for sort, (name, slug, tagline, cats, icon, color, city) in enumerate(PRINT_STORES):
        desc = (f'{tagline}. بنستلم طلبك ونرد عليك بالسعر والمعاد في أسرع وقت. '
                'كل الطلبات بتتنفّذ وتتتابع عن طريق Mouss Tec.')
        name_en, tag_en, city_en = PRINT_STORES_EN[slug]
        desc_en = (f'{tag_en}. We receive your request and get back to you with the price and timing as soon '
                   'as possible. Every order is fulfilled and followed up by Mouss Tec.')
        store = _upsert_store(Store, 'printing', sort, name, slug, tagline, icon, color, city, desc,
                              (name_en, tag_en, city_en, desc_en))
        pool = [it for it in PRINT_ITEMS if it[0] in cats]
        rng.shuffle(pool)
        pool = pool[:rng.randint(10, 20)] if len(pool) > 10 else pool
        for i, (cat, title, d, min_qty) in enumerate(pool):
            t_en, d_en, q_en = print_en[title]
            new_items += _upsert_item(
                Item, store, title,
                dict(description=d, category=cat, icon=PRINT_CATEGORIES[cat][1], min_qty=min_qty, sort=i),
                dict(title_en=t_en, description_en=d_en, min_qty_en=q_en),
            )

    for sort, (name, slug, tagline, make, models_txt, cats, icon, color, city) in enumerate(PARTS_STORES):
        desc = (f'{tagline}. قول لنا الموديل والسنة (ولو معاك رقم الشاسيه أحسن) وإحنا نرد عليك '
                'بالسعر والمتاح. كل الطلبات بتتنفّذ وتتتابع عن طريق Mouss Tec.')
        name_en, tag_en, make_en, models_en, city_en = PARTS_STORES_EN[slug]
        desc_en = (f'{tag_en}. Tell us the model and year (the VIN is even better) and we will get back to you '
                   'with the price and availability. Every order is fulfilled and followed up by Mouss Tec.')
        store = _upsert_store(Store, 'automotive', sort, name, slug, tagline, icon, color, city, desc,
                              (name_en, tag_en, city_en, desc_en))
        pool = [it for it in PARTS_ITEMS if cats is None or it[0] in cats]
        if cats is None:
            entries = [(it, make, models_txt, make_en, models_en) for it in pool]
        else:
            # 🔧 متجر متخصص (كاوتش/بطاريات/فرامل…) ⇒ القطعة لكل ماركة مشهورة عشان المتجر مايبقاش فاضي
            entries = []
            for it in pool:
                if '{make}' in it[1]:
                    entries += [(it, mk, mdl) + POPULAR_MAKES_EN[mk] for mk, mdl in POPULAR_MAKES]
                else:
                    entries.append((it, '', models_txt, '', models_en))
        rng.shuffle(entries)
        entries = entries[:rng.randint(12, 20)]
        for i, ((cat, title_t, d_t, condition), mk, mdl, mk_en, mdl_en) in enumerate(entries):
            t_en_t, d_en_t = parts_en[title_t]
            new_items += _upsert_item(
                Item, store, title_t.format(make=mk or make),
                dict(description=d_t.format(make=mk or make, models=mdl), category=cat,
                     icon=PARTS_CATEGORIES[cat][1], car_make=mk, car_models=mdl, condition=condition, sort=i),
                dict(title_en=t_en_t.format(make=mk_en or make_en), description_en=d_en_t.format(models=mdl_en, make=mk_en or make_en),
                     car_make_en=mk_en, car_models_en=mdl_en, condition_en=CONDITION_EN.get(condition, '')),
            )

    return Store.objects.count(), new_items


# ═══════════════════════════════ 🌐 النسخة الإنجليزي ═══════════════════════════════
# بنفس ترتيب القوايم العربي فوق (فيه assert تحت يتأكد إن العدد مطابق).

PRINT_CATEGORIES_EN = {
    'cards': 'Business cards', 'flyers': 'Flyers, brochures & menus', 'notebooks': 'Notebooks, planners & calendars',
    'mugs': 'Mugs', 'pens': 'Pens', 'gifts': 'Gifts & promo items', 'apparel': 'T-shirts & uniforms',
    'banners': 'Banners & signs', 'stickers': 'Stickers & labels', 'packaging': 'Boxes, packaging & bags',
    'design': 'Logo & brand identity', 'social': 'Social media designs', 'events': 'Invitations & events',
    'office': 'Business & clinic stationery',
}

# (الاسم، الوصف، أقل كمية) — بنفس ترتيب PRINT_ITEMS
PRINT_ITEMS_EN = [
    ('Business cards, 350gsm coated', 'Thick, classy card, printed both sides, matte or gloss lamination. We tidy up the design for free if you have a logo.', '100 cards'),
    ('Business cards with spot UV', 'Your logo shines on top of a matte card — a premium look every client notices.', '250 cards'),
    ('Embossed business cards', 'Your name or logo raised so you can feel it. Great for lawyers, doctors and business owners.', '250 cards'),
    ('Foil business cards (gold/silver)', 'Gold or silver foil on the logo or name — looks expensive at first glance.', '250 cards'),
    ('Clear plastic business cards', 'Transparent PVC card that will not tear or get wet — different and nobody throws it away.', '100 cards'),
    ('Loyalty & discount cards', 'Stamp or points card that brings customers back. Numbering available.', '500 cards'),
    ('A5 flyers, double-sided', 'Flyers for promotions and offers on 150gsm coated paper, vivid colours.', '1000 flyers'),
    ('Tri-fold brochure', 'A brochure that explains all your services on one neatly folded sheet.', '500 brochures'),
    ('Laminated restaurant menu', 'A menu that survives food and water, laminated on both sides.', '20 menus'),
    ('Delivery menu (light paper)', 'Menus sent with orders and handed out to homes — cheap and they bring orders.', '2000 sheets'),
    ('Stapled product catalog', '12 to 48 page product catalog, thick cover and coated inner pages.', '100 copies'),
    ('A3 and A2 posters', 'Posters for shops, clinics and schools, high-quality digital print.', '10 posters'),
    ('Spiral notebook with your logo cover', '100-sheet spiral notebook with your design on the cover — a gift for staff and clients.', '50 notebooks'),
    ('Leather planner with company name', 'Yearly planner in genuine or faux leather, name engraved or printed on the cover.', '25 planners'),
    ('Small A6 gift notebooks', 'Pocket notebooks that fit in a bag — perfect for exhibitions and events.', '100 notebooks'),
    ('Wall & desk calendars with your design', 'Next year\'s calendar with your products or work — in front of the client for 12 months.', '100 calendars'),
    ('Carbon invoice & receipt books', 'Duplicate or triplicate books, numbered, with your shop name and tax number.', '10 books'),
    ('Kids colouring & activity books', 'Educational books with your design for nurseries, schools and tutoring centres.', '100 books'),
    ('Printed white ceramic mug', '11oz mug, the print survives washing. Photo, logo or text.', '1 mug'),
    ('Magic colour-changing mug', 'The mug is black and the picture appears with a hot drink — a surprise gift.', '1 mug'),
    ('Mug with coloured handle and inside', 'Many colours inside and on the handle to match your brand.', '12 mugs'),
    ('Laser-engraved stainless tumbler', 'Keeps drinks hot or cold, name or logo laser-engraved so it never wears off.', '10 tumblers'),
    ('Frosted glass mug', 'Elegant glass mug with a soft print — great for cafes and gifts.', '12 mugs'),
    ('Gift mug set in a box', '2 or 4 mugs in a printed box — ready to give straight away.', '10 sets'),
    ('Plastic promo pens with logo', 'Promotional ballpoint pen, many colours, print on the barrel or clip.', '100 pens'),
    ('Laser-engraved metal pens', 'Heavy, elegant pen with laser-engraved name — a gift for VIP clients and conferences.', '25 pens'),
    ('Pen & keychain gift set', 'Gift-boxed set, with your name or logo.', '25 sets'),
    ('Whiteboard markers & highlighters with your name', 'For schools, tutoring centres and companies — your name on every pen.', '100 pens'),
    ('Stylus touch pen', 'Writes and works on phone screens, logo printed.', '50 pens'),
    ('Leather & acrylic keychains', 'Keychain with your logo or name, any shape you want.', '50 keychains'),
    ('Branded power bank', 'A gift the client uses every day with your name in front of them.', '20 pieces'),
    ('USB flash drives with your name', '16 or 32GB flash drive, card shape or engraved wood.', '25 drives'),
    ('Award shields & certificates', 'Engraved acrylic or wood shields for awards and occasions.', '1 shield'),
    ('Corporate gift box', 'A box with a pen, planner, mug and keychain — all with your brand.', '10 boxes'),
    ('Printed mouse pad', 'Mouse pad with your photo or design, non-slip rubber base.', '25 pieces'),
    ('Embroidered caps', 'Cotton caps with your logo embroidered — for teams, companies and trips.', '25 caps'),
    ('Printed cotton tote bags', 'Strong cotton tote bag, silk-screen or DTF print.', '50 bags'),
    ('Laser engraving on wood & leather', 'Any design engraved on wood, leather or acrylic — one piece or bulk.', '1 piece'),
    ('DTF printed cotton T-shirts', '100% cotton T-shirt, full-colour DTF print that does not crack.', '10 T-shirts'),
    ('Restaurant & cafe uniforms', 'T-shirt or polo, cap and apron with your logo — a smart, unified look.', '10 sets'),
    ('Corporate polo uniforms', 'Pique polo with chest embroidery in company colours.', '20 pieces'),
    ('Medical scrubs & uniforms', 'Scrubs for doctors and nurses with embroidered name and specialty.', '5 sets'),
    ('Sweatshirts & hoodies with your design', 'Heavy cotton hoodie for winter, printed or embroidered.', '10 pieces'),
    ('School & nursery uniforms', 'Full school uniform with embroidered logo — sizes from nursery to high school.', '20 sets'),
    ('Event & marathon T-shirts', 'Light dri-fit T-shirt with sponsor names, for marathons and conferences.', '50 T-shirts'),
    ('Printed reflective safety vests', 'Reflective vests for sites, factories and garages, name printed on the back.', '20 vests'),
    ('Roll-up banner stand', '85×200 roll-up with carry bag, sets up in seconds at any exhibition or branch.', '1 roll-up'),
    ('Flex banner for shops', 'Flex banner in any size with metal eyelets, sun-resistant.', '1 banner'),
    ('Illuminated shop sign', 'Light box or raised letters with LED lighting — your shop stands out at night.', '1 sign'),
    ('Vinyl stickers for cars & storefronts', 'Car wrap or glass storefront with printed vinyl.', '1 metre'),
    ('Directional signs for offices & clinics', 'Door, office and arrow signs in acrylic or foam board.', '5 signs'),
    ('Backdrop for conferences & photos', 'Large logo backdrop for photos and events.', '1 piece'),
    ('Die-cut logo stickers', 'Vinyl sticker cut to the shape of your logo, water-resistant.', '100 stickers'),
    ('Product labels (on a roll)', 'Labels on a roll for boxes, jars and handmade products.', '500 labels'),
    ('Kids & educational stickers', 'Sticker sheets with your design for nurseries and rewards.', '50 sheets'),
    ('Security seal stickers (hologram)', 'Tamper-evident sticker that tears when opened — for boxes and warranty.', '500 stickers'),
    ('Printed cardboard product boxes', 'A box sized to your product with your design — packaging sells first.', '100 boxes'),
    ('Kraft paper bags with logo', 'Paper bags for shops, pharmacies and clothing, twisted or die-cut handles.', '100 bags'),
    ('Printed pizza & food boxes', 'Pizza, burger and sandwich boxes with the restaurant name.', '500 boxes'),
    ('Rigid magnetic gift boxes', 'Premium box with a magnetic closure for perfume, accessories and gifts.', '50 boxes'),
    ('Printed plastic bags', 'Branded bags for shops and supermarkets.', '1000 bags'),
    ('Professional logo design', '3 different concepts and revisions until you are happy, with all source files.', '1 design'),
    ('Full brand identity', 'Logo, colours, fonts, business card, letterhead and envelope — all in one style.', 'Package'),
    ('Packaging & product box design', 'Box and label design, print-ready with exact dimensions.', '1 design'),
    ('Restaurant or cafe menu design', 'A good-looking, easy-to-read menu in Arabic and English.', '1 design'),
    ('Redraw an old logo as vector', 'Old blurry logo? We redraw it in high quality for any print.', '1 logo'),
    ('Monthly social media design package', '12 or 20 posts a month in your brand colours, with captions if you like.', 'Monthly package'),
    ('Facebook cover & profile picture', 'Cover and profile sized correctly for every platform.', '1 design'),
    ('Story & reels templates', 'Story templates and reels covers you can reuse every time.', '10 designs'),
    ('Sponsored ad design', 'An eye-catching ad that follows Facebook and Instagram rules.', '1 design'),
    ('Wedding invitations', 'Many wedding invitation designs, premium paper and envelope, foil if you like.', '100 invitations'),
    ('Engagement, katb ketab & aqiqah invitations', 'Occasion cards in many styles, with matching giveaways.', '50 invitations'),
    ('Event giveaways & tags', 'Tags and stickers for chocolates and giveaways with names and date.', '100 pieces'),
    ('Conference badges & lanyard cards', 'Badge with name and photo plus lanyard — for conferences and exhibitions.', '50 badges'),
    ('Letterhead & envelopes', 'Correspondence paper and envelopes with company name and address.', '500 sheets'),
    ('Clinic prescriptions & appointment cards', 'Prescription pads with the doctor\'s name and specialty, plus appointment books.', '10 pads'),
    ('Pocket folders', 'Rigid folder with a pocket for offers and contracts, with your logo.', '100 folders'),
    ('Warranty cards & product certificates', 'Numbered warranty card or quality certificate to ship with the product.', '500 cards'),
    ('Self-inking company stamps', 'Self-inking stamp with your details and tax number.', '1 stamp'),
]

# slug ⇒ (الاسم، الجملة، المدينة)
PRINT_STORES_EN = {
    'nile-cards': ('Nile Cards Press', 'Your card speaks for you before you do', 'Cairo'),
    'koubaya-w-hekaya': ('Mug & Story', 'Every mug has a story — tell us yours', 'Giza'),
    'qalamak-maak': ('Your Pen', 'Promo pens that stay in your client\'s hand', 'Cairo'),
    'daftargy': ('Daftargy', 'Notebooks, planners and calendars with your brand', 'Alexandria'),
    'uniform-masr': ('Uniform Masr', 'Uniforms that make your team look like a team', 'Cairo'),
    'tshirt-studio': ('T-Shirt Studio', 'Print whatever is on your mind on a T-shirt', 'Maadi'),
    'banners-giza': ('Giza Banners', 'Banners, roll-ups and signs the same day', 'Giza'),
    'sticker-town': ('Sticker Town', 'Stickers and labels in any shape and size', 'Nasr City'),
    'elba-w-taghleef': ('Box & Pack', 'Good packaging sells by itself', '10th of Ramadan'),
    'corporate-gifts': ('Corporate Gifts', 'Branded gifts for your clients and staff', 'Fifth Settlement'),
    'studio-logo': ('Studio Logo', 'A logo that lasts for years', 'Online'),
    'social-pro': ('Social Pro', 'Posts that bring engagement and sales', 'Online'),
    'farha-invitations': ('Farha Invitations', 'Invitations and giveaways for your happiest days', 'Mansoura'),
    'offset-alex': ('Alexandria Offset', 'Bulk quantities at wholesale prices', 'Alexandria'),
    'digital-print-dokki': ('Dokki Digital Print', 'Fast printing, as much as you need', 'Dokki'),
    'yafta-w-aktar': ('Signs & More', 'Make your shop visible from the end of the street', 'Shubra'),
    'tatreez-elwadi': ('El Wadi Embroidery', 'Computer embroidery with fine detail', 'Assiut'),
    'madrasty-uniform': ('Madrasty School Uniforms', 'School uniforms and notebooks with the school logo', 'Tanta'),
    'mansoura-modern-press': ('Mansoura Modern Press', 'All your printing under one roof', 'Mansoura'),
    'graphic-house': ('Graphic House', 'Design and packaging that set you apart', 'Sheikh Zayed'),
    'cap-w-shanta': ('Cap & Bag', 'Caps, bags and T-shirts for teams and trips', 'Obour'),
    'laser-craft': ('Laser Craft', 'Laser engraving on any material', 'Helwan'),
    'menu-w-mataem': ('Menus & Restaurants', 'Everything a restaurant prints, from menu to box', 'Heliopolis'),
    'calendar-print': ('Calendar Print', 'Calendars and planners in front of your client all year', 'Zagazig'),
    'medical-print': ('Medical Print', 'Prescriptions, files and scrubs for clinics and hospitals', 'Mohandessin'),
    'print-express-haram': ('Print Express Haram', 'Order in the morning, collect at night', 'Haram'),
}

PARTS_CATEGORIES_EN = {
    'ignition': 'Spark plugs & electrics', 'brakes': 'Brakes', 'suspension': 'Suspension & shocks',
    'filters': 'Oils & filters', 'tires': 'Tyres & rims', 'battery': 'Batteries', 'cooling': 'Cooling & A/C',
    'engine': 'Engine & belts', 'lights': 'Lights', 'body': 'Body & accessories',
}

CONDITION_EN = {'أصلي': 'Original', 'جديد': 'New', 'جديد أصلي': 'New original', 'جديد أو استيراد': 'New or imported'}

# (الاسم، الوصف) بنفس ترتيب PARTS_ITEMS — {make} و{models}
PARTS_ITEMS_EN = [
    ('Iridium spark plugs - {make}', 'Iridium plugs that last 60,000 km, smoother idle and better fuel economy. Fits {models}.'),
    ('Standard (nickel) spark plugs - {make}', 'Economical spark plug set for routine service — {models}.'),
    ('Ignition coil - {make}', 'New coil if the car misfires or the engine light is on — {models}.'),
    ('Alternator - {make}', 'Alternator that charges properly, guaranteed — {models}.'),
    ('Starter motor - {make}', 'Strong starter for a car that cranks slowly — {models}.'),
    ('Sensors (O2 / crank / cam) - {make}', 'All sensor types; send us the part number or fault code and we will find it — {models}.'),
    ('Front brake pads - {make}', 'Ceramic pads that stop well with little noise and dust — {models}.'),
    ('Rear brake pads - {make}', 'Rear pads or drum shoes depending on the model — {models}.'),
    ('Brake discs (rotors) - {make}', 'New discs to replace worn ones or a pulsing pedal — {models}.'),
    ('DOT4 brake fluid', 'Genuine brake fluid; change it every two years to keep brakes sharp.'),
    ('Front shock absorbers - {make}', 'Oil or gas shocks so the car feels steady over bumps again — {models}.'),
    ('Rear shock absorbers - {make}', 'Rear shock set with mounts and bushes — {models}.'),
    ('Control arms & bushes - {make}', 'Complete arms or just bushes if you hear knocking in the steering — {models}.'),
    ('Wheel bearings - {make}', 'If there is a hum that grows with speed, it is the bearing — {models}.'),
    ('Stabiliser links & anti-roll bar - {make}', 'Front and rear links and anti-roll bar bushes — {models}.'),
    ('Oil filter - {make}', 'Genuine oil filter for your engine — {models}.'),
    ('Air filter - {make}', 'A clean air filter means more torque and less fuel — {models}.'),
    ('Cabin (A/C) filter - {make}', 'Carbon cabin filter that blocks dust and smells — {models}.'),
    ('Fuel filter - {make}', 'Fuel filter that protects the pump and injectors — {models}.'),
    ('Full synthetic engine oil', '5W-30 or 5W-40 from trusted brands, 4 or 5 litre jugs.'),
    ('Automatic transmission fluid', 'ATF to factory spec; we can change it by machine if you like.'),
    ('{make} tyres in original sizes', 'Michelin, Bridgestone, Hankook and budget brands, in the original size for {models}.'),
    ('Sport rims - {make}', 'Alloy rims from 15 to 19 inches — {models}.'),
    ('Spare wheel & tool kit', 'Spare wheel in your car\'s size with jack and wrench.'),
    ('Battery - {make}', 'Battery with the right amp rating for the car, with warranty — {models}.'),
    ('AGM battery for start-stop cars', 'For cars that switch off at traffic lights — they need AGM.'),
    ('Radiator - {make}', 'New aluminium radiator if the car overheats — {models}.'),
    ('Water pump - {make}', 'Water pump with gasket — {models}.'),
    ('A/C compressor - {make}', 'New or imported compressor in excellent condition — {models}.'),
    ('Radiator fan & thermostat - {make}', 'Electric fan, thermostat and temperature sensor — {models}.'),
    ('Radiator coolant', 'Ready-to-use coolant that prevents rust and overheating.'),
    ('Timing belt kit - {make}', 'Timing belt with idler and tensioner — change it on time so the engine is not damaged — {models}.'),
    ('Accessory belt (alternator/A/C) - {make}', 'Accessory belt with tensioner — {models}.'),
    ('Timing chain kit - {make}', 'Chain, gears and tensioner for chain-driven engines — {models}.'),
    ('Head gasket & gasket set - {make}', 'Head gasket, valve cover gasket and the full set — {models}.'),
    ('Engine & gearbox mounts - {make}', 'If the car shakes at idle, it is the mounts — {models}.'),
    ('Fuel pump - {make}', 'Complete fuel pump with float — {models}.'),
    ('Injectors - {make}', 'New injectors, or we ultrasonically clean the old ones — {models}.'),
    ('Headlight - {make}', 'Complete left or right headlight, standard or LED — {models}.'),
    ('Tail light - {make}', 'Left and right tail lights — {models}.'),
    ('LED & xenon bulbs', 'Bright white LED bulbs in every size (H1, H4, H7, H11…).'),
    ('Fog lights - {make}', 'Fog lights with frame and switch — {models}.'),
    ('Front & rear bumpers - {make}', 'New bumper ready for paint — {models}.'),
    ('Side mirrors - {make}', 'Complete electric or manual mirror, with indicator if fitted — {models}.'),
    ('Wiper blades - {make}', 'Wiper set in your car\'s sizes, wipes without noise — {models}.'),
    ('Floor mats & seat covers - {make}', 'Custom-fit 5D mats and leather seat covers — {models}.'),
    ('Android screen & rear camera - {make}', 'Dash-fit screen with frame plus reversing camera — {models}.'),
]

# slug ⇒ (الاسم، الجملة، الماركة، الموديلات، المدينة)
PARTS_STORES_EN = {
    'el-seeny-el-asly': ('The Original Chinese', 'Genuine Chery and Geely parts, no runaround', 'Chery', 'Tiggo 3, Tiggo 7, Arrizo 5, Arrizo 6', 'El Herafiyeen'),
    'byd-center': ('BYD Center', 'Everything you need for your BYD', 'BYD', 'F3, L3, S5, Song', 'Nasr City'),
    'mg-parts': ('MG Parts', 'Genuine and guaranteed aftermarket MG parts', 'MG', 'MG5, MG6, ZS, RX5', 'New Cairo'),
    'geely-proton': ('Geely & Proton', 'Geely and Proton specialists for years', 'Geely', 'Emgrand, Coolray, Proton Saga, Gen-2', 'Shubra'),
    'changan-haval': ('Changan & Haval', 'Parts for the new generation of Chinese cars', 'Changan', 'CS35, CS55, Alsvin, Haval H6, Jolion', 'Obour'),
    'bavaria-parts': ('Bavaria Parts', 'Genuine and OEM BMW parts at fair prices', 'BMW', 'E90, F30, G20, X1, X3, X5', 'Mohandessin'),
    'mercedes-house': ('Mercedes House', 'Every Mercedes part from C to S', 'Mercedes', 'C180, C200, E200, E250, GLC', 'Heliopolis'),
    'vw-skoda': ('VW & Skoda', 'Golf, Octavia and Passat — genuine parts', 'Volkswagen / Skoda', 'Golf 6 & 7, Passat, Octavia A5 & A7, Superb', 'Dokki'),
    'opel-germany': ('Opel Germany', 'Opel Astra, Corsa and Vectra parts', 'Opel', 'Astra H & J, Corsa, Vectra, Insignia', 'Giza'),
    'toyota-original': ('Toyota Original', 'Toyota parts that never let you down', 'Toyota', 'Corolla, Yaris, Camry, RAV4, Fortuner', 'El Herafiyeen'),
    'nissan-sunny': ('Nissan & Sunny', 'Sunny, Sentra, Qashqai — all in stock', 'Nissan', 'Sunny, Sentra, Qashqai, Juke, X-Trail', 'Shubra El Kheima'),
    'mitsubishi-lancer': ('Mitsubishi Lancer Center', 'Lancer, Pajero and Attrage', 'Mitsubishi', 'Lancer, Attrage, Pajero, Eclipse Cross', 'Alexandria'),
    'suzuki-swift': ('Suzuki Swift', 'Parts for small Suzukis that last', 'Suzuki', 'Swift, Alto, Ciaz, Vitara, Ertiga', 'Mansoura'),
    'hyundai-verna-elantra': ('Hyundai Verna & Elantra', 'The most popular cars in Egypt — and their parts', 'Hyundai', 'Verna, Accent, Elantra HD, AD & CN7, Tucson', 'Nasr City'),
    'kia-cerato-sportage': ('Kia Cerato & Sportage', 'Kia in every model', 'Kia', 'Cerato, Rio, Picanto, Sportage, Seltos', 'Haram'),
    'peugeot-renault': ('Peugeot & Renault', 'French cars need someone who knows them', 'Peugeot / Renault', 'Peugeot 301, 508, 2008; Renault Logan, Megane, Duster', 'Alexandria'),
    'fiat-2000': ('Fiat 2000', 'Fiat Tipo, 128, Shahin and their parts', 'Fiat', 'Tipo, 128, Shahin, Siena, Punto', 'Sabtiyya'),
    'chevrolet-aveo-lanos': ('Chevrolet Aveo & Lanos', 'Chevrolet and Daewoo — the parts you are looking for', 'Chevrolet', 'Aveo, Lanos, Optra, Cruze, Nubira', 'Sabtiyya'),
    'jeep-ford': ('Jeep & Ford', 'Genuine American parts', 'Jeep / Ford', 'Grand Cherokee, Wrangler, Ford Focus, Fiesta, Kuga', 'New Cairo'),
    'donia-el-kawetsh': ('Tyre World', 'Tyres and rims for every car', 'All makes', 'All models — tell us the size written on the tyre', 'El Herafiyeen'),
    'battery-plus': ('Battery Plus', 'Batteries with warranty and installation at your door', 'All makes', 'All models — tell us the model and year', 'Maadi'),
    'oils-filters-maadi': ('Maadi Oils & Filters', 'Change your oil and filters on time', 'All makes', 'All models', 'Maadi'),
    'brakes-safety': ('Safety Brakes', 'Brakes are not a game — guaranteed parts', 'All makes', 'All Chinese, Korean, Japanese and European models', 'Nasr City'),
    'afsha-elwadi': ('El Wadi Suspension', 'Shocks, arms and bushes for every car', 'All makes', 'All models', 'Shubra'),
    'abou-el-ezz-electric': ('Abou El Ezz Auto Electric', 'Spark plugs, alternators, starters and sensors', 'All makes', 'All models', 'Sabtiyya'),
    'lights-store': ('Lights & Lamps', 'Headlights and LED bulbs for every car', 'All makes', 'All models', 'Haram'),
}

POPULAR_MAKES_EN = {
    'هيونداي': ('Hyundai', 'Verna, Accent, Elantra, Tucson'), 'كيا': ('Kia', 'Cerato, Rio, Sportage'),
    'تويوتا': ('Toyota', 'Corolla, Yaris, Fortuner'), 'نيسان': ('Nissan', 'Sunny, Sentra, Qashqai'),
    'شيري': ('Chery', 'Tiggo, Arrizo'), 'MG': ('MG', 'MG5, ZS, RX5'), 'شيفروليه': ('Chevrolet', 'Aveo, Lanos, Optra'),
    'BMW': ('BMW', '3 and 5 Series, X1, X3'), 'مرسيدس': ('Mercedes', 'C, E and GLC'),
    'فولكس فاجن': ('Volkswagen', 'Golf, Passat'), 'سكودا': ('Skoda', 'Octavia, Superb'), 'بيجو': ('Peugeot', '301, 508, 2008'),
    'رينو': ('Renault', 'Logan, Megane, Duster'), 'فيات': ('Fiat', 'Tipo, 128'), 'ميتسوبيشي': ('Mitsubishi', 'Lancer, Attrage'),
    'سوزوكي': ('Suzuki', 'Swift, Alto'), 'BYD': ('BYD', 'F3, L3'), 'جيلي': ('Geely', 'Emgrand, Coolray'),
}

assert len(PRINT_ITEMS_EN) == len(PRINT_ITEMS), (len(PRINT_ITEMS_EN), len(PRINT_ITEMS))
assert len(PARTS_ITEMS_EN) == len(PARTS_ITEMS), (len(PARTS_ITEMS_EN), len(PARTS_ITEMS))
assert set(PRINT_STORES_EN) == {s[1] for s in PRINT_STORES}
assert set(PARTS_STORES_EN) == {s[1] for s in PARTS_STORES}
assert set(POPULAR_MAKES_EN) == {m for m, _ in POPULAR_MAKES}
