"""Shared seed data for the granular billing catalog.

Both the 0006_seed_initial_features data migration AND test setUp
helpers import from here so the catalog is defined ONCE and stays in
sync. Editing this file is the only place to add a new starter feature
or starter package — both production and tests pick it up automatically.
"""
from __future__ import annotations

# (code, name, category, default_operation_type, description, sort_order)
INITIAL_FEATURES: list[tuple[str, str, str, str, str, int]] = [
    # ── Coding family ──────────────────────────────────────────────────
    ("f_series_coding", "F-Series Coding (CAFD/FA-VO)", "coding", "coding",
     "FA / VO injection and CAFD edits for F-chassis BMW (F10/F30/F20...).", 10),
    ("g_series_coding", "G-Series Coding", "coding", "coding",
     "G-chassis (G20, G30, G05...) — UDS-22 routine coding via FEM/BDC.", 20),
    ("e_series_coding", "E-Series Coding (NCS)", "coding", "coding",
     "E-chassis NCS-Expert style coding via K-line / CAN.", 30),
    # ── Repair family ──────────────────────────────────────────────────
    ("frm_repair", "FRM3 Footwell Module Repair", "repair", "repair",
     "BDM read + cloud rebuild + flash-back for corrupted FRM3 on E90 / MINI R56.", 100),
    ("bdc_repair", "FEM/BDC Module Repair", "repair", "repair",
     "Recover bricked FEM/BDC after failed coding or dead 12 V.", 110),
    # ── Key programming ────────────────────────────────────────────────
    ("key_programming", "Key Programming (CAS3 / CAS3+)", "key_programming", "coding",
     "Bench-mode ISN extraction → Mousstec cloud key gen → write to CAS.", 200),
    ("key_programming_fem", "Key Programming (FEM/BDC)", "key_programming", "coding",
     "FEM/BDC ISN learn + key slot generation over UDS bootloader.", 210),
    # ── ISN / module-swap resets ───────────────────────────────────────
    ("egs_isn_reset", "EGS / 8HP ISN Reset", "isn_reset", "isn",
     "Clear ISN binding on a used 8HP gearbox so it accepts the new CAS/DME.", 300),
    ("dme_isn_clone", "DME ISN Clone (used DME swap)", "isn_reset", "isn",
     "Clone ISN onto a replacement DME so EWS/CAS sees a paired engine ECU.", 310),
    # ── Crash reset ────────────────────────────────────────────────────
    ("acsm_crash_reset", "ACSM / Airbag Crash Reset", "crash_reset", "reset",
     "Clear crash data from ACSM (Airbag SRS) over OBD — no soldering required.", 400),
    # ── Battery / CBS ──────────────────────────────────────────────────
    ("cbs_battery_manager", "CBS & Battery Registration", "battery", "coding",
     "Register new battery (AGM / lead-acid) + reset CBS counters.", 500),
    # ── Diagnostic ─────────────────────────────────────────────────────
    ("diagnostic_room", "Smart Diagnostics Room (AI)", "diagnostic", "coding",
     "Live OBD + ISTA test plans + AI chat assistant for the workshop bay.", 900),
    ("full_system_scan", "Full-System Auto-Scan", "diagnostic", "coding",
     "فحص شامل لكل وحدات السيارة + تقرير صحة بألوان (أخضر/أصفر/أحمر).", 910),
    ("live_data_stream", "Live Data + Graphing", "diagnostic", "coding",
     "بث حي لحساسات الموتور مع رسم بياني وتنبيه القيم خارج المدى الطبيعي.", 920),
    ("service_resets", "Service Functions (Oil/EPB/SAS/DPF/Throttle)",
     "service", "reset",
     "وظائف الصيانة اليومية: تصفير الزيت، صيانة فرامل اليد، معايرة "
     "الاستيرنج، حرق فلتر الديزل، وتأقلم بوابة الهواء.", 930),
]

# (code, name, description, billing_mode, duration_days, usage_quota, price_egp, sort_order, is_featured, feature_codes)
INITIAL_PACKAGES: list[tuple[str, str, str, str, int, int, int, int, bool, list[str]]] = [
    (
        "pkg_starter",
        "Starter — Diagnostics + Basic Coding",
        "أصغر باقة: تشخيص OBD ذكي + F-series coding أساسي. مناسبة للورش الجديدة.",
        "time", 30, 0, 1500, 10, False,
        ["diagnostic_room", "full_system_scan", "service_resets",
         "f_series_coding", "cbs_battery_manager"],
    ),
    (
        "pkg_key_master",
        "Key Master — Key Programming Focus",
        "Bench key programming كامل لـ CAS3 / CAS3+ / FEM / BDC + ISN tools.",
        "usage", 0, 25, 4500, 20, True,
        ["key_programming", "key_programming_fem", "dme_isn_clone",
         "egs_isn_reset", "cbs_battery_manager"],
    ),
    (
        "pkg_lighting_coding",
        "Lighting & Coding — F+G Series",
        "Coding كامل لكل الشاسيهات الحديثة (F, G) + battery registration.",
        "time", 30, 0, 2500, 30, False,
        ["f_series_coding", "g_series_coding", "e_series_coding",
         "cbs_battery_manager", "diagnostic_room", "full_system_scan",
         "live_data_stream", "service_resets"],
    ),
    (
        "pkg_repair_specialist",
        "Repair Specialist — FRM / BDC",
        "FRM3 BDM recovery + FEM/BDC repair + Airbag crash clear. للورش المتخصصة.",
        "usage", 0, 10, 6000, 40, False,
        ["frm_repair", "bdc_repair", "acsm_crash_reset", "diagnostic_room"],
    ),
    (
        "pkg_full_suite",
        "Full Suite — Ultimate Access",
        "كل المميزات بدون أي قيود استخدام — للورش المحترفة والمراكز الكبيرة.",
        "time", 30, 0, 9500, 50, True,
        [
            "f_series_coding", "g_series_coding", "e_series_coding",
            "frm_repair", "bdc_repair",
            "key_programming", "key_programming_fem",
            "egs_isn_reset", "dme_isn_clone",
            "acsm_crash_reset", "cbs_battery_manager",
            "diagnostic_room", "full_system_scan", "live_data_stream",
            "service_resets",
        ],
    ),
]


def seed_catalog(*, Feature, SubscriptionPackage) -> None:
    """Populate Feature + SubscriptionPackage tables from the constants.

    Accepts the model classes as args so the same function works from a
    data-migration RunPython (apps.get_model) AND from a normal app
    setUp (direct import). update_or_create makes it idempotent.
    """
    feature_by_code = {}
    for code, name, cat, op_type, desc, sort in INITIAL_FEATURES:
        obj, _ = Feature.objects.update_or_create(
            code=code,
            defaults=dict(
                name=name, category=cat, default_operation_type=op_type,
                description=desc, sort_order=sort, is_active=True,
            ),
        )
        feature_by_code[code] = obj

    for (pkg_code, pkg_name, pkg_desc, billing, duration, quota,
         price, sort, featured, feature_codes) in INITIAL_PACKAGES:
        pkg, _ = SubscriptionPackage.objects.update_or_create(
            code=pkg_code,
            defaults=dict(
                name=pkg_name, description=pkg_desc,
                billing_mode=billing,
                default_duration_days=duration,
                default_usage_quota=quota,
                price_egp=price, sort_order=sort,
                is_featured=featured, is_active=True,
            ),
        )
        pkg.features.set([feature_by_code[c] for c in feature_codes
                          if c in feature_by_code])
