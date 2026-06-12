/* ============================================================
 * uds_did_catalog.js — UDS Mode 22 (Read Data By Identifier)
 * ============================================================
 *
 * Mode 22 (ISO 14229 service 0x22) reads "DIDs" (Data IDentifiers):
 * 2-byte addresses that map to arbitrary live data inside a specific
 * ECU. Unlike Mode 01 (which always talks to the engine ECU and uses
 * SAE-standard PIDs), Mode 22 targets a chosen module by setting the
 * ELM327 header (ATSH) to that module's request CAN ID. The module
 * replies on its response ID (request + 8 for 11-bit, +0x10000000 for
 * 29-bit per ISO 15765-4).
 *
 * Two layers of identifiers:
 *   • ISO 14229 reserved DIDs (F180-F1FF): every compliant ECU SHOULD
 *     answer these — VIN, ECU serial, software version, etc.
 *   • Manufacturer DIDs (0000-EFFF, F000-F1AF): proprietary. We ship a
 *     small set of well-documented Toyota/Hyundai/VAG DIDs that work
 *     on most cars from those makers.
 *
 * ECU addresses are 11-bit by default. Standard OBD-II diagnostic
 * addresses follow ISO 15765-4: physical request IDs 0x7E0-0x7EF
 * paired with response IDs +8 (0x7E8-0x7EF). Non-engine modules use
 * 0x7B0-0x7CF range historically.
 */

// ── ISO 14229 standard DIDs (manufacturer-agnostic) ────────────────────
const UDS_STANDARD_DIDS = {
    'F186': { label: 'active_diagnostic_session',   ascii: false },
    'F187': { label: 'manufacturer_spare_part_no',  ascii: true  },
    'F188': { label: 'manufacturer_ecu_sw_no',      ascii: true  },
    'F189': { label: 'manufacturer_ecu_sw_ver',     ascii: true  },
    'F18A': { label: 'system_supplier_id',          ascii: true  },
    'F18B': { label: 'ecu_manufacturing_date',      ascii: false },  // YYMMDD BCD
    'F18C': { label: 'ecu_serial_number',           ascii: true  },
    'F190': { label: 'vin',                         ascii: true  },
    'F191': { label: 'vehicle_manufacturer_ecu_hw', ascii: true  },
    'F192': { label: 'system_supplier_ecu_hw_no',   ascii: true  },
    'F193': { label: 'system_supplier_ecu_hw_ver',  ascii: true  },
    'F194': { label: 'system_supplier_ecu_sw_no',   ascii: true  },
    'F195': { label: 'system_supplier_ecu_sw_ver',  ascii: true  },
    'F197': { label: 'system_name_or_engine_type',  ascii: true  },
    'F198': { label: 'repair_shop_code',            ascii: true  },
    'F199': { label: 'programming_date',            ascii: false },
    'F19D': { label: 'ecu_installation_date',       ascii: false },
};

// ── Known module CAN addresses (11-bit) ─────────────────────────────────
// "request" is what we ATSH to. "response" is what the ECU sends back.
const UDS_MODULES = {
    engine:    { request: '7E0', response: '7E8', label: 'Engine ECU' },
    trans:     { request: '7E1', response: '7E9', label: 'Transmission TCM' },
    abs:       { request: '7E2', response: '7EA', label: 'ABS Module' },     // also '760/768' on some makes
    airbag:    { request: '7E3', response: '7EB', label: 'Airbag SRS' },     // also '780/788'
    bcm:       { request: '7E4', response: '7EC', label: 'Body Control BCM' },
    cluster:   { request: '7E5', response: '7ED', label: 'Instrument Cluster' },
    hvac:      { request: '7E6', response: '7EE', label: 'HVAC' },
    // Alternative legacy addresses some makes still use:
    abs_alt:   { request: '760', response: '768', label: 'ABS (legacy)' },
    airbag_alt:{ request: '780', response: '788', label: 'Airbag (legacy)' },
};

