"""
🚗 Diagnostic Brand Catalog — Single source of truth
======================================================
كل ماركة سيارة فيها:
  • engines      — قائمة أكواد المحركات (تظهر كـ chips في الـ sidebar)
  • shop_faqs    — أسئلة شائعة للفني (تظهر كأزرار اقتراحات)
  • customer_faqs — أسئلة شائعة لصاحب السيارة
  • expert_focus — النقطة اللي الـ AI prompt يركز عليها لهذه الماركة
  • aliases      — أسماء الموديلات اللي الـ refiner يتعرف عليها

لإضافة ماركة جديدة: ضيف entry هنا — الـ UI والـ AI prompt هيشوفوها تلقائياً.
"""
from __future__ import annotations

import re


DIAGNOSTIC_BRANDS: dict[str, dict] = {
    # ─────────────────────────────────────────────────────────────────
    'bmw': {
        'label': 'BMW / MINI',
        'emoji': '🇩🇪',
        'color': '#0099ff',
        'engines': [
            # M-Series (legacy)
            'M50', 'M52', 'M54', 'M56', 'M57',
            # N-Series (mid-2000s → 2018)
            'N13', 'N20', 'N26', 'N42', 'N43', 'N46', 'N47',
            'N52', 'N54', 'N55', 'N57', 'N62', 'N63', 'N73', 'N74',
            # B-Series (modular, 2014+)
            'B38', 'B47', 'B48', 'B57', 'B58',
            # S-Series (M-Performance)
            'S55', 'S58', 'S63', 'S65', 'S85',
        ],
        'expert_focus': (
            'BMW و MINI Cooper — معرفة هندسية صارمة لـ:\n'
            '• تخطيط الـ engine bay لكل محرك (Turbo position vs Intake direction)\n'
            '• Torque specifications (مثال: N54 turbo manifold = 22 Nm + 90°)\n'
            '• Common failure points (N54 HPFP, N20 timing chain, N13 carbon buildup, S55 rod bearings)\n'
            '• Diagnostic procedures عبر ISTA / INPA / Carly\n'
            '• فروق التصميم بين F-series و G-series و U-series'
        ),
        'aliases': [
            'E36', 'E39', 'E46', 'E60', 'E63', 'E70', 'E82', 'E83', 'E84', 'E87', 'E90', 'E91', 'E92', 'E93',
            'F10', 'F11', 'F15', 'F20', 'F21', 'F22', 'F25', 'F26', 'F30', 'F31', 'F32', 'F33', 'F34', 'F36',
            'F44', 'F45', 'F46', 'F48', 'F80', 'F82', 'F83', 'F85', 'F86', 'F87',
            'G01', 'G02', 'G05', 'G06', 'G07', 'G11', 'G12', 'G14', 'G15', 'G16',
            'G20', 'G21', 'G22', 'G23', 'G26', 'G29', 'G30', 'G31', 'G32',
            'R55', 'R56', 'R57', 'R58', 'R59', 'R60', 'R61',
            'F54', 'F55', 'F56', 'F57', 'F60',
            'MINI', 'Cooper',
        ],
        'shop_faqs': [
            {'badge': 'N20', 'label': 'P0301 ميس فاير',
             'q': 'P0301 على F30 N20 — العربية بترعش في الـ idle. إيه التشخيص المتسلسل وعزوم تربيط الـ coils؟'},
            {'badge': 'N54', 'label': 'HPFP failure',
             'q': 'N54 على E90 — صوت من الـ HPFP وإنذار Reduced Power. خطوات الفحص بالـ ISTA؟'},
            {'badge': 'N13', 'label': 'تخطيط Turbo/Intake',
             'q': 'N13 على F20 — تحديد مكان الـ Turbo بالضبط واتجاه الـ Intake وفرقها عن N20'},
            {'badge': 'N52', 'label': 'Valvetronic',
             'q': 'N52 على E60 — Valvetronic motor failure. أكواد العطل وعزم تربيط الـ Valvetronic motor؟'},
            {'badge': 'N20', 'label': 'Timing chain',
             'q': 'N20 timing chain rattle — خطوات التغيير وعزوم تربيط الـ guides والـ tensioner'},
            {'badge': 'S55', 'label': 'Rod bearings',
             'q': 'S55 على F80 M3 — متى يتم تغيير الـ rod bearings الوقائي وما هي العزوم؟'},
        ],
        'customer_faqs': [
            {'label': '🚙 رعشة في الـ idle',
             'q': 'عربيتي F30 موديل 2014 بتترعش لما باقف وأحياناً نور المحرك بيولع. إيه ممكن يكون السبب؟'},
            {'label': '🔊 صوت + ضعف عزم',
             'q': 'بسمع صوت غريب من تحت الكبوت وهي شغالة، وفيه فقدان في العزم'},
            {'label': '⚠️ Reduced Power',
             'q': 'ولّع نور Engine Reduced Power. أنا في الطريق — أقدر أكمل ولا لازم أوقف؟'},
            {'label': '🛢️ استهلاك زيت عالي',
             'q': 'استهلاك الزيت بقى عالي قوي، هل ده طبيعي لـ BMW N52؟'},
        ],
    },

    # ─────────────────────────────────────────────────────────────────
    'mercedes': {
        'label': 'Mercedes-Benz',
        'emoji': '⭐',
        'color': '#9ca3af',
        'engines': [
            # Petrol
            'M111', 'M112', 'M113', 'M156', 'M157', 'M159', 'M177', 'M178',
            'M256', 'M260', 'M264', 'M270', 'M271', 'M272', 'M273', 'M274',
            'M276', 'M278', 'M279', 'M282', 'M133', 'M152',
            # Diesel
            'OM611', 'OM612', 'OM613', 'OM642', 'OM651', 'OM654', 'OM656',
            'OM646', 'OM647', 'OM648',
        ],
        'expert_focus': (
            'Mercedes-Benz — معرفة هندسية صارمة لـ:\n'
            '• تخطيط محركات M271 / M272 / M274 / M276 / OM651\n'
            '• Torque specifications + procedures via XENTRY / DAS\n'
            '• Common failure points (M272/M273 balance shaft gear، M271 timing chain rail،\n'
            '  OM651 injector seals، M276 oil cooler leak، M157 turbo)\n'
            '• فروق W204/W205/W213 chassis والـ 722.6 / 722.9 / 9G-Tronic gearboxes\n'
            '• 4MATIC transfer case quirks'
        ),
        'aliases': [
            'W124', 'W202', 'W203', 'W204', 'W205', 'W206',
            'W210', 'W211', 'W212', 'W213', 'W214',
            'W140', 'W220', 'W221', 'W222', 'W223',
            'W163', 'W164', 'W166', 'W167', 'W251',
            'W463', 'W639', 'W447',
            'A-Class', 'C-Class', 'E-Class', 'S-Class', 'CLA', 'CLS', 'GLA', 'GLC', 'GLE', 'GLS', 'G-Class',
        ],
        'shop_faqs': [
            {'badge': 'M271', 'label': 'Timing chain rail',
             'q': 'M271 على W204 C200 — تكسر timing chain rail. الأعراض، عزوم التركيب، وأرقام OEM للـ kit'},
            {'badge': 'M272', 'label': 'Balance shaft',
             'q': 'M272 — كود P0016 / P0017، balance shaft gear sprocket wear. التشخيص والإصلاح بالـ XENTRY'},
            {'badge': 'OM651', 'label': 'Injector seals',
             'q': 'OM651 — تسريب من injector seals (ديزل). عزم الـ injector clamp وترتيب الفك'},
            {'badge': 'M276', 'label': 'Oil cooler leak',
             'q': 'M276 على W212 — تسريب زيت من الـ oil cooler/valley pan. خطوات الفك والـ gasket الصحيح'},
            {'badge': '722.9', 'label': '7G-Tronic conductor plate',
             'q': '722.9 على W211 — أعراض conductor plate failure والـ adaptation بعد التغيير'},
            {'badge': 'M157', 'label': 'Turbo failure',
             'q': 'M157 على W221 S63 AMG — turbo whistle ودخان أزرق. خطوات الفحص'},
        ],
        'customer_faqs': [
            {'label': '🚙 صوت طقطقة من المحرك',
             'q': 'عربيتي C200 W204 موديل 2010 بتطلع صوت طقطقة من المحرك في البدايه. خطر ولا عادي؟'},
            {'label': '⚠️ Visit Workshop',
             'q': 'ظهرت رسالة Visit Workshop على الـ dashboard. أقدر أكمل سواقة ولا أوقف؟'},
            {'label': '🛢️ تسريب زيت تحت العربية',
             'q': 'فيه قطرات زيت تحت العربية بعد ما توقف. مرسيدس E-Class — منين الزيت ممكن ينزل؟'},
            {'label': '🔋 بطارية ضعيفة',
             'q': 'الـ start/stop وقف يشتغل والشاشة بتقول الـ battery weak. لازم بطارية AGM؟'},
        ],
    },

    # ─────────────────────────────────────────────────────────────────
    'audi_vw': {
        'label': 'Audi / VW / Škoda',
        'emoji': '🅰️',
        'color': '#dc2626',
        'engines': [
            # EA888 family
            'EA888-Gen1', 'EA888-Gen2', 'EA888-Gen3', 'EA888-Gen4',
            # Other petrol
            'EA111', 'EA113', 'EA211', 'EA837', 'EA839',
            # TFSI / FSI codes
            'CDN', 'CCZ', 'CDA', 'CAV', 'CAX', 'CTH', 'CJS', 'CHH', 'DKT',
            # TDI diesel
            'EA189', 'EA288', 'EA897',
            'CRBC', 'CFFA', 'CFFB', 'CKRA', 'CMGB', 'CGLC',
            # W12 / V10
            'BAR', 'BHT', 'BTE',
        ],
        'expert_focus': (
            'Audi / VW / Škoda / SEAT — معرفة هندسية صارمة لـ:\n'
            '• EA888 family (Gen1/Gen2/Gen3/Gen4) — timing chain، PCV، carbon buildup\n'
            '• 2.0 TDI EA189 + EA288 (Dieselgate engines)\n'
            '• MQB / MLB / MEB platform quirks\n'
            '• DSG DQ200 / DQ250 / DQ381 / DQ500 mechatronics\n'
            '• Quattro driveline (Torsen + Haldex) faults\n'
            '• Diagnostic procedures via VCDS / ODIS / VAS'
        ),
        'aliases': [
            'B5', 'B6', 'B7', 'B8', 'B9',
            'C5', 'C6', 'C7', 'C8',
            'A1', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8',
            'Q3', 'Q5', 'Q7', 'Q8',
            'TT', 'R8', 'RS3', 'RS4', 'RS5', 'RS6', 'RS7',
            'Golf', 'Polo', 'Passat', 'Tiguan', 'Touareg', 'Jetta', 'Arteon', 'T-Roc', 'Atlas',
            'Octavia', 'Superb', 'Kodiaq', 'Karoq',
            'MK5', 'MK6', 'MK7', 'MK8',
        ],
        'shop_faqs': [
            {'badge': 'EA888', 'label': 'Carbon buildup intake',
             'q': 'EA888 Gen2 على Golf GTI MK6 — carbon buildup على الـ intake valves. ميديا الـ walnut blast والـ procedure'},
            {'badge': 'EA888', 'label': 'PCV diaphragm',
             'q': 'EA888 — صوت whistle من الـ PCV valve. تشخيص الـ vacuum leak ورقم القطعة الصحيح'},
            {'badge': 'EA189', 'label': 'EGR + DPF',
             'q': '2.0 TDI EA189 على Passat B7 — كود P2002 / P0401. تشخيص الـ DPF و EGR'},
            {'badge': 'DSG', 'label': 'DQ200 mechatronics',
             'q': 'DSG DQ200 — judder عند الإقلاع، رسالة "Gearbox Error". الـ adaptation أو mechatronics replacement؟'},
            {'badge': 'EA888', 'label': 'Timing chain stretch',
             'q': 'EA888 Gen2 — كود P0016. الـ timing chain stretch، الـ updated tensioner، وعزوم التركيب'},
        ],
        'customer_faqs': [
            {'label': '⚠️ Engine Malfunction',
             'q': 'عربيتي A4 B8 طلعت رسالة Engine Malfunction Reduced Power. ايه الموقف؟'},
            {'label': '🔄 DSG بيهز عند التحويل',
             'q': 'الـ DSG على Golf 2014 بيهز شوية عند التحويل من 1 لـ 2. مشكلة كبيرة؟'},
            {'label': '🛢️ استهلاك زيت 1L كل 1000 كم',
             'q': 'Audi A6 TFSI بياكل 1 لتر زيت كل 1000 كم. ده طبيعي ولا عيب؟'},
            {'label': '💨 دخان أبيض عند البدء',
             'q': 'فيه دخان أبيض من الشكمان أول ما أشغل العربية في الصبح، بيختفي بعد دقيقة'},
        ],
    },

    # ─────────────────────────────────────────────────────────────────
    'toyota': {
        'label': 'Toyota / Lexus',
        'emoji': '🇯🇵',
        'color': '#10b981',
        'engines': [
            # 4-cyl
            '1NZ-FE', '2NZ-FE', '1ZZ-FE', '2ZZ-GE', '3ZZ-FE',
            '1AZ-FE', '2AZ-FE', '2AZ-FXE',
            '1AR-FE', '2AR-FE', '2AR-FXE',
            '3SZ-VE', '5S-FE',
            'M20A-FKS', 'M20A-FXS', 'A25A-FKS', 'A25A-FXS',
            # 6-cyl
            '1MZ-FE', '2GR-FE', '2GR-FKS', '2GR-FXS', '3GR-FE', '4GR-FSE',
            '1JZ-GE', '2JZ-GE', '2JZ-GTE',
            # V8
            '1UR-FE', '2UR-FE', '2UR-GSE', '3UR-FE', '1UZ-FE', '2UZ-FE', '3UZ-FE',
            # Diesel
            '1KD-FTV', '2KD-FTV', '1GD-FTV', '2GD-FTV', '1HZ', '1HD-T',
            # Hybrid
            '2ZR-FXE', 'A25A-FXS-Hybrid',
        ],
        'expert_focus': (
            'Toyota / Lexus — معرفة هندسية صارمة لـ:\n'
            '• 2GR-FE / 2AR-FE / 1NZ-FE — oil consumption + VVT-i actuator faults\n'
            '• 1KD-FTV / 2KD-FTV — injector failure، EGR cooler، DPF (Hilux/Fortuner/Prado)\n'
            '• Hybrid systems (THS-II) — inverter coolant pump، traction battery cells\n'
            '• U660E / A760 / AB60 gearboxes\n'
            '• Diagnostic procedures via Techstream\n'
            '• 1JZ/2JZ legacy على Supra/Mark II'
        ),
        'aliases': [
            'Corolla', 'Camry', 'Avalon', 'Yaris', 'Vitz', 'Echo',
            'Hilux', 'Fortuner', 'Land Cruiser', 'Prado', 'FJ Cruiser',
            'RAV4', 'Highlander', 'Sequoia', 'Tundra', 'Tacoma',
            'Prius', 'Innova', 'Avanza', 'Hiace', 'Coaster',
            'Supra', 'MR2', 'Celica', 'Mark II', 'Chaser', 'Cresta',
            'ES', 'IS', 'GS', 'LS', 'NX', 'RX', 'GX', 'LX', 'LC', 'LFA',
        ],
        'shop_faqs': [
            {'badge': '2AR-FE', 'label': 'Oil consumption',
             'q': '2AR-FE على Camry 2012 — استهلاك زيت 1L كل 1500 كم. خطوات فحص الـ rings والـ PCV'},
            {'badge': '2GR-FE', 'label': 'VVT-i oil leak',
             'q': '2GR-FE على RAV4 — تسريب من الـ VVT-i actuator oil line. رقم الـ updated metal line OEM'},
            {'badge': '1KD-FTV', 'label': 'Injector failure',
             'q': '1KD-FTV على Hilux 2010 — أعراض injector فاشل وعزوم الـ rail والـ injector hold-down bolt'},
            {'badge': '2ZR-FXE', 'label': 'Inverter water pump',
             'q': 'Prius Gen3 2ZR-FXE — كود P0A93 (inverter coolant flow low). تغيير الـ electric water pump'},
            {'badge': '1NZ-FE', 'label': 'Timing chain stretch',
             'q': '1NZ-FE على Yaris 2009 — رنين timing chain، كود P0016. عزوم الـ tensioner والـ guides'},
        ],
        'customer_faqs': [
            {'label': '🔧 Check Engine في كورولا',
             'q': 'كورولا 2015 ولّع Check Engine. ممكن أكمل سواقة ولا لازم أروح ميكانيكي؟'},
            {'label': '🛢️ استهلاك زيت Camry',
             'q': 'كامري 2013 بياكل زيت كل شهر. هل ده طبيعي للموديل ده؟'},
            {'label': '⚠️ Hybrid System Warning',
             'q': 'Prius — طلعت رسالة Check Hybrid System. خطر ولا أكمل لحد البيت؟'},
            {'label': '🚗 رعشة في Hilux ديزل',
             'q': 'هايلوكس 2012 ديزل — بترعش في الـ idle ودخان أسود عند البنشات'},
        ],
    },

    # ─────────────────────────────────────────────────────────────────
    'hyundai_kia': {
        'label': 'Hyundai / Kia',
        'emoji': '🇰🇷',
        'color': '#f59e0b',
        'engines': [
            # Theta II
            'G4KD', 'G4KE', 'G4KH', 'G4KJ', 'G4KL', 'G4KN',
            # Gamma
            'G4FA', 'G4FC', 'G4FD', 'G4FG', 'G4FJ', 'G4FM',
            # Kappa
            'G3LA', 'G3LC', 'G4LA', 'G4LC', 'G4LD', 'G4LE',
            # Nu
            'G4NA', 'G4NB', 'G4NC',
            # Lambda
            'G6DA', 'G6DB', 'G6DC', 'G6DH', 'G6DJ',
            # Smartstream
            'G1.6T-GDi', 'G2.5T-GDi',
            # Diesel
            'D4FD', 'D4HA', 'D4HB', 'D4CB', 'D4EA',
        ],
        'expert_focus': (
            'Hyundai / Kia — معرفة هندسية صارمة لـ:\n'
            '• Theta II GDi family (G4KH/G4KJ) — engine seizure recall، rod bearing failure\n'
            '• Gamma family (G4FA/G4FC/G4FD) — timing chain، oil control valve\n'
            '• Lambda V6 (G6DA/G6DB) — oil leak من الـ valve cover\n'
            '• GDi carbon buildup على الـ intake valves\n'
            '• Diagnostic procedures via GDS / KDS (Kia/Hyundai Diagnostic System)\n'
            '• 8-speed A8MF1/A8LF1 + Smartstream IVT CVT quirks'
        ),
        'aliases': [
            'Elantra', 'Sonata', 'Accent', 'Verna', 'i10', 'i20', 'i30',
            'Tucson', 'Santa Fe', 'Creta', 'Kona', 'Palisade', 'Venue',
            'Genesis', 'Equus', 'Veloster',
            'Picanto', 'Rio', 'Cerato', 'Forte', 'Optima', 'K5',
            'Sportage', 'Sorento', 'Carnival', 'Sedona', 'Soul', 'Stinger', 'Mohave',
        ],
        'shop_faqs': [
            {'badge': 'G4KH', 'label': 'Theta II rod knock',
             'q': 'Sonata 2014 G4KH 2.0T — knocking sound من الـ rod bearings. خطوات تشخيص KSDS وموقف الـ recall'},
            {'badge': 'G4FC', 'label': 'Timing chain noise',
             'q': 'Elantra 2013 G4FC — صوت timing chain rattle عند البدء. عزوم الـ tensioner والـ guides'},
            {'badge': 'G4KJ', 'label': 'GDi carbon buildup',
             'q': 'G4KJ Optima 2016 — misfire P0301 بسبب carbon على الـ intake valves. media للـ walnut blasting'},
            {'badge': 'G6DA', 'label': 'Lambda valve cover',
             'q': 'Genesis Coupe G6DA — تسريب زيت من valve cover gasket. عزوم التربيط ورقم الـ OEM'},
            {'badge': 'A8MF1', 'label': '8-speed harsh shift',
             'q': 'Sorento 2017 — gear box A8MF1 بيدي shocks عند التحويل من 2-3. الـ adaptation أو valve body؟'},
        ],
        'customer_faqs': [
            {'label': '🔊 صوت طرقعة في Elantra',
             'q': 'إلنترا 2014 — بسمع صوت طرقعة من المحرك لما أبدأ في الصبح، بيختفي بعد ما تسخن'},
            {'label': '⚠️ Engine Failure Warning',
             'q': 'Sonata 2.4 طلعت رسالة Engine Failure والمحرك بيقف فجأة. خطر؟'},
            {'label': '🛢️ استهلاك زيت Tucson',
             'q': 'Tucson 2016 GDi — استهلاك زيت زاد، فيه recall لده؟'},
            {'label': '🚗 سرعة ضعيفة في Sportage',
             'q': 'Sportage 2018 — العربية بقت ثقيلة وضعيفة، استهلاك بنزين عالي'},
        ],
    },

    # ─────────────────────────────────────────────────────────────────
    'nissan': {
        'label': 'Nissan / Infiniti',
        'emoji': '🇯🇵',
        'color': '#ef4444',
        'engines': [
            # QR / MR family
            'QR20DE', 'QR25DE', 'QR25DER',
            'MR16DDT', 'MR20DE', 'MR20DD',
            # HR / SR
            'HR12DE', 'HR12DDR', 'HR15DE', 'HR16DE',
            'SR20DE', 'SR20DET', 'SR16VE',
            # VQ V6
            'VQ23DE', 'VQ25DE', 'VQ25HR', 'VQ35DE', 'VQ35HR', 'VQ37VHR', 'VQ40DE',
            # VR / VK V8
            'VR30DDTT', 'VR38DETT', 'VK45DE', 'VK56DE', 'VK56VD',
            # Diesel
            'YD22DDT', 'YD25DDTi', 'ZD30DDTi', 'TD27', 'TD42', 'RD28',
            'M9R',
            # CVT codes
            'RE0F09A', 'RE0F09B', 'RE0F10A', 'RE0F10D', 'RE0F11A',
        ],
        'expert_focus': (
            'Nissan / Infiniti — معرفة هندسية صارمة لـ:\n'
            '• Jatco CVT family (RE0F09/10/11) — judder، slippage، valve body wear\n'
            '• VQ35DE / VQ35HR — oil consumption، timing chain tensioner\n'
            '• QR25DE — timing chain noise، oil consumption\n'
            '• MR20DD / MR16DDT direct injection — carbon buildup\n'
            '• YD25DDTi (Navara/Pathfinder) — turbo failure، EGR clog\n'
            '• Diagnostic procedures via CONSULT-III+'
        ),
        'aliases': [
            'Sunny', 'Tiida', 'Versa', 'Micra', 'Note', 'Almera',
            'Sentra', 'Altima', 'Maxima', 'Teana', 'Bluebird',
            'X-Trail', 'Qashqai', 'Juke', 'Murano', 'Pathfinder',
            'Patrol', 'Armada', 'Navara', 'Frontier', 'Hardbody', 'Urvan',
            '350Z', '370Z', 'GT-R', 'Skyline', 'Silvia',
            'Q30', 'Q50', 'Q60', 'Q70', 'QX50', 'QX60', 'QX70', 'QX80',
        ],
        'shop_faqs': [
            {'badge': 'CVT', 'label': 'Jatco judder',
             'q': 'X-Trail 2016 QR25 + RE0F10A CVT — judder عند الإقلاع وارتفاع حرارة CVT. valve body أو full rebuild؟'},
            {'badge': 'VQ35DE', 'label': 'Timing chain',
             'q': 'VQ35DE على Maxima — صوت rattle من الـ timing chain. أرقام الـ updated tensioner والـ procedure'},
            {'badge': 'QR25', 'label': 'Oil consumption',
             'q': 'QR25DE Altima 2010 — استهلاك زيت عالي. piston rings replacement وعزوم rod cap'},
            {'badge': 'YD25', 'label': 'Turbo failure',
             'q': 'Navara YD25DDTi 2014 — turbo whining + ضعف عزم. تشخيص الـ VGT actuator'},
            {'badge': 'MR16DDT', 'label': 'Carbon buildup',
             'q': 'Juke MR16DDT — misfire intermittent. carbon على الـ intake valves والـ walnut blasting'},
        ],
        'customer_faqs': [
            {'label': '⚠️ CVT بترتفع حرارته',
             'q': 'X-Trail — رسالة CVT Hot ظهرت على لوحة العدادات. خطر؟'},
            {'label': '🚗 تسارع مفاجئ في Altima',
             'q': 'Altima 2013 — أحياناً بتاخد سرعة لوحدها لما أكون واقف على إشارة. عيب؟'},
            {'label': '🛢️ استهلاك زيت Sunny',
             'q': 'Sunny 2014 بياكل زيت بسرعة، ودخان أزرق خفيف من الشكمان'},
            {'label': '🔧 صوت في Navara ديزل',
             'q': 'نافارا 2015 ديزل — صوت من التيربو وعزم ضعيف عند الصعود'},
        ],
    },

    # ─────────────────────────────────────────────────────────────────
    'honda': {
        'label': 'Honda / Acura',
        'emoji': '🇯🇵',
        'color': '#0ea5e9',
        'engines': [
            # K-series
            'K20A', 'K20A2', 'K20A3', 'K20Z3', 'K24A', 'K24A4', 'K24Z3', 'K24W', 'K24V',
            # L-series
            'L13A', 'L15A', 'L15B', 'L15B7', 'LFA', 'LEA',
            # R-series
            'R18A', 'R20A', 'R20Z',
            # J-series V6
            'J30A', 'J32A', 'J35A', 'J35Y', 'J37A',
            # Honda Earth Dreams turbo
            'L15B7', 'K20C1', 'K20C4',
            # Civic Type R
            'K20A1', 'K20C1-FK8',
        ],
        'expert_focus': (
            'Honda / Acura — معرفة هندسية صارمة لـ:\n'
            '• K-series (K20A/K24A) — VTEC solenoid، rocker arm wear\n'
            '• J35A V6 (Accord/Odyssey/Pilot) — VCM cylinder deactivation issues\n'
            '• L15B7 1.5L Turbo — oil dilution problem (gasoline in oil)\n'
            '• R18/R20 — variable valve timing actuator\n'
            '• CVT BC family (Civic/CR-V) — judder + chain failure\n'
            '• Diagnostic procedures via HDS (Honda Diagnostic System)'
        ),
        'aliases': [
            'Civic', 'Accord', 'City', 'Jazz', 'Fit', 'CR-V', 'HR-V', 'BR-V', 'Pilot', 'Passport', 'Odyssey',
            'Ridgeline', 'Element', 'Insight', 'Stream', 'Stepwgn',
            'Integra', 'NSX', 'S2000', 'Prelude', 'Civic Type R',
            'TLX', 'ILX', 'RLX', 'MDX', 'RDX', 'TL', 'TSX', 'ZDX',
        ],
        'shop_faqs': [
            {'badge': 'L15B7', 'label': 'Oil dilution',
             'q': 'CR-V 2018 L15B7 1.5T — مستوى الزيت بيرتفع وفيه ريحة بنزين. الموقف الرسمي للـ ECM update'},
            {'badge': 'J35A', 'label': 'VCM misfire',
             'q': 'Pilot J35A V6 — misfire P0302 على الـ rear bank بسبب VCM. الـ VCMtuner أو piston rings؟'},
            {'badge': 'K24A', 'label': 'VTEC oil pressure',
             'q': 'K24A Accord — كود P2647 (VTEC stuck). الـ rocker oil pressure switch والـ solenoid'},
            {'badge': 'R18A', 'label': 'Timing chain stretch',
             'q': 'Civic 2010 R18A — كود P0016 timing chain stretch. عزم الـ tensioner والـ guide'},
            {'badge': 'CVT', 'label': 'Honda CVT judder',
             'q': 'Civic 2017 — CVT judder عند الإقلاع البطيء. الـ ATF + adaptation؟'},
        ],
        'customer_faqs': [
            {'label': '🛢️ زيت بيزيد في CR-V',
             'q': 'CR-V 2018 1.5 — مستوى الزيت بيزيد لوحده وفيه ريحة بنزين. سمعت ده عيب معروف'},
            {'label': '⚠️ Check Engine في Civic',
             'q': 'Civic 2015 طلعت Check Engine ودا بيحصل بعد ما العربية تشتغل بساعة'},
            {'label': '🔧 صوت رفافة من Accord',
             'q': 'أكورد V6 — بسمع صوت رفافة (ticking) من المحرك في الـ idle'},
            {'label': '🚗 رعشة في City',
             'q': 'سيتي 2017 بترعش وهي واقفة على الإشارة. عيب في الـ throttle body؟'},
        ],
    },

    # ─────────────────────────────────────────────────────────────────
    'chevrolet': {
        'label': 'Chevrolet / GM / Opel',
        'emoji': '🇺🇸',
        'color': '#fbbf24',
        'engines': [
            # GM Family 0 / Family 1 (Lanos / Aveo / Spark)
            'A13DTC', 'A14NET', 'A14XEL', 'A14XER',
            'F14D3', 'F16D3', 'F16D4', 'F18D4',
            # Ecotec
            'LDE', 'LUW', 'LUV', 'LUJ', 'LUZ', 'LUX', 'LUK',
            'L83', 'LE5', 'LEA', 'LFW', 'LFX', 'LLT', 'LY7',
            # LS / LT V8
            'LS1', 'LS3', 'LS7', 'LT1', 'LT4',
            # Cruze / Sonic / Trax
            '2H0', 'LWE', 'LFV', 'L3G', 'L2H',
            # Duramax diesel
            'LB7', 'LLY', 'LBZ', 'LMM', 'LML', 'LWN', 'L5P',
        ],
        'expert_focus': (
            'Chevrolet / GM / Opel — معرفة هندسية صارمة لـ:\n'
            '• F16D3 / F16D4 (Aveo, Optra, Lanos) — coolant leak من thermostat housing البلاستيك\n'
            '• LUW/LUV (Cruze 1.4T / 1.8) — PCV valve، water pump، intake manifold\n'
            '• LFV (Cruze 1.4T Gen2) — head gasket failure، piston ring wear\n'
            '• LDE / LUJ — timing chain stretch مع الـ direct injection carbon buildup\n'
            '• 6T30 / 6T40 / 6T45 gearboxes — wave plate fracture\n'
            '• Diagnostic via Tech 2 / GDS 2 / MDI'
        ),
        'aliases': [
            'Lanos', 'Aveo', 'Optra', 'Spark', 'Sonic', 'Cruze', 'Camaro', 'Captiva',
            'Equinox', 'Trax', 'Trailblazer', 'Tahoe', 'Suburban', 'Silverado', 'Colorado',
            'Malibu', 'Impala', 'Corvette', 'Blazer',
            'Lacetti', 'Epica', 'Kalos', 'Matiz', 'Nubira',
            'Astra', 'Corsa', 'Insignia', 'Mokka', 'Vectra', 'Zafira',
        ],
        'shop_faqs': [
            {'badge': 'F16D3', 'label': 'Coolant leak',
             'q': 'Optra F16D3 — تسريب coolant من thermostat housing البلاستيك. رقم القطعة الـ updated alu'},
            {'badge': 'LUW', 'label': 'Cruze water pump',
             'q': 'Cruze 1.8 LUW — تسريب من الـ water pump خلف الـ timing cover. ترتيب الفك وعزوم التركيب'},
            {'badge': 'LUJ', 'label': 'Timing chain',
             'q': 'Cruze 1.4T LUJ — كود P0008 timing chain stretch. عزوم الـ guides والـ tensioner الجديد'},
            {'badge': '6T40', 'label': '6T40 wave plate',
             'q': '6T40 على Cruze 2014 — slip في 3rd gear. الـ wave plate fracture وحلها'},
            {'badge': 'LFV', 'label': 'Head gasket',
             'q': 'Cruze Gen2 LFV 1.4T — اختلاط زيت بالمياه. اختبار head gasket والـ replacement procedure'},
        ],
        'customer_faqs': [
            {'label': '🛢️ تسريب مياه Aveo',
             'q': 'افيو 2010 — بيقل المياه باستمرار وفيه بقعة تحت الموتور'},
            {'label': '⚠️ Engine في Cruze',
             'q': 'كروز 2013 — ولّع Check Engine ومستوى الزيت بيقل. خطر؟'},
            {'label': '🔧 صوت في Lanos',
             'q': 'لانوس قديمة — بتطلع صوت طقطقة من المحرك صبح بس'},
            {'label': '🚗 ترددات في Optra',
             'q': 'أوبترا — في تردد عند التحويل من 1 لـ 2 على القير العادي'},
        ],
    },

    # ─────────────────────────────────────────────────────────────────
    'peugeot_citroen': {
        'label': 'Peugeot / Citroën / DS',
        'emoji': '🇫🇷',
        'color': '#3b82f6',
        'engines': [
            # TU / EW / EP legacy
            'TU3', 'TU5', 'TU5JP4',
            'EW7', 'EW10', 'EW12',
            # PSA / BMW Prince (1.4 / 1.6 THP)
            'EP3', 'EP6', 'EP6C', 'EP6CDT', 'EP6CDTX', 'EP6FDT', 'EP6FDTX', 'EP6FDTM',
            # PureTech (1.0 / 1.2 / 1.6 turbo)
            'EB0', 'EB2', 'EB2DT', 'EB2DTS', 'EB2ADT', 'EB2ADTS',
            'EP6FADTX',
            # Diesel HDi / BlueHDi
            'DV4', 'DV6', 'DV6C', 'DV6D', 'DV6FC', 'DV5R',
            'DW10', 'DW10BTED4', 'DW12C',
        ],
        'expert_focus': (
            'Peugeot / Citroën / DS — معرفة هندسية صارمة لـ:\n'
            '• EP6 (Prince 1.6 THP) — timing chain stretch، carbon buildup، VANOS solenoid (مشترك مع BMW N13/N14)\n'
            '• PureTech EB2DT/EB2DTS — wet timing belt failure، injector seal، coolant leak\n'
            '• 1.6 HDi DV6 — turbo failure، EGR cooler، DPF clog، injector seal\n'
            '• 1.5 BlueHDi DV5R — recall على الـ timing belt + oil pump\n'
            '• EAT6 / EAT8 / BMP6 gearboxes — Aisin AWF21 mechatronics\n'
            '• Diagnostic via DiagBox / Lexia / PP2000'
        ),
        'aliases': [
            # Peugeot
            '206', '207', '208', '301', '307', '308', '407', '408', '508', '607', '807',
            '2008', '3008', '4008', '5008',
            'Partner', 'Expert', 'Boxer', 'Bipper',
            # Citroën
            'C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C8', 'DS3', 'DS4', 'DS5', 'DS7',
            'Berlingo', 'Jumpy', 'Jumper', 'Xsara', 'Picasso',
        ],
        'shop_faqs': [
            {'badge': 'EP6', 'label': 'THP timing chain',
             'q': '208 EP6 1.6 THP — رنين timing chain وكود P0016. الـ updated chain kit والـ procedure'},
            {'badge': 'EP6', 'label': 'Carbon buildup',
             'q': '308 GTi EP6FDT — misfire P0301 من carbon على intake valves. media للـ walnut blast'},
            {'badge': 'EB2', 'label': 'PureTech wet belt',
             'q': '3008 PureTech 1.2 EB2 — تكسر الـ wet timing belt مبكر. رقم الـ kit والـ oil pump pickup screen'},
            {'badge': 'DV6', 'label': '1.6 HDi turbo',
             'q': '308 1.6 HDi DV6 — turbo whining + ضعف عزم. تشخيص الـ VGT والـ oil supply line'},
            {'badge': 'EAT6', 'label': 'AWF21 mechatronics',
             'q': '308 EAT6 — harsh shift 1-2. الـ adaptation أو valve body replacement؟'},
        ],
        'customer_faqs': [
            {'label': '🛢️ زيت بيقل في 208',
             'q': '208 موديل 2015 — استهلاك زيت زيادة عن الطبيعي ودخان أزرق خفيف'},
            {'label': '⚠️ STOP في 3008',
             'q': '3008 طلعت رسالة STOP حمرا. أوقف فوراً ولا أقدر أكمل لورشة قريبة؟'},
            {'label': '🔧 صوت في C4',
             'q': 'سيتروين C4 — صوت طقطقة من المحرك بيختفي بعد التسخين'},
            {'label': '🚗 وحدة قير في 308',
             'q': '308 قير أوتوماتيك — بيدي رجة عند التحويل من 1 لـ 2'},
        ],
    },

    # ─────────────────────────────────────────────────────────────────
    'renault_dacia': {
        'label': 'Renault / Dacia',
        'emoji': '🇫🇷',
        'color': '#facc15',
        'engines': [
            # K series petrol
            'K4J', 'K4M', 'K7J', 'K7M', 'K9K',
            # F series petrol
            'F4R', 'F4RT', 'F9Q',
            # Energy / TCe family
            'H4Bt', 'H4Jt', 'H4M', 'H5Ht', 'H5Ft',
            'M5M', 'M5Mt',
            'M9R', 'M9T',
            # D series
            'D4F', 'D4D', 'D7F',
            # Diesel dCi
            'K9K', 'M9R', 'M9T', 'R9M', 'R9N',
        ],
        'expert_focus': (
            'Renault / Dacia — معرفة هندسية صارمة لـ:\n'
            '• K4M (Megane / Logan 1.6) — coil pack failure، timing belt interval\n'
            '• K9K 1.5 dCi — injector failure (Delphi vs Bosch)، swirl flap motor\n'
            '• H5Ht 1.3 TCe (Megane IV / Captur II) — مشترك مع Mercedes M282 (DDD coolant leak)\n'
            '• EDC dual-clutch (DC4) و Easy-R AMT quirks\n'
            '• CVT JF015E / JF016E (Jatco) على Captur الجديدة\n'
            '• Diagnostic via CLIP / Easy Scan'
        ),
        'aliases': [
            # Renault
            'Clio', 'Megane', 'Symbol', 'Logan', 'Sandero', 'Duster', 'Captur',
            'Fluence', 'Talisman', 'Laguna', 'Espace', 'Scenic', 'Grand Scenic',
            'Kangoo', 'Trafic', 'Master', 'Koleos', 'Kadjar',
            'Twingo', 'Modus',
            # Dacia
            'Lodgy', 'Dokker', 'Spring',
        ],
        'shop_faqs': [
            {'badge': 'K4M', 'label': 'Coil pack',
             'q': 'Megane II K4M 1.6 — misfire P0301 من coil pack. رقم الـ OEM والـ updated coil'},
            {'badge': 'K9K', 'label': 'dCi injector',
             'q': 'Duster 1.5 dCi K9K — صوت knock من المحرك. أي حقن (Delphi/Bosch) وعزم الـ clamp'},
            {'badge': 'H5Ht', 'label': '1.3 TCe coolant',
             'q': 'Captur II 1.3 TCe H5Ht — تسريب coolant داخل المحرك (Mercedes M282 issue). الإصلاح'},
            {'badge': 'EDC', 'label': 'DC4 dual-clutch',
             'q': 'Megane III EDC — judder عند الإقلاع. الـ clutch adaptation أو replacement؟'},
            {'badge': 'F4R', 'label': 'Phase variator',
             'q': 'Megane RS F4RT — رنين phase variator (VVT). رقم القطعة وعزم التركيب'},
        ],
        'customer_faqs': [
            {'label': '🚗 ضعف عزم في Duster',
             'q': 'داستر 1.5 ديزل — ضعف عزم مع نور engine، يجي ويروح'},
            {'label': '🔧 صوت في Logan',
             'q': 'لوجان 1.6 — صوت طقطقة من المحرك صبح'},
            {'label': '⚠️ Service due',
             'q': 'مجان IV — رسالة Service Required ظهرت لوحدها. لازم برمجة؟'},
            {'label': '🛢️ تسريب زيت',
             'q': 'فلونس — بقعة زيت تحت العربية بعد الوقوف ليلة كاملة'},
        ],
    },

    # ─────────────────────────────────────────────────────────────────
    'mitsubishi': {
        'label': 'Mitsubishi',
        'emoji': '🇯🇵',
        'color': '#dc2626',
        'engines': [
            # 4G family (legacy + Lancer/Outlander)
            '4G13', '4G15', '4G18', '4G19', '4G63', '4G63T', '4G64', '4G69', '4G92', '4G93', '4G94',
            # 4B family (MIVEC modern)
            '4B10', '4B11', '4B11T', '4B12', '4B40',
            # 4J / 4N (new generation + diesel)
            '4J10', '4J11', '4J12',
            '4N13', '4N14', '4N15',
            # V6
            '6A12', '6A13', '6G72', '6G73', '6G74', '6G75',
            # Pajero diesel
            '4D56', '4D56T', '4M40', '4M41', '4M42',
            # CVT codes
            'F1CJA', 'CVT8',
        ],
        'expert_focus': (
            'Mitsubishi — معرفة هندسية صارمة لـ:\n'
            '• 4B11 (Lancer EX / Outlander) — timing chain، oil control valve\n'
            '• 4G63T (Lancer Evo legacy) — turbo، forged internals procedure\n'
            '• 4D56 / 4M41 (Pajero / L200 ديزل) — balance shaft، injector failure\n'
            '• 6G74 / 6G75 (Pajero V6) — timing belt interval، oil leak\n'
            '• Jatco JF011E CVT (Lancer/Outlander) — judder، valve body\n'
            '• Diagnostic via MUT-III'
        ),
        'aliases': [
            'Lancer', 'Mirage', 'Colt', 'Galant', 'ASX', 'Outlander', 'Eclipse', 'Eclipse Cross',
            'Pajero', 'Montero', 'Shogun', 'L200', 'Triton', 'Strada',
            'Attrage', 'Xpander', 'Space Wagon', 'Grandis', 'Delica', 'Canter',
            'EVO', 'Evolution', 'FTO', 'GTO', '3000GT',
        ],
        'shop_faqs': [
            {'badge': '4B11', 'label': 'Timing chain noise',
             'q': 'Lancer EX 4B11 — رنين timing chain صبح. عزوم الـ tensioner والـ guides'},
            {'badge': '4D56', 'label': 'L200 balance shaft',
             'q': 'L200 4D56 — صوت من balance shaft. الـ chain tensioner أو balance shaft replacement؟'},
            {'badge': '6G74', 'label': 'Pajero timing belt',
             'q': 'Pajero V6 6G74 — interval الـ timing belt + water pump + tensioner. أرقام OEM'},
            {'badge': 'JF011E', 'label': 'CVT judder',
             'q': 'Lancer EX CVT JF011E — judder عند الإقلاع. الـ ATF spec والـ adaptation'},
            {'badge': '4G15', 'label': 'MIVEC actuator',
             'q': 'Mirage 4G15 — كود P0011 (VVT). الـ oil control valve والـ MIVEC actuator'},
        ],
        'customer_faqs': [
            {'label': '🚗 CVT في لانسر',
             'q': 'لانسر CVT — العربية بقت بتاخد سرعة بصعوبة، احتمال عيب في القير؟'},
            {'label': '⚠️ Check Engine ASX',
             'q': 'ASX 2014 — ولّع Check Engine بدون أي أعراض. خطر؟'},
            {'label': '🔧 صوت في Pajero',
             'q': 'باجيرو ديزل — صوت من المحرك زاد بعد ما العربية مشيت 200 ألف'},
            {'label': '🛢️ تسريب زيت L200',
             'q': 'L200 ديزل — تسريب زيت من خلف المحرك ناحية الـ flywheel'},
        ],
    },

    # ─────────────────────────────────────────────────────────────────
    'chinese': {
        'label': 'MG / Chery / Geely / BYD',
        'emoji': '🇨🇳',
        'color': '#ec4899',
        'engines': [
            # MG / SAIC
            '15S4C', '15S4G', '15S4H', '20T4H', '13NTGE', '15NTGE',
            'NSE', 'NLE', 'NetA',
            # Chery
            'SQR371', 'SQR472', 'SQRE4G16', 'SQRE4T15', 'SQRE4G15B',
            'SQRD4G15T', 'SQRD4T15C', 'SQRF4J20',
            'ACTECO-1.5T', 'ACTECO-1.6T', 'ACTECO-2.0T',
            # Geely
            'JLD-4G15', 'JLD-4G18', 'JLD-4G20',
            'JLH-3G15TD', 'JLH-4G15TD', 'JLH-4G18TD', 'JLH-4G20TDB',
            'BHE15-EFZ',
            # BYD
            'BYD476ZQA', 'BYD473QE', 'BYD483QB',
            'BYD476ZQB',
            'DM-i', 'DM-p',  # hybrid
            # Great Wall / Haval
            'GW4D20', 'GW4G15B', 'GW4N20', 'GW4B15',
        ],
        'expert_focus': (
            'الماركات الصينية (MG / Chery / Geely / BYD / Great Wall) — معرفة هندسية صارمة لـ:\n'
            '• MG (SAIC) — استخدام محركات Volvo VEA معدلة (NSE/NLE)، DCT mechatronics\n'
            '• Chery ACTECO — تطور من Tritec إلى ACTECO Gen3 (1.5T/1.6T)\n'
            '• Geely (Volvo-derived JLH series) — محركات مرخصة من Volvo، 7DCT/CVT\n'
            '• BYD DM-i hybrid — Atkinson 1.5L + EV motor، Blade battery\n'
            '• Diagnostic غالباً يحتاج Launch X431 أو Autel + brand-specific software\n'
            '• ⚠️ قطع الغيار: بعض الأكواد لها بدائل من VW/Volvo/Mitsubishi (origin engines)'
        ),
        'aliases': [
            # Brand keywords — bare matches catch compound names like
            # "MG ZS", "BYD Atto 3", "Chery Tiggo 7" with one token.
            'MG', 'BYD', 'Chery', 'Geely', 'Haval', 'Wuling', 'SAIC',
            # Specific full model names (long enough not to false-positive)
            'MG3', 'MG4', 'MG5', 'MG6', 'MG7', 'RX5', 'RX8',
            'Marvel', 'Cyberster',
            'Tiggo', 'Arrizo', 'Eastar', 'Fulwin', 'Cowin',
            'Emgrand', 'Coolray', 'Tugella', 'Azkarra', 'Boyue', 'Okavango', 'Monjaro',
            'Dolphin', 'Atto', 'Trumpchi', 'Changan',
            # Sub-brands
            'JAC', 'Dongfeng', 'GAC',
            'Wingle', 'Hover', 'Jolion', 'Dargo',
        ],
        'shop_faqs': [
            {'badge': 'MG ZS', 'label': '15S4C timing chain',
             'q': 'MG ZS 15S4C 1.5 — رنين timing chain بعد 60 ألف. الـ tensioner update والـ chain kit OEM'},
            {'badge': 'Chery', 'label': 'ACTECO carbon',
             'q': 'Tiggo 7 ACTECO 1.5T — misfire intermittent من carbon buildup. media للـ walnut blast'},
            {'badge': 'Geely', 'label': 'JLH 3-cyl vibration',
             'q': 'Coolray JLH-3G15TD — vibration في idle ودخان أزرق خفيف. الـ engine mount أو piston rings؟'},
            {'badge': 'BYD', 'label': 'DM-i hybrid system',
             'q': 'BYD Song DM-i — رسالة Hybrid System Check. تشخيص الـ traction battery isolation'},
            {'badge': 'Haval', 'label': 'H6 DCT judder',
             'q': 'Haval H6 7DCT — judder عند الإقلاع. clutch adaptation أو mechatronics؟'},
        ],
        'customer_faqs': [
            {'label': '⚠️ Check في MG ZS',
             'q': 'MG ZS موديل 2020 — Check Engine بيولع كل فترة، استهلاك بنزين زاد'},
            {'label': '🔋 بطارية BYD',
             'q': 'BYD Atto 3 — رسالة Battery Cell Imbalance. خطر؟'},
            {'label': '🚗 صوت في Tiggo',
             'q': 'تيجو 7 — صوت طقطقة من تحت العربية لما أعدي مطبات'},
            {'label': '🛢️ تسريب Haval',
             'q': 'هافال H6 — تسريب زيت بسيط بعد ما العربية تقف ساعات'},
        ],
    },
}


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────
def get_brand(key: str) -> dict | None:
    """Return brand entry or None if unknown."""
    return DIAGNOSTIC_BRANDS.get((key or '').lower())


