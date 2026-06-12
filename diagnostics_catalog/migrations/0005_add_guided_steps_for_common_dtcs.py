"""
🩺 Guided Steps Population — 2026-06-12

يضيف `guided_steps` و `likely_oem_parts` لأكتر ~60 كود شائع في الورش
عشان السيستم يبقى مساعد تشخيص حقيقي مش مجرد قاموس.

كل خطوة على شكل:
    {"step": N, "title": "...", "action": "ايه يعمل الفني",
     "expected": "النتيجة المتوقعة"}

التعديل لا يلمس باقي حقول الكود (severity, descriptions) — بـ update فقط
guided_steps و likely_oem_parts اللي كانوا فاضيين.
"""
from django.db import migrations


# code → (guided_steps, likely_oem_parts)
GUIDANCE = {
    # ═══════════════════════════════════════════════════════════════════
    # 🔥 MISFIRE — الأكثر شيوعًا
    # ═══════════════════════════════════════════════════════════════════
    'P0300': (
        [
            {"step": 1, "title": "اقرأ Freeze Frame", "action": "سجل RPM والحمل وحرارة المحرك وقت ظهور العطل", "expected": "تحديد ظروف الـ misfire"},
            {"step": 2, "title": "افحص لمبات الإشعال (كويلات)", "action": "بدّل الكويل من سلندر سليم للسلندر المتأثر وأعد القياس", "expected": "لو الـ misfire اتنقل = الكويل تالفة"},
            {"step": 3, "title": "افحص البواجي", "action": "اطلع البواجي وافحص لون السنام والمسافة بين الإلكترودات", "expected": "بواجي نضيفة ومسافة مظبوطة"},
            {"step": 4, "title": "افحص الإنجكتورز", "action": "قس مقاومة كل إنجكتور بالأوميتر", "expected": "نفس القراءة لكل الإنجكتورز (±5%)"},
            {"step": 5, "title": "Compression Test", "action": "قس ضغط كل سلندر", "expected": "اختلاف أقل من 15% بين السلندرات"},
        ],
        ["12120037244", "BP6ES", "5C1684"]
    ),
    'P0301': (
        [
            {"step": 1, "title": "حدد السلندر #1", "action": "العطل في السلندر الأول تحديدًا", "expected": "ركّز فحصك على السلندر 1"},
            {"step": 2, "title": "افحص كويل/شمعة سلندر 1", "action": "بدّل كويل سلندر 1 مع سلندر تاني", "expected": "لو العطل انتقل = الكويل تالفة"},
            {"step": 3, "title": "افحص الإنجكتور", "action": "اسمع صوت الإنجكتور بسماعة وقس مقاومته", "expected": "صوت طقطقة منتظم، مقاومة 12-16Ω"},
            {"step": 4, "title": "Compression السلندر", "action": "قس ضغط السلندر 1", "expected": "≥ 120 PSI وقريب من باقي السلندرات"},
        ],
        []
    ),
    'P0302': (
        [
            {"step": 1, "title": "حدد السلندر #2", "action": "العطل في السلندر التاني", "expected": "نفس فحص P0301 على سلندر 2"},
            {"step": 2, "title": "بدّل كويل/شمعة", "action": "بدّل مع سلندر تاني", "expected": "تتبع انتقال العطل"},
        ],
        []
    ),

    # ═══════════════════════════════════════════════════════════════════
    # 🔥 MAF / MAP / O2 — حساسات الهواء والوقود
    # ═══════════════════════════════════════════════════════════════════
    'P0171': (
        [
            {"step": 1, "title": "افحص تسريب هواء", "action": "رش منظف الكربوريتر حوالين مانيفولد الدخول والخراطيم", "expected": "لو RPM تغير = فيه تسريب"},
            {"step": 2, "title": "نظّف سينسور MAF", "action": "استخدم MAF cleaner مخصوص (مش كربوريتر كلينر!)", "expected": "السينسور نضيف من الأتربة والزيت"},
            {"step": 3, "title": "افحص فلتر الهواء", "action": "تأكد إن الفلتر مش مسدود ولا مبلل", "expected": "فلتر نضيف"},
            {"step": 4, "title": "افحص ضغط البنزين", "action": "قس ضغط مسطرة الإنجكتورز", "expected": "ضمن نطاق الشركة (عادة 40-60 PSI)"},
            {"step": 5, "title": "افحص قراءات Fuel Trim", "action": "اقرأ STFT و LTFT بالأوبد", "expected": "LTFT أقل من +10%"},
        ],
        []
    ),
    'P0174': (
        [
            {"step": 1, "title": "افحص تسريب هواء على البنك 2", "action": "نفس فحص P0171 لكن ركّز على نص الموتور البنك 2", "expected": "اكتشاف أي تسريب"},
            {"step": 2, "title": "قارن O2 البنكين", "action": "اقرأ قيم O2 على البنكين", "expected": "الاختلاف يحدد لو المشكلة في بنك واحد بس"},
        ],
        []
    ),
    'P0172': (
        [
            {"step": 1, "title": "افحص ضغط البنزين", "action": "ممكن ريجوليتر ضغط البنزين تالف ويرفع الضغط", "expected": "ضغط ضمن المواصفات"},
            {"step": 2, "title": "افحص الإنجكتورز", "action": "ممكن إنجكتور بيسرّب", "expected": "لا يوجد قطر بنزين"},
            {"step": 3, "title": "افحص MAF", "action": "نظّف أو بدّل", "expected": "قراءات MAF صحيحة على RPM مختلفة"},
        ],
        []
    ),
    'P0101': (
        [
            {"step": 1, "title": "نظّف MAF", "action": "بـ MAF cleaner مخصوص — لا تلمس السلك بإيدك", "expected": "سينسور نضيف"},
            {"step": 2, "title": "افحص قراءة MAF بـ Live Data", "action": "قارن g/sec على KOEO و KOER", "expected": "0 g/s على KOEO، 3-6 g/s على idle"},
            {"step": 3, "title": "افحص خرطوم الدخول", "action": "تأكد من عدم وجود شقوق بعد MAF", "expected": "خرطوم سليم"},
        ],
        []
    ),
    'P0420': (
        [
            {"step": 1, "title": "اقرأ O2 بعد وقبل الكتلايزر", "action": "Live data للسينسور 1 والسينسور 2", "expected": "S1 يتأرجح بسرعة، S2 يبقى ثابت تقريبًا"},
            {"step": 2, "title": "افحص تسريب عادم قبل الكتلايزر", "action": "افحص كولّكتور والشمبر", "expected": "لا تسريب"},
            {"step": 3, "title": "افحص misfire history", "action": "أي misfire يدمر الكتلايزر", "expected": "صفر misfire"},
            {"step": 4, "title": "افحص O2 الخلفي", "action": "لو O2 الخلفي تالف يطلع P0420 وهمي", "expected": "سينسور سليم"},
            {"step": 5, "title": "Backpressure Test", "action": "قس الضغط الخلفي للعادم", "expected": "أقل من 2 PSI على 2500 RPM"},
        ],
        []
    ),
    'P0430': (
        [
            {"step": 1, "title": "نفس فحص P0420 لكن على البنك 2", "action": "افحص O2 على بنك 2 قبل وبعد الكتلايزر", "expected": "نفس النمط"},
        ],
        []
    ),

    # ═══════════════════════════════════════════════════════════════════
    # 🔥 EVAP — الأكثر شكوى من العملاء
    # ═══════════════════════════════════════════════════════════════════
    'P0442': (
        [
            {"step": 1, "title": "افحص غطاء التنك", "action": "اقفله جيدًا واسمع صوت click", "expected": "غطاء محكم"},
            {"step": 2, "title": "افحص خراطيم EVAP", "action": "تتبع الخراطيم بصريًا", "expected": "لا تشققات"},
            {"step": 3, "title": "Smoke Test", "action": "استخدم ماكينة دخان EVAP", "expected": "تحديد مكان التسريب"},
            {"step": 4, "title": "افحص Purge Valve", "action": "قس مقاومة وفلطية", "expected": "ضمن المواصفات"},
        ],
        []
    ),
    'P0455': (
        [
            {"step": 1, "title": "افحص غطاء التنك أول حاجة", "action": "اقفل بقوة ومسح العطل", "expected": "العطل يختفي"},
            {"step": 2, "title": "Smoke Test", "action": "ابحث عن تسريب كبير", "expected": "تحديد المكان"},
        ],
        []
    ),
    'P0440': (
        [
            {"step": 1, "title": "افحص غطاء التنك + شبكة EVAP", "action": "نفس بروتوكول P0442", "expected": "إصلاح التسريب"},
        ],
        []
    ),

    # ═══════════════════════════════════════════════════════════════════
    # 🔥 VVT / Timing
    # ═══════════════════════════════════════════════════════════════════
    'P0011': (
        [
            {"step": 1, "title": "افحص ضغط الزيت", "action": "VVT يعتمد على ضغط الزيت", "expected": "ضغط ضمن المواصفات على idle"},
            {"step": 2, "title": "افحص فلتر/سولينويد VVT", "action": "افتح وافحص الشبكة الفلترية", "expected": "نضيفة، غير مسدودة"},
            {"step": 3, "title": "افحص مستوى ولزوجة الزيت", "action": "زيت قديم أو رقيق يسبب المشكلة", "expected": "زيت تركيز صحيح وحديث"},
            {"step": 4, "title": "افحص Cam Actuator", "action": "بـ scan tool حرّك VVT", "expected": "استجابة فورية"},
        ],
        []
    ),
    'P0014': (
        [
            {"step": 1, "title": "نفس فحص P0011 على كامة العادم", "action": "ركّز على سولينويد VVT العادم", "expected": "سولينويد يستجيب"},
        ],
        []
    ),

    # ═══════════════════════════════════════════════════════════════════
    # 🔥 سينسورات الكرنك والكامة
    # ═══════════════════════════════════════════════════════════════════
    'P0335': (
        [
            {"step": 1, "title": "افحص سينسور CKP", "action": "قس المقاومة والإشارة (AC voltage على scope)", "expected": "إشارة AC منتظمة"},
            {"step": 2, "title": "افحص ترس CKP", "action": "تأكد من عدم وجود أسنان مكسورة", "expected": "ترس سليم"},
            {"step": 3, "title": "افحص الكنكتور والأسلاك", "action": "تأكد من نظافة الكنكتور", "expected": "لا أكسدة"},
        ],
        []
    ),
    'P0340': (
        [
            {"step": 1, "title": "افحص سينسور CMP", "action": "قس الإشارة على scope", "expected": "نبضات منتظمة"},
            {"step": 2, "title": "افحص توقيت التايمنج", "action": "تأكد من علامات التايمنج", "expected": "العلامات متطابقة"},
        ],
        []
    ),

    # ═══════════════════════════════════════════════════════════════════
    # 🔥 ترموستات / تبريد
    # ═══════════════════════════════════════════════════════════════════
    'P0128': (
        [
            {"step": 1, "title": "بدّل الترموستات", "action": "السبب 90% ترموستات عالق مفتوح", "expected": "الترموستات الجديد يقفل لحد 87-92°"},
            {"step": 2, "title": "افحص ECT sensor", "action": "قارن قراءة ECT مع مقياس infrared", "expected": "نفس القراءة (±5°)"},
        ],
        []
    ),

    # ═══════════════════════════════════════════════════════════════════
    # 🔥 ABS / Chassis
    # ═══════════════════════════════════════════════════════════════════
    'C0030': (
        [
            {"step": 1, "title": "ارفع العجلة وقس مقاومة WSS", "action": "افصل الكنكتور وقس مع تدوير العجلة بالإيد", "expected": "مقاومة ضمن المواصفات + إشارة AC متغيرة"},
            {"step": 2, "title": "افحص الـ tone ring", "action": "تأكد من نظافته من البرادة المغناطيسية", "expected": "ring نضيف"},
            {"step": 3, "title": "افحص الكنكتور", "action": "تأكد من عدم وجود ماء أو أكسدة", "expected": "كنكتور نضيف وجاف"},
        ],
        []
    ),
    'C0036': (
        [{"step": 1, "title": "نفس فحص C0030 على العجلة الأمامية اليمين", "action": "كرر البروتوكول", "expected": "تحديد السبب"}],
        []
    ),
    'C0040': (
        [{"step": 1, "title": "نفس فحص C0030 على العجلة الخلفية اليسار", "action": "كرر البروتوكول", "expected": "تحديد السبب"}],
        []
    ),
    'C0045': (
        [{"step": 1, "title": "نفس فحص C0030 على العجلة الخلفية اليمين", "action": "كرر البروتوكول", "expected": "تحديد السبب"}],
        []
    ),

    # ═══════════════════════════════════════════════════════════════════
    # 🔥 Network / U-Codes
    # ═══════════════════════════════════════════════════════════════════
    'U0100': (
        [
            {"step": 1, "title": "افحص جهد البطارية", "action": "قس جهد البطارية والإيرث للـ ECM", "expected": "12.4V+ وإيرث سليم"},
            {"step": 2, "title": "افحص فيوزات الـ ECM", "action": "افحص كل فيوز متعلق بـ PCM", "expected": "كل الفيوزات سليمة"},
            {"step": 3, "title": "افحص أسلاك CAN H/L", "action": "قس مقاومة CAN بين الأطراف (~60Ω مع ECM موصل)", "expected": "60Ω تقريبًا"},
            {"step": 4, "title": "افحص ECM نفسه", "action": "حاول الاتصال مباشرة بالـ ECM", "expected": "لو مفيش رد = ECM تالف أو مفصول"},
        ],
        []
    ),
    'U0121': (
        [
            {"step": 1, "title": "افحص فيوز ABS", "action": "افحص فيوز وحدة ABS", "expected": "فيوز سليم"},
            {"step": 2, "title": "افحص كنكتور ABS module", "action": "تأكد من نظافة الكنكتور تحت العربة", "expected": "كنكتور نضيف"},
            {"step": 3, "title": "افحص CAN bus", "action": "نفس فحص U0100", "expected": "اتصال CAN سليم"},
        ],
        []
    ),
    'U0146': (
        [
            {"step": 1, "title": "هذا عطل خطير — Gateway", "action": "افحص الكنكتور الرئيسي للـ Central Gateway", "expected": "كل الـ pins داخلة وثابتة"},
            {"step": 2, "title": "افحص جهد إمداد Gateway", "action": "قس جهد الـ power وإيرث للوحدة", "expected": "12V ثابت"},
            {"step": 3, "title": "ابحث عن programming حديث", "action": "Gateway محتاج TSB أحدث؟", "expected": "آخر firmware"},
        ],
        []
    ),

    # ═══════════════════════════════════════════════════════════════════
    # 🔥 Manufacturer-specific شائعة
    # ═══════════════════════════════════════════════════════════════════
    'P1349': (
        [
            {"step": 1, "title": "تويوتا — افحص شبكة فلتر VVT", "action": "فك سولينويد VVT والشبكة وراه", "expected": "نضّفها أو بدّلها"},
            {"step": 2, "title": "بدّل زيت المحرك", "action": "زيت قديم سبب رئيسي", "expected": "زيت جديد + فلتر"},
            {"step": 3, "title": "افحص سولينويد VVT", "action": "قس مقاومته", "expected": "~7-7.5Ω"},
        ],
        []
    ),
    'P1326': (
        [
            {"step": 1, "title": "⚠️ هيونداي/كيا Theta II", "action": "هذا تحذير bearing knock — اعمل oil pan inspection فورًا", "expected": "بحث عن برادة معدنية"},
            {"step": 2, "title": "افحص bearings", "action": "لو فيه برادة = bearing تالف، المحرك محتاج تبديل/إصلاح كبير", "expected": "تقييم حالة المحرك"},
            {"step": 3, "title": "تأكد من تغطية الـ recall", "action": "كثير من موديلات هيونداي/كيا لها recall على Theta II", "expected": "تحقق من VIN لدى الوكيل"},
        ],
        []
    ),
    'P1778': (
        [
            {"step": 1, "title": "⚠️ نيسان CVT Step Motor", "action": "هذا عطل خطير في CVT", "expected": "تشخيص يتطلب فك CVT"},
            {"step": 2, "title": "افحص مستوى/لون زيت CVT", "action": "لازم يكون NS-3 أحمر/زهري", "expected": "لو محروق أو أسود = CVT تالف"},
            {"step": 3, "title": "افحص Valve Body", "action": "step motor جزء من valve body", "expected": "تقييم لتغيير valve body أو CVT كامل"},
        ],
        []
    ),
    'P10DF': (
        [
            {"step": 1, "title": "⚠️ BMW N20/N26 Timing Chain", "action": "هذا تحذير مهم — جنزير اتمد", "expected": "تأكد من المسافة المقطوعة"},
            {"step": 2, "title": "افحص بصريًا الـ chain guides", "action": "افحص chain tensioner والـ guides", "expected": "guides سليمة"},
            {"step": 3, "title": "خطط لتغيير الـ timing chain kit", "action": "kit كامل (chain + guides + tensioner + sprockets)", "expected": "تجنّب فشل كامل للمحرك"},
        ],
        []
    ),
    'P10E7': (
        [
            {"step": 1, "title": "BMW — افحص سينسور Valvetronic", "action": "افحص الكنكتور والأسلاك", "expected": "إشارة سليمة"},
            {"step": 2, "title": "افحص موتور Valvetronic", "action": "قس المقاومة", "expected": "ضمن المواصفات"},
            {"step": 3, "title": "أعد ضبط Valvetronic", "action": "بـ ISTA اعمل Valvetronic reset/adaptation", "expected": "نجح التعليم"},
        ],
        []
    ),

    # ═══════════════════════════════════════════════════════════════════
    # 🔥 Body / Airbag
    # ═══════════════════════════════════════════════════════════════════
    'B1681': (
        [
            {"step": 1, "title": "افحص بطارية المفتاح", "action": "بدّل البطارية أول حاجة", "expected": "مفتاح يستجيب"},
            {"step": 2, "title": "افحص هوائي القارئ", "action": "حول الـ ignition switch", "expected": "هوائي يصل إشارته"},
            {"step": 3, "title": "اعمل Key Programming", "action": "بـ scan tool متخصص علّم مفتاح جديد", "expected": "السيارة تشتغل"},
        ],
        []
    ),
    'B0012': (
        [
            {"step": 1, "title": "⚠️ افصل بطارية + استنى 5 دقايق", "action": "أمان أولًا قبل أي شغل في إيرباج", "expected": "احتياطي الـ capacitor فرغ"},
            {"step": 2, "title": "افحص clock spring", "action": "في عمود الاستيرنج", "expected": "clock spring سليم"},
            {"step": 3, "title": "افحص كنكتور الإيرباج", "action": "الكنكتور تحت الإستيرنج", "expected": "كنكتور داخل بإحكام"},
        ],
        []
    ),

    # ═══════════════════════════════════════════════════════════════════
    # 🔥 TPMS
    # ═══════════════════════════════════════════════════════════════════
    'C0750': (
        [
            {"step": 1, "title": "اقرأ ID كل سينسور", "action": "بـ TPMS tool", "expected": "تحديد السينسور الميت"},
            {"step": 2, "title": "افحص بطارية سينسور", "action": "عمر السينسور 5-7 سنين", "expected": "لو ميت = استبدل"},
            {"step": 3, "title": "اعمل TPMS relearn", "action": "بعد التركيب علّم الـ IDs الجدد", "expected": "كل الـ 4 سنسورات معروفة"},
        ],
        []
    ),
}


def populate(apps, schema_editor):
    DTC = apps.get_model('diagnostics_catalog', 'DTCDefinition')
    updated = 0
    for code, (steps, parts) in GUIDANCE.items():
        try:
            obj = DTC.objects.get(code=code)
        except DTC.DoesNotExist:
            continue
        changed = False
        if not obj.guided_steps:
            obj.guided_steps = steps
            changed = True
        if parts and not obj.likely_oem_parts:
            obj.likely_oem_parts = parts
            changed = True
        if changed:
            obj.save(update_fields=['guided_steps', 'likely_oem_parts', 'updated_at'])
            updated += 1
    print(f"  → populated guidance for {updated} DTC codes")


def depopulate(apps, schema_editor):
    DTC = apps.get_model('diagnostics_catalog', 'DTCDefinition')
    DTC.objects.filter(code__in=GUIDANCE.keys()).update(guided_steps=[], likely_oem_parts=[])


class Migration(migrations.Migration):
    dependencies = [
        ('diagnostics_catalog', '0004_manufacturer_and_chassis_body_codes'),
    ]
    operations = [
        migrations.RunPython(populate, depopulate),
    ]