// ── Toyota — well-known BCM/ABS DIDs (publicly documented) ──────────────
const TOYOTA_DIDS = {
    bcm: {
        '1101': { label: 'battery_voltage_mv', unit: 'mV',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) : null },
        '1201': { label: 'door_status_bitmap', unit: '',
                  parse: b => b[0] },
        '1301': { label: 'ignition_status', unit: '',
                  parse: b => b[0] },
    },
    abs: {
        '0101': { label: 'wheel_speed_fl_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 100 : null },
        '0102': { label: 'wheel_speed_fr_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 100 : null },
        '0103': { label: 'wheel_speed_rl_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 100 : null },
        '0104': { label: 'wheel_speed_rr_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 100 : null },
    },
};

// ── Hyundai/Kia — well-known BCM/ABS DIDs ──────────────────────────────
const HYUNDAI_DIDS = {
    abs: {
        'C101': { label: 'wheel_speed_fl_kph', unit: 'km/h',
                  parse: b => b[0] },
        'C102': { label: 'wheel_speed_fr_kph', unit: 'km/h',
                  parse: b => b[0] },
        'C103': { label: 'wheel_speed_rl_kph', unit: 'km/h',
                  parse: b => b[0] },
        'C104': { label: 'wheel_speed_rr_kph', unit: 'km/h',
                  parse: b => b[0] },
        'C201': { label: 'brake_pedal_pct', unit: '%',
                  parse: b => b[0] * 100 / 255 },
    },
    bcm: {
        '0101': { label: 'battery_voltage_v', unit: 'V',
                  parse: b => b[0] * 0.1 },
    },
};

// ── VAG (VW/Audi/Skoda/SEAT) — UDS DIDs after 2014 ─────────────────────
const VAG_DIDS = {
    engine: {
        '0407': { label: 'rail_pressure_bar', unit: 'bar',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 0.1 : null },
        '0412': { label: 'boost_pressure_mbar', unit: 'mbar',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) : null },
    },
    abs: {
        '1809': { label: 'steering_angle_deg', unit: '°',
                  parse: b => b.length >= 2 ? (((b[0] << 8) + b[1]) - 32768) / 10 : null },
    },
};

/* ──────────────────────────────────────────────────────────────────────
 * IMPORTANT: The manufacturer DIDs below are best-effort entries derived
 * from publicly available community resources (forums, ELM327 docs, open-
 * source scan tool projects). Unlike SAE J1979 PIDs and ISO 14229 standard
 * DIDs (F186-F19D), manufacturer DIDs have NO public specification — they
 * vary per model, model year, and ECU supplier. ALWAYS validate scaling on
 * a known-good vehicle before trusting the returned values for diagnosis.
 *
 * The Mode 22 driver gracefully handles unknown DIDs: an ECU that doesn't
 * recognize a DID returns negative response 7F 22 31 ("requestOutOfRange"),
 * which we surface as { ok: false, nrc: '31' } rather than throwing.
 * ────────────────────────────────────────────────────────────────────── */

// ── BMW (F/G chassis, 2008+) ───────────────────────────────────────────
const BMW_DIDS = {
    engine: {  // DDE for diesel, DME for petrol
        '4101': { label: 'battery_voltage_v', unit: 'V',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 1000 : null },
        '4115': { label: 'oil_temp_c', unit: '°C',
                  parse: b => b.length >= 2 ? (((b[0] << 8) + b[1]) - 4000) / 10 : null },
        '4116': { label: 'oil_pressure_bar', unit: 'bar',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 0.01 : null },
    },
    abs: {  // DSC module
        '4505': { label: 'wheel_speed_fl_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 0.0625 : null },
        '4506': { label: 'wheel_speed_fr_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 0.0625 : null },
        '4507': { label: 'wheel_speed_rl_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 0.0625 : null },
        '4508': { label: 'wheel_speed_rr_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 0.0625 : null },
    },
};

// ── Mercedes-Benz (W204/W212/W213-era, post-2010) ──────────────────────
const MERCEDES_DIDS = {
    engine: {
        '0101': { label: 'battery_voltage_v', unit: 'V',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 0.1 : null },
        '010C': { label: 'fuel_rail_pressure_bar', unit: 'bar',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) : null },
    },
    abs: {  // ESP module
        '0301': { label: 'steering_angle_deg', unit: '°',
                  parse: b => b.length >= 2 ? (((b[0] << 8) + b[1]) - 32768) / 10 : null },
        '0501': { label: 'brake_pressure_bar', unit: 'bar',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 0.1 : null },
    },
};

// ── Nissan (Tiida/Sunny/Sentra/Altima/Qashqai post-2010) ───────────────
const NISSAN_DIDS = {
    engine: {
        '0101': { label: 'battery_voltage_v', unit: 'V',
                  parse: b => b[0] * 0.08 },
        '0107': { label: 'coolant_temp_c', unit: '°C',
                  parse: b => b[0] - 40 },
    },
    abs: {
        '0102': { label: 'wheel_speed_fl_kph', unit: 'km/h',
                  parse: b => b[0] },
        '0103': { label: 'wheel_speed_fr_kph', unit: 'km/h',
                  parse: b => b[0] },
        '0104': { label: 'wheel_speed_rl_kph', unit: 'km/h',
                  parse: b => b[0] },
        '0105': { label: 'wheel_speed_rr_kph', unit: 'km/h',
                  parse: b => b[0] },
    },
};

// ── Honda (Civic/Accord/CR-V, UDS-era 2014+) ───────────────────────────
const HONDA_DIDS = {
    engine: {
        '1101': { label: 'battery_voltage_v', unit: 'V',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 0.01 : null },
        '1110': { label: 'fuel_pressure_kpa', unit: 'kPa',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) : null },
    },
    abs: {  // VSA module
        '0201': { label: 'wheel_speed_fl_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 100 : null },
        '0202': { label: 'wheel_speed_fr_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 100 : null },
        '0203': { label: 'wheel_speed_rl_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 100 : null },
        '0204': { label: 'wheel_speed_rr_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 100 : null },
    },
};

// ── Ford (Focus/Fusion/EcoSport, PCM 2010+) ────────────────────────────
const FORD_DIDS = {
    engine: {  // PCM
        '1100': { label: 'battery_voltage_v', unit: 'V',
                  parse: b => b[0] * 0.0625 },
        '1130': { label: 'fuel_rail_pressure_kpa', unit: 'kPa',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 10 : null },
        '113F': { label: 'turbo_boost_kpa', unit: 'kPa',
                  parse: b => b[0] },
    },
    abs: {
        '8001': { label: 'wheel_speed_fl_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 100 : null },
        '8002': { label: 'wheel_speed_fr_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 100 : null },
        '8003': { label: 'wheel_speed_rl_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 100 : null },
        '8004': { label: 'wheel_speed_rr_kph', unit: 'km/h',
                  parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 100 : null },
    },
};

// ── Make → catalog lookup (use after VIN-decode tells us the make) ─────
const MANUFACTURER_DID_CATALOGS = {
    toyota:   TOYOTA_DIDS,
    lexus:    TOYOTA_DIDS,
    hyundai:  HYUNDAI_DIDS,
    kia:      HYUNDAI_DIDS,
    vw:       VAG_DIDS,
    volkswagen: VAG_DIDS,
    audi:     VAG_DIDS,
    skoda:    VAG_DIDS,
    seat:     VAG_DIDS,
    bmw:      BMW_DIDS,
    mini:     BMW_DIDS,
    mercedes: MERCEDES_DIDS,
    'mercedes-benz': MERCEDES_DIDS,
    nissan:   NISSAN_DIDS,
    infiniti: NISSAN_DIDS,
    honda:    HONDA_DIDS,
    acura:    HONDA_DIDS,
    ford:     FORD_DIDS,
    lincoln:  FORD_DIDS,
};

function getDIDsForMake(make) {
    if (!make) return null;
    const key = String(make).toLowerCase().trim();
    return MANUFACTURER_DID_CATALOGS[key] || null;
}

// Export for both module and global (drivers use global; tests inspect file).
if (typeof window !== 'undefined') {
    window.UDS_STANDARD_DIDS         = UDS_STANDARD_DIDS;
    window.UDS_MODULES               = UDS_MODULES;
    window.TOYOTA_DIDS               = TOYOTA_DIDS;
    window.HYUNDAI_DIDS              = HYUNDAI_DIDS;
    window.VAG_DIDS                  = VAG_DIDS;
    window.BMW_DIDS                  = BMW_DIDS;
    window.MERCEDES_DIDS             = MERCEDES_DIDS;
    window.NISSAN_DIDS               = NISSAN_DIDS;
    window.HONDA_DIDS                = HONDA_DIDS;
    window.FORD_DIDS                 = FORD_DIDS;
    window.MANUFACTURER_DID_CATALOGS = MANUFACTURER_DID_CATALOGS;
    window.getDIDsForMake            = getDIDsForMake;
}
