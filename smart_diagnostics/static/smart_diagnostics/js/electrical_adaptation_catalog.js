/* ============================================================
 * electrical_adaptation_catalog.js
 * ============================================================
 *
 * Two related diagnostic surfaces consolidated in one file:
 *
 * 1. BATTERY_HEALTH_THRESHOLDS — universal voltage gates the
 *    runBatteryChargingTest() routine uses to verdict each phase
 *    (rest, cranking, idle charging, rev charging). Values come
 *    from SAE J537 / BCI battery standards plus IBS practice.
 *
 * 2. ADAPTATION_PROCEDURES — manufacturer relearn / adaptation
 *    flows. Each procedure is a sequence of either:
 *      • manual: instructions the mechanic must follow on the car
 *      • clear:  Mode 04 clear DTCs
 *      • write:  UDS Mode 0x2E writeDataByIdentifier (needs CAN +
 *                often an extended diagnostic session 10 03)
 *      • wait:   pause N seconds (e.g. ignition cycle settling)
 *
 * IMPORTANT: Many proprietary relearns require special equipment
 * (J2534, dealer-level tools) — we ship the BEST-EFFORT subset
 * achievable via ELM327. Procedures we can't do safely are listed
 * with manual-only steps so the mechanic at least has a checklist.
 */

// ── Universal voltage gates ─────────────────────────────────────────────
// All in volts. Phases match runBatteryChargingTest() phase keys.
const BATTERY_HEALTH_THRESHOLDS = {
    rest: {        // engine OFF, settled ≥ 4 hours (or as close as practical)
        good:        12.6,
        borderline:  12.4,
        weak:        12.2,
        // Below 12.0 → discharged or dead cell.
    },
    crank: {       // voltage observed during cranking (lowest dip)
        good:         9.6,        // healthy AGM/flooded
        borderline:   9.0,
        weak:         8.5,
        // Below 8.0 → starter draws excessive current OR battery sulfated.
    },
    idle_charging: {  // engine running, no electrical load
        good_low:    13.5,
        good_high:   14.7,
        // Outside [13.2, 15.0] → alternator regulator suspect.
    },
    rev_charging: {   // engine ~ 2000 RPM, loads on (lights+blower+rear defog)
        good_low:    13.6,
        good_high:   14.7,
        // < 13.6 under load → alternator output insufficient.
    },
};

function verdictForPhase(phaseKey, voltage) {
    const t = BATTERY_HEALTH_THRESHOLDS[phaseKey];
    if (!t || voltage == null || !isFinite(voltage)) {
        return { level: 'unknown', message: 'لا توجد قراءة' };
    }
    if (phaseKey === 'rest') {
        if (voltage >= t.good)        return { level: 'good',       message: 'بطارية ممتازة' };
        if (voltage >= t.borderline)  return { level: 'borderline', message: 'بطارية جيدة لكن تحتاج متابعة' };
        if (voltage >= t.weak)        return { level: 'weak',       message: 'بطارية ضعيفة — احتمال استبدال قريب' };
        return { level: 'bad', message: 'بطارية فاضية أو خلية موتت — استبدال فوري' };
    }
    if (phaseKey === 'crank') {
        if (voltage >= t.good)        return { level: 'good',       message: 'سحب السلف طبيعي' };
        if (voltage >= t.borderline)  return { level: 'borderline', message: 'بطارية تنخفض تحت السلف — افحصها' };
        if (voltage >= t.weak)        return { level: 'weak',       message: 'سحب السلف عالي أو بطارية ضعيفة' };
        return { level: 'bad', message: 'انهيار جهد عند التشغيل — السلف أو البطارية تالفة' };
    }
    if (phaseKey === 'idle_charging' || phaseKey === 'rev_charging') {
        if (voltage >= t.good_low && voltage <= t.good_high) {
            return { level: 'good', message: 'الدينامو شغّال صح' };
        }
        if (voltage < t.good_low) {
            return { level: 'weak', message: 'الدينامو مش بيشحن كفاية' };
        }
        return { level: 'bad', message: 'جهد شحن مرتفع — منظم الدينامو معطل' };
    }
    return { level: 'unknown', message: '' };
}

