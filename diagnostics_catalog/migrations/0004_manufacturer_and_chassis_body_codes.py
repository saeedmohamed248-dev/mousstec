"""
📚 DTC Catalog Expansion II — 2026-06-12

يضيف ~180 كود جديد بتركيز على:
1. Manufacturer-specific للماركات الشائعة في السوق المصري
   (Toyota, Hyundai, Kia, Nissan, Chevrolet, Hyundai, MG, Chery, BMW, Mercedes)
2. تكملة B-codes (Body): HVAC, نوافذ, مقاعد, إضاءة, immobilizer, BCM
3. تكملة C-codes (Chassis): EPB, TPMS تفصيلي, ABS تفصيلي, ESP, suspension
4. تكملة U-codes (Network): lost-comm للـ ECUs الحديثة (Gateway, ADAS, Telematics)

is_generic=False للأكواد الـ manufacturer-specific. آمن للتشغيل عدة مرات
(update_or_create).
"""
from django.db import migrations


# (code, system, severity, short_ar, full_ar, is_generic)
CODES = [
    # ═══════════════════════════════════════════════════════════════════
    # 🚗 TOYOTA / LEXUS — أعطال شائعة جدًا في السوق المصري
    # ═══════════════════════════════════════════════════════════════════
    ('P1133', 'P', 'medium', 'تويوتا — استجابة O2 سينسور البنك 1',
     'Air-fuel ratio sensor (B1S1) استجابته بطيئة. شائع في كورولا/كامري/يارس بعد 80,000 كم.', False),
    ('P1135', 'P', 'medium', 'تويوتا — دائرة سخان A/F سينسور البنك 1',
     'سخان سينسور نسبة الهواء/الوقود تالف. شائع جدًا في كورولا و RAV4.', False),
    ('P1155', 'P', 'medium', 'تويوتا — دائرة سخان A/F سينسور البنك 2',
     'سخان سينسور نسبة الهواء/الوقود على البنك 2.', False),
    ('P1300', 'P', 'high',   'تويوتا — خلل دائرة الإشعال (Igniter)',
     'Igniter circuit malfunction. كويل إشعال تالفة — شائع في كامري/كورولا.', False),
    ('P1346', 'P', 'medium', 'تويوتا — مستشعر VVT — البنك 1',
     'VVT position sensor مشكلة. يسبب رفرفة في تشغيل المحرك.', False),
    ('P1349', 'P', 'high',   'تويوتا — نظام VVT لا يعمل',
     'VVT system malfunction. غالبًا فلتر شبكة VVT مسدود أو سولينويد VVT.', False),
    ('P1604', 'P', 'medium', 'تويوتا — مشكلة في بدء التشغيل',
     'Startability malfunction. تأخر في الدوران أو misfire وقت التشغيل البارد.', False),
    ('P1605', 'P', 'medium', 'تويوتا — Knock Control',
     'وحدة التحكم في الـ knock تعمل بشكل غير صحيح.', False),
    ('P1656', 'P', 'medium', 'تويوتا — OCV سولينويد VVT',
     'سولينويد التحكم في زيت VVT (Oil Control Valve) تالف.', False),
    ('P1780', 'P', 'low',    'تويوتا — موقع ذراع جير الـ Park/Neutral',
     'مستشعر موقع ذراع الجير في الـ Park/Neutral.', False),

    # ═══════════════════════════════════════════════════════════════════
    # 🚗 HYUNDAI / KIA — شائعة جدًا (إلنترا، أكسنت، توسان، سبورتاج)
    # ═══════════════════════════════════════════════════════════════════
    ('P1009', 'P', 'medium', 'هيونداي/كيا — حالة VVT متقدمة (بنك 1 Intake)',
     'D-CVVT actuator على كامة الدخول لا يصل للزاوية المطلوبة.', False),
    ('P1115', 'P', 'medium', 'هيونداي/كيا — قراءات حرارة الماء غير منطقية',
     'تعارض بين قراءات ECT والحرارة المتوقعة. شائع في إلنترا/أكسنت.', False),
    ('P1166', 'P', 'medium', 'هيونداي/كيا — Adaptive Test Failed سينسور O2',
     'سينسور الأكسجين فشل في اختبار التكيف. غالبًا السينسور نفسه تالف.', False),
    ('P1186', 'P', 'medium', 'هيونداي/كيا — جهد سينسور MAP خارج المعدل',
     'مستشعر MAP يعطي قراءات غير منطقية.', False),
    ('P1295', 'P', 'high',   'هيونداي/كيا — TPS (Electronic Throttle)',
     'مشكلة بدائرة Electronic Throttle Pedal Position. شائع في توسان.', False),
    ('P1326', 'P', 'critical','هيونداي/كيا — Knock Sensor Detection (Theta II)',
     'محرك Theta II — اكتشاف اهتزاز غير طبيعي. علامة على bearing تالف. شائع في سوناتا/سانتافي.', False),
    ('P1505', 'P', 'medium', 'هيونداي/كيا — Idle Speed Actuator',
     'سولينويد التحكم بسرعة الـ idle خارج النطاق.', False),
    ('P1586', 'P', 'medium', 'هيونداي/كيا — التحكم في فالف EGR',
     'EGR valve control system. شائع في أكسنت/إلنترا الديزل.', False),
    ('P1693', 'P', 'low',    'هيونداي/كيا — لمبة Check Engine معطلة',
     'دائرة MIL (Malfunction Indicator Lamp) لا تعمل.', False),

    # ═══════════════════════════════════════════════════════════════════
    # 🚗 NISSAN / INFINITI — صني، تيدا، سنترا، التيما
    # ═══════════════════════════════════════════════════════════════════
    ('P1031', 'P', 'medium', 'نيسان — سخان سينسور A/F بنك 1',
     'سخان مستشعر نسبة الهواء/الوقود — جهد منخفض.', False),
    ('P1065', 'P', 'medium', 'نيسان — جهد إمداد ECM',
     'جهد البطارية الواصل للـ ECM خارج النطاق. افحص الإيرث والكابلات.', False),
    ('P1126', 'P', 'medium', 'نيسان — ترموستات لا يقفل',
     'مشكلة ترموستات — لا يصل المحرك لحرارة التشغيل. شائع في صني/سنترا.', False),
    ('P1148', 'P', 'medium', 'نيسان — اختبار Closed Loop فشل بنك 1',
     'النظام لا يدخل closed loop. غالبًا A/F sensor.', False),
    ('P1212', 'P', 'medium', 'نيسان — اتصال TCS بين الـ ECMs',
     'مشكلة اتصال بين ABS/TCS وECM.', False),
    ('P1273', 'P', 'medium', 'نيسان — A/F سينسور دائرة منخفضة بنك 1',
     'دائرة مستشعر A/F منخفضة.', False),
    ('P1564', 'P', 'medium', 'نيسان — ASCD/Cruise Control',
     'دائرة كروز كنترول.', False),
    ('P1715', 'P', 'high',   'نيسان — مستشعر السرعة الداخلي للجير',
     'مستشعر السرعة الداخلي بـ CVT/AT. شائع في صني/التيما CVT.', False),
    ('P1778', 'P', 'high',   'نيسان CVT — Step Motor',
     'الموتور التدريجي بتاع CVT — علامة على CVT محتاج إصلاح. شائع في التيما.', False),

    # ═══════════════════════════════════════════════════════════════════
    # 🚗 CHEVROLET / GM — أوبترا، أفيو، كروز، كابتيفا، كروزر، تاهو
    # ═══════════════════════════════════════════════════════════════════
    ('P1336', 'P', 'medium', 'شيفروليه — تعليم CKP غير مكتمل',
     'يحتاج إجراء CKP relearn (variation learn) بعد تغيير الكرنك سينسور.', False),
    ('P1351', 'P', 'medium', 'شيفروليه — IC (Ignition Control) دائرة عالية',
     'دائرة التحكم بالإشعال جهد عالي.', False),
    ('P1380', 'P', 'medium', 'شيفروليه — DTC اكتشاف الطرق الوعرة فعّال',
     'DTC رفض اكتشاف misfire بسبب الطرق الوعرة.', False),
    ('P1682', 'P', 'medium', 'شيفروليه — Ignition Switch Run/Crank Voltage',
     'جهد سويتش الإشعال على Run منخفض. شائع في كروز/إيكينوكس.', False),

    # ═══════════════════════════════════════════════════════════════════
    # 🚗 MG / CHERY — صاعدة بقوة في السوق المصري
    # ═══════════════════════════════════════════════════════════════════
    ('P1497', 'P', 'medium', 'شيري/MG — مستشعر ضغط البوست',
     'سينسور ضغط الترتشارج. شائع في MG و Tiggo بعد 60,000 كم.', False),
    ('P1631', 'P', 'medium', 'شيري — Smart Alternator دائرة',
     'مولد ذكي — مشكلة في الاتصال.', False),

    # ═══════════════════════════════════════════════════════════════════
    # 🚗 BMW — متشار في الأعطال على F-Series و E-Series
    # ═══════════════════════════════════════════════════════════════════
    ('P10DF', 'P', 'medium', 'BMW — N20/N26 Timing Chain Stretch',
     'جنزير التايمنج اتمدد — مشكلة شهيرة جدًا في N20/N26 (F30, F10). علامة مبكرة.', False),
    ('P1056', 'P', 'medium', 'BMW — Vanos Solenoid Intake',
     'سولينويد Vanos Intake. شائع في N52/N54/N55.', False),
    ('P1058', 'P', 'medium', 'BMW — Vanos Solenoid Exhaust',
     'سولينويد Vanos Exhaust.', False),
    ('P10E7', 'P', 'high',   'BMW — Valvetronic Eccentric Shaft Sensor',
     'سينسور عمود Valvetronic. السيارة تدخل limp. شائع جدًا في N20/B48.', False),
    ('P1525', 'P', 'medium', 'BMW — Camshaft Position Actuator',
     'محرك ضبط زاوية الكامة.', False),
    ('P1776', 'P', 'high',   'BMW — ZF 8HP Mechatronic',
     'وحدة الـ Mechatronic بتاعة جير ZF 8HP — تسريب أو فشل sealing.', False),
    ('P2BAC', 'P', 'high',   'BMW — NOx Mass Above Threshold',
     'كمية NOx فوق الحد. شائع في موديلات الديزل الأوروبية.', False),

    # ═══════════════════════════════════════════════════════════════════
    # 🚗 MERCEDES — موديلات متنوعة
    # ═══════════════════════════════════════════════════════════════════
    ('P0420', 'P', 'medium', 'مرسيدس — كفاءة الكتلايزر بنك 1 تحت العتبة',
     'الكتلايزر فقد كفاءته. شائع في M271/M272.', False),
    ('P0480', 'P', 'low',    'مرسيدس — مروحة التبريد 1',
     'مروحة الرادياتير الكهربا.', False),
    ('P200A', 'P', 'medium', 'مرسيدس — Intake Manifold Runner Position Sensor',
     'سينسور موقع لوحات مانيفولد الدخول. شائع في M276.', False),
    ('P2563', 'P', 'high',   'مرسيدس — Turbo Boost Control Position',
     'مشكلة في actuator التيربو الكهربا.', False),

    # ═══════════════════════════════════════════════════════════════════
    # 🅱️ BODY CODES (B) — توسعة كبيرة
    # ═══════════════════════════════════════════════════════════════════
    # — Airbag / SRS تفصيلي
    ('B0012', 'B', 'high',     'Airbag Driver Stage 1 — دائرة منخفضة',
     'دائرة المرحلة الأولى لإيرباج السائق منخفضة.', True),
    ('B0013', 'B', 'high',     'Airbag Driver Stage 1 — دائرة عالية',
     'دائرة المرحلة الأولى لإيرباج السائق عالية.', True),
    ('B0014', 'B', 'high',     'Airbag Driver Stage 1 — قصر للأرضي',
     'قصر دائرة إيرباج السائق إلى الأرضي.', True),
    ('B0015', 'B', 'high',     'Airbag Driver Stage 2 — دائرة مفتوحة',
     'دائرة المرحلة الثانية لإيرباج السائق مفتوحة.', True),
    ('B0021', 'B', 'high',     'Airbag Passenger Stage 1 — دائرة منخفضة',
     'دائرة المرحلة الأولى لإيرباج الراكب منخفضة.', True),
    ('B0024', 'B', 'high',     'Airbag Passenger Stage 1 — قصر للأرضي',
     'قصر إيرباج الراكب.', True),
    ('B0051', 'B', 'high',     'Deployment Commanded — Driver Front',
     'تم تفعيل إيرباج السائق الأمامي.', True),
    ('B0052', 'B', 'high',     'Deployment Commanded — Passenger Front',
     'تم تفعيل إيرباج الراكب الأمامي.', True),
    ('B0053', 'B', 'high',     'Deployment Commanded — Side',
     'تم تفعيل إيرباج جانبي.', True),
    ('B0080', 'B', 'high',     'Side Impact Sensor — يسار',
     'مستشعر التصادم الجانبي الأيسر تالف.', True),
    ('B0081', 'B', 'high',     'Side Impact Sensor — يمين',
     'مستشعر التصادم الجانبي الأيمن.', True),
    ('B0092', 'B', 'high',     'Seat Position Sensor — السائق',
     'سينسور موقع كرسي السائق (يحدد قوة تفجير الإيرباج).', True),
    ('B0095', 'B', 'high',     'Seat Occupancy Sensor — الراكب',
     'حساس وجود راكب في الكرسي الأمامي (Passenger Presence).', True),
    ('B0100', 'B', 'high',     'Seatbelt Pretensioner — السائق',
     'شداد حزام أمان السائق دائرة معطلة.', True),
    ('B0101', 'B', 'high',     'Seatbelt Pretensioner — الراكب',
     'شداد حزام أمان الراكب.', True),

    # — HVAC تفصيلي
    ('B1320', 'B', 'low',      'HVAC — سينسور حرارة داخل الكابينة',
     'مستشعر حرارة الهواء الداخلي للكابينة.', True),
    ('B1325', 'B', 'low',      'HVAC — سينسور حرارة الهواء الخارجي',
     'مستشعر حرارة الجو الخارجي.', True),
    ('B1342', 'B', 'low',      'HVAC — Blend Door Actuator (السائق)',
     'موتور توجيه الهواء الساخن/البارد على جهة السائق.', True),
    ('B1343', 'B', 'low',      'HVAC — Blend Door Actuator (الراكب)',
     'موتور توجيه الهواء على جهة الراكب.', True),
    ('B1345', 'B', 'low',      'HVAC — Mode Door Actuator',
     'موتور توجيه الهواء (وش/أرجل/ديفروست).', True),
    ('B1347', 'B', 'low',      'HVAC — Recirculation Door Actuator',
     'موتور تدوير الهواء الداخلي/الخارجي.', True),
    ('B1356', 'B', 'medium',   'HVAC — سينسور ضغط مكيف عالي',
     'سينسور الضغط العالي للفريون.', True),
    ('B1357', 'B', 'medium',   'HVAC — سينسور ضغط مكيف منخفض',
     'سينسور الضغط المنخفض للفريون.', True),
    ('B1370', 'B', 'low',      'HVAC — Blower Motor Speed Control',
     'مقاومة سرعة مروحة المكيف.', True),

    # — نوافذ كهربا، أقفال، مرايات
    ('B1400', 'B', 'low',      'Power Window — السائق Up/Down switch',
     'دائرة سويتش نافذة السائق.', True),
    ('B1410', 'B', 'low',      'Power Window — الراكب Up/Down',
     'دائرة سويتش نافذة الراكب.', True),
    ('B1430', 'B', 'low',      'Power Lock — جميع الأبواب',
     'دائرة قفل مركزي.', True),
    ('B1450', 'B', 'low',      'Mirror Adjust — السائق',
     'موتور ضبط مرآة السائق.', True),
    ('B1460', 'B', 'low',      'Mirror Heater — دائرة',
     'سخان المرآة الجانبية.', True),

    # — Immobilizer / Keys
    ('B1682', 'B', 'high',     'Immobilizer — Transponder communication',
     'فشل الاتصال مع شريحة المفتاح.', True),
    ('B1683', 'B', 'high',     'Immobilizer — Antenna circuit',
     'هوائي قارئ المفتاح حول السويتش معطل.', True),
    ('B1701', 'B', 'medium',   'BCM — Battery Voltage Low',
     'وحدة BCM تتلقى جهد منخفض.', True),
    ('B1705', 'B', 'medium',   'BCM — Ignition Run/Start',
     'مشكلة في إشارة الـ Ignition Run.', True),

    # — Lighting
    ('B2200', 'B', 'low',      'إضاءة — Headlamp Low Beam اليسار',
     'لمبة الكشاف الأمامي الأيسر منخفضة.', True),
    ('B2201', 'B', 'low',      'إضاءة — Headlamp Low Beam اليمين',
     'لمبة الكشاف الأمامي الأيمن.', True),
    ('B2202', 'B', 'low',      'إضاءة — High Beam اليسار',
     'لمبة الـ High Beam الأيسر.', True),
    ('B2204', 'B', 'low',      'Tail Lamp — اليسار',
     'لمبة خلفية يسار.', True),
    ('B2205', 'B', 'low',      'Tail Lamp — اليمين',
     'لمبة خلفية يمين.', True),
    ('B2210', 'B', 'low',      'إضاءة — DRL Circuit',
     'دائرة Daytime Running Lights.', True),
    ('B2477', 'B', 'low',      'Module — Configuration mismatch',
     'وحدة تحكم تحتاج programming/تكوين صحيح.', True),

    # ═══════════════════════════════════════════════════════════════════
    # 🅲 CHASSIS CODES (C) — توسعة كبيرة
    # ═══════════════════════════════════════════════════════════════════
    # — ABS / Wheel Speed تفصيلي
    ('C0030', 'C', 'high',     'ABS — سينسور سرعة الإطار الأمامي اليسار',
     'مستشعر سرعة العجلة الأمامية اليسار. علامة تشغيل لمبة ABS.', True),
    ('C0031', 'C', 'high',     'ABS — دائرة WSS أمامي يسار مفتوحة',
     'دائرة مستشعر العجلة أمامي يسار مفتوحة.', True),
    ('C0036', 'C', 'high',     'ABS — WSS أمامي يمين',
     'سينسور سرعة العجلة الأمامية اليمين.', True),
    ('C0040', 'C', 'high',     'ABS — WSS خلفي يسار',
     'سينسور سرعة العجلة الخلفية اليسار.', True),
    ('C0045', 'C', 'high',     'ABS — WSS خلفي يمين',
     'سينسور سرعة العجلة الخلفية اليمين.', True),
    ('C0050', 'C', 'high',     'ABS — Tone Wheel غير صحيح',
     'دائرة المسننات بتاعة سرعة الإطار تالفة (حلقة الـ ABS reluctor).', True),
    ('C0110', 'C', 'high',     'ABS — Pump Motor Circuit',
     'دائرة موتور طلمبة الـ ABS.', True),
    ('C0121', 'C', 'high',     'ABS — Valve Relay Circuit',
     'دائرة ريلاي صمامات ABS.', True),
    ('C0196', 'C', 'medium',   'Yaw Rate Sensor',
     'مستشعر معدل الانعراج (ESP). يؤثر على ESP/VSC.', True),
    ('C0710', 'C', 'medium',   'Steering Position Signal',
     'إشارة مستشعر زاوية الاستيرنج. يحتاج SAS calibration.', True),
    ('C0800', 'C', 'medium',   'Control Module Power Circuit',
     'دائرة طاقة وحدة ABS.', True),

    # — EPB / Hand brake كهربا
    ('C0900', 'C', 'high',     'EPB — Left Caliper Motor',
     'موتور كاليبر فرامل اليد الكهربا الأيسر.', True),
    ('C0901', 'C', 'high',     'EPB — Right Caliper Motor',
     'موتور كاليبر فرامل اليد الأيمن.', True),
    ('C0902', 'C', 'medium',   'EPB — Switch Circuit',
     'دائرة سويتش EPB.', True),
    ('C0903', 'C', 'high',     'EPB — Module Communication Lost',
     'فقدان اتصال مع وحدة EPB.', True),

    # — TPMS تفصيلي
    ('C0750', 'C', 'medium',   'TPMS — Front Left Sensor',
     'مستشعر ضغط الإطار الأمامي اليسار — بطارية ضعيفة أو حساس تالف.', True),
    ('C0755', 'C', 'medium',   'TPMS — Front Right Sensor',
     'مستشعر ضغط الإطار الأمامي اليمين.', True),
    ('C0760', 'C', 'medium',   'TPMS — Rear Left Sensor',
     'مستشعر ضغط الإطار الخلفي اليسار.', True),
    ('C0765', 'C', 'medium',   'TPMS — Rear Right Sensor',
     'مستشعر ضغط الإطار الخلفي اليمين.', True),
    ('C0770', 'C', 'low',      'TPMS — Spare Tire Sensor',
     'مستشعر ضغط الإطار الاحتياطي.', True),
    ('C0775', 'C', 'medium',   'TPMS — Sensor IDs غير معلَّمة',
     'يحتاج TPMS relearn بعد تغيير حساسات أو إطارات.', True),

    # — Suspension / Steering
    ('C1100', 'C', 'medium',   'Electric Power Steering — Motor Circuit',
     'دائرة موتور EPS. لمبة الاستيرنج تشتغل.', True),
    ('C1101', 'C', 'medium',   'EPS — Torque Sensor',
     'مستشعر العزم في عمود الاستيرنج.', True),
    ('C1102', 'C', 'medium',   'EPS — Calibration Required',
     'يحتاج EPS calibration (بعد ميزانية أو تغيير سينسور).', True),
    ('C1500', 'C', 'medium',   'Adaptive Suspension — Front Left Damper',
     'مساعد التعليق التكيفي الأمامي يسار.', True),
    ('C1501', 'C', 'medium',   'Adaptive Suspension — Front Right Damper',
     'مساعد التعليق التكيفي الأمامي يمين.', True),
    ('C1510', 'C', 'medium',   'Ride Height Sensor — Front',
     'سينسور ارتفاع السيارة الأمامي.', True),

    # — 4WD / AWD
    ('C1800', 'C', 'medium',   '4WD — Transfer Case Actuator',
     'محرك تفعيل الـ Transfer Case (4WD).', True),
    ('C1801', 'C', 'medium',   '4WD — Mode Selector Switch',
     'سويتش اختيار وضع الدفع 4WD.', True),
    ('C1802', 'C', 'medium',   'AWD — Coupling Solenoid',
     'سولينويد وصلة الـ AWD (مثل Haldex).', True),

    # ═══════════════════════════════════════════════════════════════════
    # 🆄 NETWORK CODES (U) — توسعة مهمة جدًا
    # ═══════════════════════════════════════════════════════════════════
    ('U0010', 'U', 'high',     'Medium Speed CAN Bus — Off',
     'باص CAN متوسط السرعة متوقف. مشكلة عامة في شبكة CAN.', True),
    ('U0011', 'U', 'high',     'Medium Speed CAN Bus — Bus Communication',
     'فشل اتصال على Medium Speed CAN.', True),
    ('U0028', 'U', 'high',     'Vehicle Comm Bus A — Performance',
     'أداء غير مرضي على شبكة الاتصالات A.', True),
    ('U0100', 'U', 'critical', 'فقدان اتصال مع ECM/PCM',
     'الـ Gateway مش شايف ECM. مشكلة Powertrain CAN خطيرة.', True),
    ('U0101', 'U', 'high',     'فقدان اتصال مع TCM',
     'فقدان اتصال مع وحدة الجير.', True),
    ('U0121', 'U', 'high',     'فقدان اتصال مع وحدة ABS',
     'لمبة ABS + ESP + EPB ممكن تشتغل كلهم.', True),
    ('U0140', 'U', 'high',     'فقدان اتصال مع BCM',
     'فقدان اتصال مع Body Control Module.', True),
    ('U0146', 'U', 'high',     'فقدان اتصال مع Gateway',
     'مشكلة في الـ Central Gateway. خطر — معظم الشبكة هتقع.', True),
    ('U0155', 'U', 'high',     'فقدان اتصال مع Instrument Cluster',
     'فقدان اتصال مع طبلون السيارة.', True),
    ('U0159', 'U', 'medium',   'فقدان اتصال مع Parking Assist',
     'حساسات الركن مش شغالة.', True),
    ('U0212', 'U', 'medium',   'فقدان اتصال مع Steering Column Module',
     'فقدان اتصال مع SCM (multifunction switches).', True),
    ('U0214', 'U', 'medium',   'فقدان اتصال مع Remote Function Module',
     'فقدان اتصال مع وحدة الـ remote/keyless.', True),
    ('U0235', 'U', 'medium',   'فقدان اتصال مع Cruise Control Module',
     'فقدان اتصال مع وحدة الكروز كنترول.', True),
    ('U0293', 'U', 'high',     'فقدان اتصال مع HEV ECU',
     'وحدة هايبرد منقطعة.', True),
    ('U029D', 'U', 'medium',   'فقدان اتصال مع NOx Sensor B',
     'سينسور NOx الخلفي (للديزل/SCR).', True),
    ('U0315', 'U', 'high',     'فقدان اتصال مع ABS Control Module',
     'تكرار U0121.', True),
    ('U0422', 'U', 'medium',   'بيانات غير صالحة من BCM',
     'BCM يرسل قيم خارج المعدل.', True),
    ('U0428', 'U', 'medium',   'بيانات غير صالحة من Steering Angle',
     'يحتاج SAS calibration.', True),
    ('U0452', 'U', 'medium',   'بيانات غير صالحة من Trailer Brake',
     'بيانات فرامل المقطورة غير صالحة.', True),

    # — ADAS / حديثة
    ('U0529', 'U', 'medium',   'فقدان اتصال مع Forward Camera',
     'كاميرا أمامية لـ Lane Keep / AEB.', True),
    ('U0530', 'U', 'medium',   'فقدان اتصال مع Forward Radar',
     'رادار أمامي لـ Adaptive Cruise / AEB.', True),
    ('U0532', 'U', 'medium',   'فقدان اتصال مع Side Object Detection',
     'وحدة Blind Spot Monitor.', True),
    ('U0593', 'U', 'medium',   'بيانات غير صالحة من Rain Sensor',
     'مستشعر المطر للمساحات الأوتوماتيك.', True),
    ('U0625', 'U', 'medium',   'فقدان اتصال مع Telematics Module',
     'وحدة الـ Telematics/eCall.', True),
    ('U1000', 'U', 'medium',   'CAN Communication Bus — manufacturer specific',
     'خلل اتصال CAN مخصص للشركة المصنعة.', True),
    ('U1064', 'U', 'medium',   'LAN Communication Bus Error',
     'خلل اتصال LAN.', True),
]


def seed(apps, schema_editor):
    DTC = apps.get_model('diagnostics_catalog', 'DTCDefinition')
    for code, system, severity, short, full, is_generic in CODES:
        DTC.objects.update_or_create(
            code=code,
            defaults={
                'system': system,
                'severity': severity,
                'short_description': short,
                'full_description': full,
                'source': 'community' if is_generic else 'manual',
                'is_generic': is_generic,
            },
        )


def unseed(apps, schema_editor):
    DTC = apps.get_model('diagnostics_catalog', 'DTCDefinition')
    DTC.objects.filter(code__in=[c[0] for c in CODES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('diagnostics_catalog', '0003_expand_dtc_catalog_2026'),
    ]
    operations = [
        migrations.RunPython(seed, unseed),
    ]