def all_engine_codes() -> list[str]:
    """Flat list of every supported engine code across all brands."""
    seen: set[str] = set()
    out: list[str] = []
    for b in DIAGNOSTIC_BRANDS.values():
        for code in b.get('engines', []):
            if code not in seen:
                seen.add(code)
                out.append(code)
    return out


def detect_brand_from_text(text: str) -> str | None:
    """Cheap brand sniff — match the first brand whose engine code or alias
    appears in text as a whole token (word boundary on both sides).

    Substring matching is unsafe because short chassis codes like 'B5' or 'A1'
    would false-positive on engine codes like 'L15B7' or arbitrary part numbers.
    We tokenise the input and require an exact-token match.
    """
    if not text:
        return None
    # Token = run of letters/digits/hyphen. Lowercases all-at-once for cheap
    # case-insensitive comparison against pre-lowered catalogs.
    tokens = {t for t in re.findall(r'[A-Za-z0-9\-]+', text)}
    if not tokens:
        return None
    tokens_upper = {t.upper() for t in tokens}

    for key, brand in DIAGNOSTIC_BRANDS.items():
        # Engine code match (exact token)
        for code in brand.get('engines', []):
            if code.upper() in tokens_upper:
                return key
        # Alias match (exact token) — chassis codes, model names, etc.
        for alias in brand.get('aliases', []):
            if alias.upper() in tokens_upper:
                return key
    return None