// ── Per-make adaptation / relearn procedures ────────────────────────────
// Each procedure: { id, make, name, severity, requires_can?, steps: [...] }
// Step types:
//   { type: 'manual', text: '...' }
//   { type: 'clear' }                                   // Mode 04
//   { type: 'wait', seconds: 5 }
//   { type: 'write', module: 'engine', did: '...', data: '...' }   // UDS 0x2E
//   { type: 'session', module: 'engine', session: '03' }            // UDS 0x10
//
// We KEEP destructive UDS writes to a minimum and ALWAYS expose them
// behind a confirmation step in the UI.
const ADAPTATION_PROCEDURES = [
    // ──────────── GENERIC (works on most cars) ────────────
    {
        id: 'generic_clear_adaptive',
        make: 'generic',
        name: 'مسح الذاكرة التكيّفية للـ ECU',
        description: 'بعد تغيير حساس MAF/O2/TPS — معظم العربيات بتعيد تعلّم القيم بعد دورتين قيادة.',
        severity: 'low',
        steps: [
            { type: 'manual',  text: 'تأكد إن المحرك في درجة حرارة التشغيل (>80°C).' },
            { type: 'manual',  text: 'أوقف كل الأجهزة الكهربائية (AC، راديو، أنوار).' },
            { type: 'clear',   text: 'Mode 04 — مسح الـ DTCs والـ adaptive memory.' },
            { type: 'wait',    seconds: 5, text: 'انتظر استقرار الـ ECU.' },
            { type: 'manual',  text: 'شغّل المحرك ودعه يدور 60 ثانية في idle.' },
            { type: 'manual',  text: 'اقفل الـ contact، انتظر 30 ثانية، ثم شغّل تاني.' },
            { type: 'manual',  text: 'سُق العربية لمدة 10-15 دقيقة بسرعات متنوعة لإكمال إعادة التعلّم.' },
        ],
    },

    // ──────────── BMW ────────────
    {
        id: 'bmw_battery_registration',
        make: 'bmw',
        name: 'تسجيل بطارية BMW (Battery Registration)',
        description: 'لازمة بعد تغيير البطارية في BMW E/F/G-series. لو ما اتعملتش، السيستم هيشحن بشكل خاطئ ويقصّر عمرها.',
        severity: 'high',
        requires_can: true,
        steps: [
            { type: 'manual',  text: 'تأكد إن نوع البطارية الجديد مطابق للأصلي (AGM/Lead-acid).' },
            { type: 'manual',  text: 'سجّل سعة البطارية (Ah) ورقم الـ S/N من الملصق.' },
            { type: 'session', module: 'engine', session: '03',
              text: 'فتح Extended Diagnostic Session.' },
            { type: 'write',   module: 'engine', did: 'F1A1', data: 'TBD_BATTERY_CAPACITY',
              text: 'كتابة سعة البطارية (DID F1A1) — قيم نموذجية: 4650=46.5Ah, 5000=50Ah, 6000=60Ah, 7000=70Ah.',
              data_input: 'capacity_ah' },
            { type: 'write',   module: 'engine', did: 'F1A2', data: 'TBD_BATTERY_TYPE',
              text: 'كتابة نوع البطارية (DID F1A2) — 01=Lead-acid, 02=AGM, 03=EFB.',
              data_input: 'battery_type' },
            { type: 'manual',  text: 'اقفل الـ contact 30 ثانية ثم اشغّل للتأكد من قبول الإعدادات.' },
        ],
    },
    {
        id: 'bmw_cbs_reset',
        make: 'bmw',
        name: 'تصفير عدّاد الصيانة CBS',
        description: 'تصفير Condition Based Service بعد عمل صيانة.',
        severity: 'low',
        requires_can: true,
        steps: [
            { type: 'manual',  text: 'حدّد بنود الصيانة المنفّذة (زيت، فلاتر، فرامل، إلخ).' },
            { type: 'manual',  text: 'CBS reset في BMW بيتم عادةً من Idrive — اتبع كتيب المالك.' },
            { type: 'manual',  text: 'بديل: ثبّت زر الـ trip على عداد المسافة لمدة 10 ثوانٍ والـ contact ON بدون تشغيل.' },
        ],
    },

    // ──────────── VAG (VW / Audi / Skoda / SEAT) ────────────
    {
        id: 'vag_throttle_adaptation',
        make: 'vag',
        name: 'تعلّم الخانق VAG (Throttle Adaptation)',
        description: 'لازمة بعد تنظيف/تغيير جسم الخانق أو الـ TPS في عربيات VW/Audi.',
        severity: 'medium',
        steps: [
            { type: 'manual',  text: 'الـ contact ON لكن المحرك متوقف.' },
            { type: 'manual',  text: 'انتظر 2 دقيقة بدون لمس بدّال البنزين.' },
            { type: 'clear',   text: 'Mode 04 — مسح الأكواد لبدء التعلّم.' },
            { type: 'wait',    seconds: 30, text: 'انتظر 30 ثانية ليسمع الـ ECU صوت موتور الخانق وهو يعيد المعايرة.' },
            { type: 'manual',  text: 'اقفل الـ contact 10 ثوانٍ.' },
            { type: 'manual',  text: 'شغّل المحرك ودعه يدور 3 دقايق idle (مفيش بنزين).' },
            { type: 'manual',  text: 'سُق دورة قيادة كاملة مع تباطؤ كامل عدة مرات.' },
        ],
    },

    // ──────────── Toyota / Lexus ────────────
    {
        id: 'toyota_idle_relearn',
        make: 'toyota',
        name: 'إعادة تعلّم Idle (Toyota / Lexus)',
        description: 'بعد قطع البطارية أو تنظيف الخانق على Corolla/Camry/Yaris/Lexus.',
        severity: 'low',
        steps: [
            { type: 'manual',  text: 'تأكد إن المحرك في درجة حرارة التشغيل.' },
            { type: 'manual',  text: 'AC ON على درجة عالية.' },
            { type: 'manual',  text: 'دع المحرك idle 10 دقايق بدون تدخّل.' },
            { type: 'manual',  text: 'بعدها AC OFF ودع 10 دقايق إضافية idle.' },
            { type: 'clear',   text: 'Mode 04 — مسح أي codes ظهرت.' },
            { type: 'manual',  text: 'سُق العربية 15 دقيقة بسرعات مختلفة لإكمال التعلّم.' },
        ],
    },

    // ──────────── Hyundai / Kia ────────────
    {
        id: 'hyundai_etc_adaptation',
        make: 'hyundai',
        name: 'تعلّم بدّال البنزين الإلكتروني (ETC)',
        description: 'لازمة بعد تنظيف/تغيير جسم الخانق على Hyundai/Kia.',
        severity: 'medium',
        steps: [
            { type: 'manual',  text: 'الـ contact OFF تماماً.' },
            { type: 'manual',  text: 'لا تضغط على بدّال البنزين.' },
            { type: 'manual',  text: 'الـ contact ON بدون تشغيل لمدة 10 ثوانٍ.' },
            { type: 'manual',  text: 'الـ contact OFF 10 ثوانٍ.' },
            { type: 'manual',  text: 'كرّر الخطوتين السابقتين 3 مرات.' },
            { type: 'clear',   text: 'Mode 04 — مسح الأكواد.' },
            { type: 'manual',  text: 'شغّل المحرك ودعه idle 3 دقايق.' },
        ],
    },

    // ──────────── Honda / Acura ────────────
    {
        id: 'honda_etcs_relearn',
        make: 'honda',
        name: 'تعلّم Honda ETCS / Idle',
        description: 'بعد تغيير ECM أو قطع البطارية على Civic/Accord/CR-V.',
        severity: 'low',
        steps: [
            { type: 'manual',  text: 'تأكد إن المحرك بارد (أقل من 35°C).' },
            { type: 'manual',  text: 'AC OFF، كل الأحمال OFF.' },
            { type: 'manual',  text: 'الـ contact ON بدون تشغيل لمدة 2 ثانية.' },
            { type: 'manual',  text: 'شغّل المحرك ودعه يسخّن لـ idle طبيعي.' },
            { type: 'manual',  text: 'بعد ما مروحة التبريد تشتغل وتطفئ، AC ON لمدة 5 دقايق.' },
            { type: 'clear',   text: 'Mode 04 — مسح الأكواد.' },
        ],
    },

    // ──────────── Ford ────────────
    {
        id: 'ford_kam_reset',
        make: 'ford',
        name: 'تصفير ذاكرة Ford KAM',
        description: 'Keep Alive Memory reset — يجبر الـ PCM يعيد تعلّم كل الـ adaptive values.',
        severity: 'medium',
        steps: [
            { type: 'manual',  text: 'افصل الكابل السالب من البطارية.' },
            { type: 'manual',  text: 'دوس على الفرامل لمدة 30 ثانية لتفريغ الكبسولات.' },
            { type: 'manual',  text: 'انتظر 5 دقايق إضافية.' },
            { type: 'manual',  text: 'وصّل البطارية تاني.' },
            { type: 'manual',  text: 'الـ contact ON بدون تشغيل 30 ثانية.' },
            { type: 'manual',  text: 'شغّل المحرك ودعه idle حتى مروحة التبريد تشتغل دورتين.' },
            { type: 'clear',   text: 'Mode 04 — مسح أي codes حصلت أثناء العملية.' },
        ],
    },

    // ──────────── Nissan / Infiniti ────────────
    {
        id: 'nissan_idle_relearn',
        make: 'nissan',
        name: 'إعادة تعلّم Idle (Nissan/Infiniti)',
        description: 'لـ Sunny/Tiida/Sentra/Altima بعد تغيير جسم الخانق.',
        severity: 'medium',
        steps: [
            { type: 'manual',  text: 'حرارة المحرك > 70°C، AC OFF.' },
            { type: 'manual',  text: 'الـ contact OFF لـ 10 ثوانٍ.' },
            { type: 'manual',  text: 'الـ contact ON بدون تشغيل لـ 3 ثوانٍ.' },
            { type: 'manual',  text: 'في خلال ثانية: ادوس بدّال البنزين كامل 5 مرات بسرعة (في 5 ثوانٍ).' },
            { type: 'manual',  text: 'انتظر 7 ثوانٍ.' },
            { type: 'manual',  text: 'ادوس البدّال كامل لمدة 10 ثوانٍ — لمبة الـ Check Engine لازم تبدأ تطفي وتولّع.' },
            { type: 'manual',  text: 'ارفع الرجل عن البدّال. لمبة CEL لازم تطفي خلال 3 ثوانٍ.' },
            { type: 'manual',  text: 'شغّل المحرك ودعه idle 20 ثانية.' },
        ],
    },
];

function getAdaptationProceduresForMake(make) {
    if (!make) return ADAPTATION_PROCEDURES.filter(p => p.make === 'generic');
    const key = String(make).toLowerCase().trim();
    const aliases = {
        lexus: 'toyota', kia: 'hyundai', mini: 'bmw', infiniti: 'nissan',
        acura: 'honda', lincoln: 'ford', vw: 'vag', volkswagen: 'vag',
        audi: 'vag', skoda: 'vag', seat: 'vag', 'mercedes-benz': 'mercedes',
    };
    const normalized = aliases[key] || key;
    return ADAPTATION_PROCEDURES.filter(
        p => p.make === 'generic' || p.make === normalized,
    );
}

// Export to window for driver/UI consumption.
if (typeof window !== 'undefined') {
    window.BATTERY_HEALTH_THRESHOLDS         = BATTERY_HEALTH_THRESHOLDS;
    window.verdictForPhase                   = verdictForPhase;
    window.ADAPTATION_PROCEDURES             = ADAPTATION_PROCEDURES;
    window.getAdaptationProceduresForMake    = getAdaptationProceduresForMake;
}
