/* ============================================================
 * obd_bluetooth.js — Hardware-agnostic Web Bluetooth driver
 *                    for generic ELM327 BLE OBD-II dongles.
 * ============================================================
 *
 * Built for the Diagnostics Room (غرفة تشخيص الأعطال).
 *
 * Design goals:
 *   • Frictionless "click & connect" UX for mechanics — no UUID lookups.
 *   • Works with the wide zoo of cheap ELM327-BLE clones, which advertise
 *     under TI, Nordic, HM-10, JDY, and bare 16-bit serial profiles.
 *   • Never hardcode write/notify characteristic UUIDs — discover them
 *     dynamically by inspecting GATT `properties`.
 *   • Surface failures as plain-language remediation steps the user can act on.
 *
 * Browser support: Chromium-based browsers on HTTPS (Chrome, Edge, Opera,
 * Brave, Android Chrome). Safari + Firefox do NOT ship Web Bluetooth.
 *
 * Public API:
 *   const obd = new OBDBluetooth();
 *   obd.addEventListener('status', e => ui.setStatus(e.detail));
 *   obd.addEventListener('connected',  ...);
 *   obd.addEventListener('disconnected', ...);
 *   obd.addEventListener('live',       e => ui.render(e.detail));
 *   obd.addEventListener('error',      e => ui.showHelp(e.detail));
 *
 *   try { await obd.pairAndConnect(); await obd.initialize(); }
 *   catch (err) { ui.showHelp(err.userMessage || err.message); }
 */

// ── Known ELM327-BLE service UUIDs (cast a WIDE net) ────────────────────
// We pass these as `optionalServices` so the browser exposes them after
// pairing even though we did not filter by them.
const KNOWN_SERVICE_UUIDS = [
    // Cypress / TI CC2540 / CC2541 family — the most common ELM327 clones
    '0000ffe0-0000-1000-8000-00805f9b34fb',   // HM-10, JDY-08, generic "BLE-UART"
    '0000ffe1-0000-1000-8000-00805f9b34fb',
    '0000ffe5-0000-1000-8000-00805f9b34fb',   // HM-10 second TX endpoint
    '0000ffe9-0000-1000-8000-00805f9b34fb',

    // Standard 16-bit serial port profile UUIDs used by Chinese OBD clones
    '0000fff0-0000-1000-8000-00805f9b34fb',
    '0000fff1-0000-1000-8000-00805f9b34fb',
    '0000fff2-0000-1000-8000-00805f9b34fb',

    // Nordic UART Service (NUS) — used by nRF-based dongles (Vgate iCar Pro BLE 4.0)
    '6e400001-b5a3-f393-e0a9-e50e24dcca9e',
    '6e400002-b5a3-f393-e0a9-e50e24dcca9e',
    '6e400003-b5a3-f393-e0a9-e50e24dcca9e',

    // Microchip RN4870 / RN4871
    '49535343-fe7d-4ae5-8fa9-9fafd205e455',
    '49535343-1e4d-4bd9-ba61-23c647249616',
    '49535343-8841-43f4-a8d4-ecbe34729bb3',

    // Telit / generic SPP-over-BLE seen on Konnwei KW902 and ANCEL BD200
    '0000fee0-0000-1000-8000-00805f9b34fb',
    '0000fee1-0000-1000-8000-00805f9b34fb',
    'e7810a71-73ae-499d-8c15-faa9aef0c3f2',

    // Battery & device-info services — useful to surface, not for transport
    '0000180a-0000-1000-8000-00805f9b34fb',
    '0000180f-0000-1000-8000-00805f9b34fb',
];

// Services we should NOT probe for the UART (they are decorative / system).
const SERVICE_BLOCKLIST = new Set([
    '00001800-0000-1000-8000-00805f9b34fb',   // Generic Access
    '00001801-0000-1000-8000-00805f9b34fb',   // Generic Attribute
    '0000180a-0000-1000-8000-00805f9b34fb',   // Device Information
    '0000180f-0000-1000-8000-00805f9b34fb',   // Battery
]);

// ── requestDevice() pairing filters ─────────────────────────────────────
// We pass these as `filters: [...]` to navigator.bluetooth.requestDevice.
// Chrome treats each entry as OR — a device matching ANY entry appears in
// the picker, and EVERYTHING ELSE (TVs, headphones, watches, random MACs
// in the auto-repair shop) is hidden. This is the strict-UX path.
//
// We deliberately combine TWO families of filters because real-world ELM327
// clones split into two camps:
//   1. Dongles that advertise their service UUID in the primary adv packet
//      → caught by the `services:[...]` filters below.
//   2. Dongles that hide the service UUID in the scan response and only
//      put their NAME in the primary adv (very common on cheap clones)
//      → caught by the `namePrefix:` filters below.
const PAIRING_FILTERS = [
    // ── 1. Service-UUID filters (most reliable when the dongle cooperates)
    { services: ['0000ffe0-0000-1000-8000-00805f9b34fb'] },   // HM-10 / TI CC254x
    { services: ['0000fff0-0000-1000-8000-00805f9b34fb'] },   // Generic Chinese SPP-over-BLE
    { services: ['6e400001-b5a3-f393-e0a9-e50e24dcca9e'] },   // Nordic UART (Vgate iCar Pro BLE 4.0)
    { services: ['0000fee0-0000-1000-8000-00805f9b34fb'] },   // Telit / Konnwei / ANCEL
    { services: ['49535343-fe7d-4ae5-8fa9-9fafd205e455'] },   // Microchip RN487x

    // ── 2. Name-prefix filters (covers every well-known ELM327 brand) ──
    //    These names come from the BLE adv "Local Name" field. They match
    //    case-sensitively as a prefix, so "OBD" matches "OBDII", "OBD-II",
    //    "OBDBLE", "OBDLink CX", etc. in one shot.
    { namePrefix: 'OBD' },          // OBDII, OBD-II, OBDBLE, OBDLink, OBDFusion…
    { namePrefix: 'ELM' },          // ELM327, ELM-327
    { namePrefix: 'V-LINK' },       // Vgate V-LINK
    { namePrefix: 'VLINK' },        // Vgate VLINK
    { namePrefix: 'Vgate' },        // Vgate iCar 2/Pro/BLE
    { namePrefix: 'iCar' },         // iCar Pro BLE 4.0
    { namePrefix: 'Konnwei' },      // KW902, KW903
    { namePrefix: 'KW' },           // bare KW902 / KW903 name
    { namePrefix: 'ANCEL' },        // ANCEL BD200
    { namePrefix: 'Veepeak' },      // Veepeak BLE+
    { namePrefix: 'VEEPEAK' },
    { namePrefix: 'BlueDriver' },   // BlueDriver Pro
    { namePrefix: 'Carista' },
    { namePrefix: 'FIXD' },
    { namePrefix: 'TONWON' },
    { namePrefix: 'IOS-Vlink' },    // an alias seen on iCar clones
    { namePrefix: 'BT05' },         // bare HM-10 clones
    { namePrefix: 'JDY' },          // bare JDY-08 clones
];

// ── ELM327 protocol constants ───────────────────────────────────────────
const PROMPT_BYTE = '>';
const CR = '\r';
const DEFAULT_TIMEOUT_MS = 2500;

// Mode 01 PIDs we stream by default.
const PIDS = {
    '04': { label: 'engine_load',     unit: '%',    parse: b => b[0] * 100 / 255 },
    '05': { label: 'coolant_temp_c',  unit: '°C',   parse: b => b[0] - 40 },
    '06': { label: 'stft_b1',         unit: '%',    parse: b => (b[0] - 128) * 100 / 128 },
    '07': { label: 'ltft_b1',         unit: '%',    parse: b => (b[0] - 128) * 100 / 128 },
    '08': { label: 'stft_b2',         unit: '%',    parse: b => (b[0] - 128) * 100 / 128 },
    '09': { label: 'ltft_b2',         unit: '%',    parse: b => (b[0] - 128) * 100 / 128 },
    '0A': { label: 'fuel_pressure_kpa',unit: 'kPa', parse: b => b[0] * 3 },
    '0B': { label: 'map_kpa',         unit: 'kPa',  parse: b => b[0] },
    '0C': { label: 'rpm',             unit: 'rpm',  parse: b => ((b[0] << 8) + b[1]) / 4 },
    '0D': { label: 'speed_kph',       unit: 'km/h', parse: b => b[0] },
    '0E': { label: 'timing_advance_deg', unit: '°', parse: b => (b[0] / 2) - 64 },
    '0F': { label: 'intake_temp_c',   unit: '°C',   parse: b => b[0] - 40 },
    '10': { label: 'maf_gs',          unit: 'g/s',  parse: b => ((b[0] << 8) + b[1]) / 100 },
    '11': { label: 'throttle_pct',    unit: '%',    parse: b => b[0] * 100 / 255 },
    '14': { label: 'o2_b1s1_v',       unit: 'V',    parse: b => b[0] / 200 },
    '15': { label: 'o2_b1s2_v',       unit: 'V',    parse: b => b[0] / 200 },
    // Wideband O2 (Equivalence Ratio = Lambda)
    '24': { label: 'o2s1_lambda', unit: 'λ',
        parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 2 / 65535 : null },
    '25': { label: 'o2s2_lambda', unit: 'λ',
        parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 2 / 65535 : null },
    '26': { label: 'o2s3_lambda', unit: 'λ',
        parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 2 / 65535 : null },
    '27': { label: 'o2s4_lambda', unit: 'λ',
        parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 2 / 65535 : null },
    '2C': { label: 'egr_commanded_pct', unit: '%',
        parse: b => b.length >= 1 ? b[0] * 100 / 255 : null },
    '2D': { label: 'egr_error_pct',     unit: '%',
        parse: b => b.length >= 1 ? (b[0] - 128) * 100 / 128 : null },
    '2E': { label: 'evap_purge_cmd_pct', unit: '%',
        parse: b => b.length >= 1 ? b[0] * 100 / 255 : null },
    '2F': { label: 'fuel_level_pct',  unit: '%',    parse: b => b[0] * 100 / 255 },
    '32': { label: 'evap_vapor_press_pa', unit: 'Pa',
        parse: b => {
            if (b.length < 2) return null;
            const raw = (b[0] << 8) + b[1];
            const signed = raw >= 0x8000 ? raw - 0x10000 : raw;
            return signed * 0.25;
        }},
    '33': { label: 'baro_kpa',        unit: 'kPa',  parse: b => b[0] },
    '42': { label: 'control_voltage', unit: 'V',    parse: b => ((b[0] << 8) + b[1]) / 1000 },
    '45': { label: 'rel_throttle_pct', unit: '%',   parse: b => b[0] * 100 / 255 },
    '46': { label: 'ambient_temp_c',  unit: '°C',   parse: b => b[0] - 40 },
    '53': { label: 'evap_abs_vapor_kpa', unit: 'kPa',
        parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 0.005 : null },
    '54': { label: 'evap_vapor_press_kpa', unit: 'kPa',
        parse: b => {
            if (b.length < 2) return null;
            const raw = (b[0] << 8) + b[1];
            const signed = raw >= 0x8000 ? raw - 0x10000 : raw;
            return signed * 1.0;
        }},
    '5C': { label: 'oil_temp_c',      unit: '°C',   parse: b => b[0] - 40 },
    '5E': { label: 'fuel_rate_lh',    unit: 'L/h',  parse: b => ((b[0] << 8) + b[1]) * 0.05 },

    // ── Status / accumulators — non-numeric but vital for diagnosis ─────
    '03': { label: 'fuel_system_status', unit: '',
        parse: b => {
            const code = (b[0] || 0).toString(16).toUpperCase().padStart(2, '0');
            const map = { '01': 'open_low_temp', '02': 'closed_loop',
                          '04': 'open_load_or_decel', '08': 'open_failure',
                          '10': 'closed_with_fault' };
            return map[code] || code;
        }},
    '1C': { label: 'obd_standard', unit: '',
        parse: b => b[0] },                       // 1=OBDII CARB, 6=EOBD, etc.
    '1F': { label: 'run_time_s', unit: 's',
        parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
    '21': { label: 'dist_with_mil_km', unit: 'km',
        parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
    '30': { label: 'warmups_since_clear', unit: '',
        parse: b => b[0] },
    '31': { label: 'dist_since_clear_km', unit: 'km',
        parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
    '4D': { label: 'mil_on_min', unit: 'min',
        parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
    '4E': { label: 'time_since_clear_min', unit: 'min',
        parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },

    // ── Pedal & throttle actuator ──────────────────────────────────────
    '49': { label: 'accel_pedal_d_pct',  unit: '%', parse: b => b[0] * 100 / 255 },
    '4A': { label: 'accel_pedal_e_pct',  unit: '%', parse: b => b[0] * 100 / 255 },
    '4B': { label: 'accel_pedal_f_pct',  unit: '%', parse: b => b[0] * 100 / 255 },
    '4C': { label: 'cmd_throttle_pct',   unit: '%', parse: b => b[0] * 100 / 255 },

    // ── Fuel type & ethanol ────────────────────────────────────────────
    '51': { label: 'fuel_type', unit: '',
        parse: b => {
            const map = { 0:'unknown', 1:'gasoline', 2:'methanol', 3:'ethanol',
                          4:'diesel', 5:'lpg', 6:'cng', 7:'propane',
                          8:'electric', 9:'bi_gasoline_cng', 10:'bi_propane_cng' };
            return map[b[0]] !== undefined ? map[b[0]] : b[0];
        }},
    '52': { label: 'ethanol_fuel_pct', unit: '%', parse: b => b[0] * 100 / 255 },

    // ── Hybrid / EV ────────────────────────────────────────────────────
    '5B': { label: 'hybrid_battery_pct', unit: '%', parse: b => b[0] * 100 / 255 },
    '5D': { label: 'fuel_inj_timing_deg', unit: '°',
        parse: b => b.length >= 2 ? (((b[0] << 8) + b[1]) - 26880) / 128 : null },

    // ── Diesel-specific (Mode 01 PIDs 61-7F, subset) ───────────────────
    '61': { label: 'driver_demand_torque_pct', unit: '%', parse: b => b[0] - 125 },
    '62': { label: 'actual_engine_torque_pct', unit: '%', parse: b => b[0] - 125 },
    '63': { label: 'reference_torque_nm', unit: 'Nm',
        parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
    '67': { label: 'ect_2_c', unit: '°C',
        parse: b => b.length >= 2 ? b[1] - 40 : b[0] - 40 },
    '6B': { label: 'egr_temp_c', unit: '°C',
        parse: b => b.length >= 2 ? b[1] - 40 : b[0] - 40 },
    '78': { label: 'egt_bank1_c', unit: '°C',
        parse: b => b.length >= 3 ? (((b[1] << 8) + b[2]) / 10) - 40 : null },
    '79': { label: 'egt_bank2_c', unit: '°C',
        parse: b => b.length >= 3 ? (((b[1] << 8) + b[2]) / 10) - 40 : null },
    '7B': { label: 'dpf_delta_kpa', unit: 'kPa',
        parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 0.01 : null },
    '7C': { label: 'dpf_temp_c', unit: '°C',
        parse: b => b.length >= 3 ? (((b[1] << 8) + b[2]) / 10) - 40 : null },
};
const DEFAULT_POLL_PIDS = ['0C', '0D', '05', '11', '04', '42'];

// ── Human-readable error helper ─────────────────────────────────────────
// Maps a raw exception into a friendly remediation message the mechanic can act on.
function humanizeBluetoothError(err) {
    const msg = String(err && (err.message || err.name || err)) || '';
    const lower = msg.toLowerCase();

    if (lower.includes('user cancelled') || lower.includes('chooser cancelled')) {
        return {
            title: 'Pairing cancelled',
            hint:  'You closed the Bluetooth picker before selecting a dongle. ' +
                   'Click "Connect to OBD" again. If the picker is empty, your ' +
                   'specific dongle model may not advertise a recognised name — ' +
                   'click "Show all devices" to switch to relaxed pairing.',
            recoverable: true,
        };
    }
    if (lower.includes('bluetooth adapter not available') ||
        lower.includes('no bluetooth') ||
        lower.includes('globally disabled')) {
        return {
            title: 'Bluetooth is off on this computer',
            hint:  'Turn Bluetooth ON in your laptop\'s system settings (Windows: ' +
                   'Settings → Bluetooth & devices. macOS: System Settings → Bluetooth), ' +
                   'then refresh this page.',
            recoverable: true,
        };
    }
    if (lower.includes('not supported') || lower.includes('navigator.bluetooth')) {
        return {
            title: 'Browser does not support Web Bluetooth',
            hint:  'Open this page in Google Chrome or Microsoft Edge over HTTPS. ' +
                   'Safari and Firefox cannot connect to OBD-II dongles.',
            recoverable: false,
        };
    }
    if (lower.includes('gatt operation failed') ||
        lower.includes('gatt server is disconnected') ||
        lower.includes('connection failed') ||
        lower.includes('device is no longer in range')) {
        return {
            title: 'Could not establish a stable connection',
            hint:  'Make sure the dongle is NOT already paired to your phone or to ' +
                   'your laptop\'s OS Bluetooth menu — only ONE host can hold the BLE ' +
                   'channel at a time. Unpair / "Forget" the device from those menus, ' +
                   'unplug and re-plug the dongle from the OBD port, then try again.',
            recoverable: true,
        };
    }
    if (lower.includes('no services matching uuid found') ||
        lower.includes('no compatible') ||
        lower.includes('uart')) {
        return {
            title: 'Dongle paired, but it does not speak a known ELM327 profile',
            hint:  'This adapter\'s BLE service is not in our list yet. Capture the ' +
                   'service UUIDs from chrome://bluetooth-internals and send them to ' +
                   'support, or try a different ELM327 dongle (Vgate iCar Pro BLE 4.0 ' +
                   'and OBDLink CX are confirmed compatible).',
            recoverable: false,
        };
    }
    if (lower.includes('security') || lower.includes('permission')) {
        return {
            title: 'Browser blocked Bluetooth access',
            hint:  'This page must be served over HTTPS for Web Bluetooth to work. ' +
                   'If you self-host, install a TLS certificate (Let\'s Encrypt) and ' +
                   'reload over https://',
            recoverable: false,
        };
    }
    return {
        title: 'Bluetooth connection failed',
        hint:  `Unexpected error: ${msg}. Try unplugging the OBD dongle, turning the ` +
               'ignition off and back on (KOEO), and clicking Connect again.',
        recoverable: true,
    };
}

// Annotate an error with a user-facing message so callers can `throw` it
// and the UI layer can render `.userMessage` directly.
function withUserMessage(err) {
    if (!err) err = new Error('Unknown Bluetooth error');
    if (!err.userMessage) err.userMessage = humanizeBluetoothError(err);
    return err;
}

// ── Persistence keys for silent auto-reconnect ──────────────────────────
// After ANY successful pair we write the BLE device.id into localStorage.
// On the next visit we call navigator.bluetooth.getDevices(), match against
// the saved id, and reconnect WITHOUT showing a picker. This is the true
// "click & connect" UX for ghost dongles that hide their name + service
// UUIDs in the BLE scan response (e.g. IOS-Vlink ELM327 clones).
const LS_REMEMBERED_DEVICES = 'mousstec_obd_known_device_ids';

function _loadRememberedIds() {
    try {
        const raw = localStorage.getItem(LS_REMEMBERED_DEVICES);
        return raw ? JSON.parse(raw) : [];
    } catch (_) { return []; }
}
function _rememberDeviceId(id) {
    if (!id) return;
    const ids = _loadRememberedIds().filter(x => x !== id);
    ids.unshift(id);                              // MRU
    try { localStorage.setItem(LS_REMEMBERED_DEVICES, JSON.stringify(ids.slice(0, 5))); }
    catch (_) {}
}
function _forgetDeviceId(id) {
    if (!id) return;
    try {
        const ids = _loadRememberedIds().filter(x => x !== id);
        localStorage.setItem(LS_REMEMBERED_DEVICES, JSON.stringify(ids));
    } catch (_) {}
}

// ── Driver ──────────────────────────────────────────────────────────────
class OBDBluetooth extends EventTarget {
    constructor() {
        super();
        this.device = null;
        this.server = null;
        this.service = null;
        this.writeChar = null;
        this.notifyChar = null;
        this._writeWithoutResponse = false;   // chosen based on char.properties
        this._rxBuffer = '';
        this._pending = null;                  // { resolve, reject, timer, cmd }
        this._streaming = false;
        this._streamHandle = null;
    }

    get isConnected() {
        return !!(this.device && this.device.gatt && this.device.gatt.connected
                  && this.writeChar && this.notifyChar);
    }

    // ── SILENT auto-reconnect via navigator.bluetooth.getDevices() ──────
    // After ANY successful pair, the browser remembers the device for our
    // origin and exposes it via getDevices(). On subsequent visits we can
    // reconnect WITHOUT showing a Bluetooth picker — the picker only opens
    // the very first time per dongle. Returns the device info on success,
    // or null when no remembered dongle is reachable.
    //
    // Requires:
    //  • Chrome/Edge ≥ 85
    //  • The "Use the new permissions backend for Web Bluetooth" flag
    //    (default-on since Chrome 92)
    //  • At least one prior successful pair from this origin
    async tryAutoReconnect({ timeoutMs = 6000 } = {}) {
        if (!('bluetooth' in navigator) || typeof navigator.bluetooth.getDevices !== 'function') {
            return null;        // browser too old or feature off
        }

        let known;
        try { known = await navigator.bluetooth.getDevices(); }
        catch (_) { return null; }
        if (!known || !known.length) return null;

        // Prefer the most recently used remembered ID; fall back to first known.
        const remembered = _loadRememberedIds();
        const ordered = [
            ...remembered.map(id => known.find(d => d.id === id)).filter(Boolean),
            ...known.filter(d => !remembered.includes(d.id)),
        ];

        for (const candidate of ordered) {
            try {
                this._emit('status', { phase: 'auto-reconnect',
                    message: `إعادة اتصال صامت بـ ${candidate.name || 'الفيشة المحفوظة'}…` });

                this.device = candidate;
                this.device.addEventListener('gattserverdisconnected', () => {
                    this._streaming = false;
                    this.writeChar = null;
                    this.notifyChar = null;
                    this._emit('disconnected', { name: candidate.name });
                });

                // Race the connect attempt against a watchdog — getDevices()
                // returns adapters that may be physically out of range.
                await Promise.race([
                    this.connect(),
                    new Promise((_, rej) => setTimeout(
                        () => rej(new Error('auto-reconnect timeout')), timeoutMs)),
                ]);

                _rememberDeviceId(candidate.id);
                this._emit('auto_reconnected', {
                    name: candidate.name, id: candidate.id,
                });
                return { name: candidate.name || 'OBD-II', id: candidate.id };
            } catch (_) {
                // try the next remembered device — dongle may be unplugged
                this.device = null;
                this.writeChar = null;
                this.notifyChar = null;
            }
        }
        return null;
    }

    // List dongles the browser remembers for this origin (without connecting).
    async listRememberedDevices() {
        if (!('bluetooth' in navigator) || !navigator.bluetooth.getDevices) return [];
        try {
            const ds = await navigator.bluetooth.getDevices();
            return ds.map(d => ({ id: d.id, name: d.name || 'OBD-II' }));
        } catch (_) { return []; }
    }

    // Drop the saved MRU id for this device — used when user clicks "Forget".
    forgetCurrentDevice() {
        if (this.device) _forgetDeviceId(this.device.id);
    }

    // ── Convenience one-shot — pair + connect + locate UART. ────────────
    // Pass `{ relaxed: true }` to fall back to acceptAllDevices in the rare
    // case the user's specific dongle advertises neither a known name nor a
    // known service UUID. Default is the strict filtered picker.
    async pairAndConnect(opts = {}) {
        try {
            await this.requestDevice(opts);
            await this.connect();
            return { name: this.device.name || 'OBD-II', id: this.device.id };
        } catch (err) {
            const annotated = withUserMessage(err);
            this._emit('error', annotated.userMessage);
            throw annotated;
        }
    }

    // 1. Pairing — MUST be called from a user gesture (button click handler).
    //    By default uses STRICT filters so the Chrome picker only shows
    //    known ELM327 dongles (hides TVs, headphones, watches, random MACs).
    //    Pass `{ relaxed: true }` to fall back to acceptAllDevices for the
    //    rare edge-case dongle that advertises neither a known name nor a
    //    known service UUID.
    async requestDevice({ relaxed = false } = {}) {
        if (!('bluetooth' in navigator)) {
            throw new Error('Web Bluetooth not supported in this browser.');
        }
        if (navigator.bluetooth.getAvailability) {
            const available = await navigator.bluetooth.getAvailability();
            if (!available) {
                throw new Error('Bluetooth adapter not available on this machine.');
            }
        }

        this._emit('status', { phase: 'requesting',
            message: relaxed
                ? 'Opening Bluetooth picker (relaxed mode — all devices visible)…'
                : 'Opening Bluetooth picker — only ELM327 dongles will be listed…' });

        // STRICT mode (default): each entry in `filters` is an OR clause —
        // a device matching ANY entry appears, everything else is hidden.
        // `optionalServices` MUST still be populated with the full UUID list
        // so getPrimaryService() can reach UART services that weren't part
        // of the filter clause that matched.
        //
        // RELAXED mode: opt-in fallback that mirrors the old behaviour
        // (acceptAllDevices) — only use it when the strict picker truly
        // shows nothing for a given dongle model.
        const config = relaxed
            ? { acceptAllDevices: true, optionalServices: KNOWN_SERVICE_UUIDS }
            : { filters: PAIRING_FILTERS, optionalServices: KNOWN_SERVICE_UUIDS };

        this.device = await navigator.bluetooth.requestDevice(config);

        this.device.addEventListener('gattserverdisconnected', () => {
            this._streaming = false;
            this.writeChar = null;
            this.notifyChar = null;
            this._emit('disconnected', { name: this.device && this.device.name });
        });

        return { name: this.device.name || 'OBD-II', id: this.device.id };
    }

    // 2. Connect to GATT and DISCOVER the UART characteristics by walking
    //    every primary service and inspecting char properties.
    async connect() {
        if (!this.device) throw new Error('Call requestDevice() first.');

        this._emit('status', { phase: 'connecting',
            message: 'Establishing GATT link to the dongle…' });

        // Retry GATT connect — BLE link-layer is flaky on first attempt on Windows.
        this.server = await this._connectGattWithRetry(this.device, 3);

        this._emit('status', { phase: 'discovering',
            message: 'Discovering services on the adapter…' });

        const services = await this.server.getPrimaryServices().catch(() => []);
        if (!services.length) {
            throw new Error('Connected, but the dongle exposed no GATT services.');
        }

        const pair = await this._discoverUART(services);
        if (!pair) {
            throw new Error(
                'No compatible UART found. Connected to the dongle but could not ' +
                'locate a write + notify characteristic pair.',
            );
        }

        this.service     = pair.service;
        this.writeChar   = pair.writeChar;
        this.notifyChar  = pair.notifyChar;
        this._writeWithoutResponse =
            pair.writeChar.properties.writeWithoutResponse &&
            !pair.writeChar.properties.write;

        await this.notifyChar.startNotifications();
        this.notifyChar.addEventListener(
            'characteristicvaluechanged', this._onNotify.bind(this),
        );

        // Remember this dongle so subsequent visits skip the picker.
        if (this.device && this.device.id) _rememberDeviceId(this.device.id);

        this._emit('connected', {
            service:   this.service.uuid,
            writeChar: this.writeChar.uuid,
            notifyChar: this.notifyChar.uuid,
            writeWithoutResponse: this._writeWithoutResponse,
            deviceId:  this.device && this.device.id,
            deviceName: this.device && (this.device.name || 'OBD-II'),
        });

        return { service: this.service.uuid };
    }

    // Walk every service and pick the FIRST (write|writeWithoutResponse) char
    // that has a companion (notify|indicate) char in the same service.
    // Some dongles publish both on the SAME characteristic (HM-10 0xFFE1) —
    // we handle that case too.
    async _discoverUART(services) {
        for (const svc of services) {
            if (SERVICE_BLOCKLIST.has(svc.uuid)) continue;

            let chars;
            try { chars = await svc.getCharacteristics(); }
            catch (_) { continue; }

            const writeable = chars.filter(c =>
                c.properties.write || c.properties.writeWithoutResponse);
            const listenable = chars.filter(c =>
                c.properties.notify || c.properties.indicate);

            if (!writeable.length || !listenable.length) continue;

            // Prefer a notify char distinct from the write char (Nordic UART style).
            for (const w of writeable) {
                const n = listenable.find(c => c.uuid !== w.uuid) || listenable[0];
                if (n) return { service: svc, writeChar: w, notifyChar: n };
            }
        }
        return null;
    }

    async _connectGattWithRetry(device, attempts) {
        let lastErr;
        for (let i = 0; i < attempts; i++) {
            try { return await device.gatt.connect(); }
            catch (e) {
                lastErr = e;
                await new Promise(r => setTimeout(r, 350 * (i + 1)));
            }
        }
        throw lastErr;
    }

    // 3. ELM327 chip init — exhaustive sweep across EVERY OBD-II bus protocol.
    //    Kept in lock-step with obd_wifi.js so a mechanic gets identical
    //    coverage whether they're using a Bluetooth or Wi-Fi dongle.
    async initialize() {
        const seq = ['ATD', 'ATZ', 'ATE0', 'ATL0', 'ATH0', 'ATS0', 'ATAT1'];
        const out = {};
        for (const cmd of seq) {
            try { out[cmd] = await this._sendCommand(cmd, DEFAULT_TIMEOUT_MS); }
            catch (e) { out[cmd] = `<err:${e.message}>`; }
        }

        // ── Protocol negotiation — every ELM327 protocol family ─────────
        // Covers all 11 ELM327 protocol codes: 1-9 standard OBD-II buses,
        // A = auto-search, B = SAE J1939 (heavy-duty CAN, 250 kbps).
        const protocols = [
            { code: 'A', label: 'auto-search (ATSPA)',     probeMs: 8000  },
            { code: '6', label: 'CAN 11-bit / 500 kbps',   probeMs: 4000  },
            { code: '7', label: 'CAN 29-bit / 500 kbps',   probeMs: 4000  },
            { code: '8', label: 'CAN 11-bit / 250 kbps',   probeMs: 4000  },
            { code: '9', label: 'CAN 29-bit / 250 kbps',   probeMs: 4000  },
            { code: 'B', label: 'SAE J1939 (heavy-duty CAN)', probeMs: 5000 },
            { code: '3', label: 'ISO 9141-2 (K-Line)',     probeMs: 12000 },
            { code: '5', label: 'KWP2000 fast init',       probeMs: 7000  },
            { code: '4', label: 'KWP2000 5-baud init',     probeMs: 12000 },
            { code: '1', label: 'SAE J1850 PWM',           probeMs: 5000  },
            { code: '2', label: 'SAE J1850 VPW',           probeMs: 5000  },
        ];

        // ── Protocol memory — if we've seen this dongle/VIN before, try
        // the previously-successful protocol FIRST. Saves ~30s on average.
        const dongleId = (this.device && this.device.id) || '';
        let sweepStart = Date.now();
        let usedMemory = false;
        let probeOrder = protocols;
        if (window.ProtocolMemoryClient && dongleId) {
            const hit = await window.ProtocolMemoryClient.lookup({ dongleId });
            if (hit && hit.protocol_code) {
                probeOrder = window.ProtocolMemoryClient.reorder(protocols, hit.protocol_code);
                usedMemory = true;
                this._emit('protocol_memory_hit', {
                    protocol_code: hit.protocol_code,
                    protocol_label: hit.protocol_label,
                    hit_count: hit.hit_count,
                });
            }
        }

        let chosen = null;
        const probes = [];
        for (const p of probeOrder) {
            try {
                await this._sendCommand('ATSP' + p.code, 1500);
                this._emit('protocol_probe', { protocol: p.label, phase: 'trying' });
                const probe = await this._sendCommand('0100', p.probeMs);
                const clean = (probe || '').replace(/\s/g, '').toUpperCase();
                const stripped = clean.replace(/^.*BUSINIT:OK/, '');
                const explicitFail = /NODATA|UNABLETOCONNECT|SEARCHING\.\.\.|STOPPED|BUSERROR|BUSINIT:ERROR|CANERROR|DATAERROR/
                    .test(stripped) && !/4100[0-9A-F]{2,}/.test(stripped);
                const ok = !explicitFail && /4100[0-9A-F]{2,}/.test(stripped);
                probes.push({ protocol: p.label, code: p.code, response: probe, ok });
                this._emit('protocol_probe', { protocol: p.label, response: probe, ok });
                if (ok) {
                    chosen = p;
                    this._lastProtocolCode  = p.code;
                    this._lastProtocolLabel = p.label;
                    out['protocol']    = p.label;
                    out['protocol_id'] = p.code;
                    out['0100']        = probe;
                    break;
                }
            } catch (e) {
                probes.push({ protocol: p.label, code: p.code, response: `<err:${e.message}>`, ok: false });
                this._emit('protocol_probe', { protocol: p.label, error: e.message, ok: false });
            }
        }
        out['_probes'] = probes;

        if (!chosen) {
            const lines = probes.map(p =>
                `  • ${p.protocol}: ${p.ok ? 'OK' : (p.response || 'no response').slice(0, 60)}`).join('\n');
            throw new Error(
                'الدونجل بيكلم الموبايل بس مش عارف يكلم ECU العربية. ' +
                'جرّبت كل بروتوكولات OBD-II وكلهم فشلوا.\n\n' +
                '1. مفتاح السيارة لازم يبقى على ON (مش لازم تشغّل المحرك).\n' +
                '2. الفيشة بايبس على فيشة OBD صح؟\n' +
                '3. الفيشة فيها مشكلة hardware؟\n\n' +
                'نتايج التجارب:\n' + lines,
            );
        }

        // ── Persist the successful protocol so the next visit skips the sweep.
        // Estimate seconds saved as the total probe time of every protocol that
        // would have run BEFORE this one in the natural order.
        if (window.ProtocolMemoryClient && dongleId) {
            const naturalIdx = protocols.findIndex(p => p.code === chosen.code);
            const wouldHaveTaken = protocols
                .slice(0, naturalIdx)
                .reduce((s, p) => s + (p.probeMs || 0), 0) / 1000;
            window.ProtocolMemoryClient.save({
                dongleId,
                code: chosen.code,
                label: chosen.label,
                sweepSecondsSaved: wouldHaveTaken,
            });
        }

        out['_used_memory'] = usedMemory;
        out['_sweep_ms']    = Date.now() - sweepStart;
        this._emit('initialized', out);
        return out;
    }

    // 4. Streaming PIDs.
    async streamLiveData({ pids = DEFAULT_POLL_PIDS, intervalMs = 600 } = {}) {
        if (this._streaming) return;
        this._streaming = true;

        const tick = async () => {
            if (!this._streaming || !this.isConnected) return;
            const snapshot = { _at: Date.now() };
            for (const pid of pids) {
                try {
                    const raw = await this._sendCommand(`01${pid}`, DEFAULT_TIMEOUT_MS);
                    const parsed = this._parsePIDResponse(pid, raw);
                    if (parsed !== null) snapshot[PIDS[pid].label] = parsed;
                } catch (_) { /* tolerate single-PID dropouts */ }
            }
            this._emit('live', snapshot);
            this._streamHandle = setTimeout(tick, intervalMs);
        };
        tick();
    }

    stopStream() {
        this._streaming = false;
        if (this._streamHandle) clearTimeout(this._streamHandle);
        this._streamHandle = null;
    }

    // 5. DTC read — pulls STORED (Mode 03) + PENDING (Mode 07) + PERMANENT
    //    (Mode 0A) so we catch faults that haven't been confirmed yet, plus
    //    permanent codes that survive a Mode 04 clear until the ECU verifies
    //    the underlying defect is gone.
    async readDTCs() {
        const buckets = [
            { mode: '03', tag: 'stored',    header: '43' },
            { mode: '07', tag: 'pending',   header: '47' },
            { mode: '0A', tag: 'permanent', header: '4A' },
        ];
        const all = [];
        const raws = {};
        for (const b of buckets) {
            try {
                const raw = await this._sendCommand(b.mode, 4000);
                raws[b.mode] = raw;
                const codes = this._parseDTCResponseWithHeader(raw, b.header);
                for (const c of codes) all.push({ code: c, type: b.tag });
            } catch (e) { raws[b.mode] = `<err:${e.message}>`; }
        }
        this._emit('dtcs', {
            codes: all.map(x => x.code),
            byType: all,
            raw: raws,
        });
        return all;
    }

    _parseDTCResponseWithHeader(raw, header) {
        if (!raw || raw.includes('NO DATA')) return [];
        const stripped = raw.replace(/\s+/g, '').toUpperCase();
        const idx = stripped.indexOf(header);
        if (idx < 0) return [];
        let cursor = stripped.slice(idx + 2);
        if (cursor.length % 4 !== 0) cursor = cursor.slice(2);
        const codes = [];
        for (let i = 0; i + 4 <= cursor.length; i += 4) {
            const hi = parseInt(cursor.slice(i, i + 2), 16);
            const lo = parseInt(cursor.slice(i + 2, i + 4), 16);
            if (hi === 0 && lo === 0) continue;
            codes.push(this._decodeDTCNibbles(hi, lo));
        }
        return codes;
    }

    // 6. Mode 04 — clear DTCs (DESTRUCTIVE; UI must confirm first).
    async clearDTCs() {
        const raw = await this._sendCommand('04', 4000);
        this._emit('dtcs_cleared', { raw });
        return raw;
    }

    // 7. Mode 09 PID 02 — vehicle VIN.
    async readVIN() {
        const raw = await this._sendCommand('0902', 5000);
        const vin = this._parseVINResponse(raw);
        if (vin) {
            this._emit('vin', { vin });
            // Link the VIN to the protocol memory row we wrote at init.
            // (The row was keyed only on dongle_id then; now we add the VIN
            // so any future driver that knows the VIN — even on a different
            // dongle — hits the same record.)
            const dongleId = (this.device && this.device.id) || '';
            if (window.ProtocolMemoryClient && this._lastProtocolCode && (vin || dongleId)) {
                window.ProtocolMemoryClient.save({
                    vin, dongleId,
                    code: this._lastProtocolCode,
                    label: this._lastProtocolLabel || '',
                });
            }
        }
        return vin;
    }

    async disconnect() {
        this.stopStream();
        try {
            if (this.notifyChar) await this.notifyChar.stopNotifications().catch(() => {});
        } catch (_) {}
        if (this.device && this.device.gatt && this.device.gatt.connected) {
            this.device.gatt.disconnect();
        }
        this.writeChar = null;
        this.notifyChar = null;
        this.service = null;
        this.server = null;
    }

    // ── internal: command transport ──────────────────────────────────────
    // Commands are serialized through a promise chain so the live-data
    // stream cannot collide with a user-triggered Mode 03 / Mode 04 request.
    async _sendCommand(cmd, timeoutMs) {
        const queued = (this._cmdChain || Promise.resolve())
            .catch(() => {})
            .then(() => this._sendCommandRaw(cmd, timeoutMs));
        this._cmdChain = queued.catch(() => {});
        return queued;
    }

    async _sendCommandRaw(cmd, timeoutMs) {
        if (!this.writeChar) throw new Error('Not connected.');

        const fullCmd = cmd + CR;
        const bytes = new TextEncoder().encode(fullCmd);
        this._rxBuffer = '';

        const reply = new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                this._pending = null;
                reject(new Error(`Timeout waiting for ELM reply to "${cmd}".`));
            }, timeoutMs);
            this._pending = { resolve, reject, timer, cmd };
        });

        if (this._writeWithoutResponse && this.writeChar.writeValueWithoutResponse) {
            await this.writeChar.writeValueWithoutResponse(bytes);
        } else if (this.writeChar.writeValueWithResponse) {
            await this.writeChar.writeValueWithResponse(bytes);
        } else {
            await this.writeChar.writeValue(bytes);   // deprecated fallback
        }

        const response = await reply;
        return response.replace(cmd, '').trim();
    }

    _onNotify(event) {
        const txt = new TextDecoder().decode(event.target.value);
        this._rxBuffer += txt;
        if (this._rxBuffer.includes(PROMPT_BYTE)) {
            const payload = this._rxBuffer
                .split(PROMPT_BYTE)[0]
                .replace(/\r/g, ' ')
                .trim();
            this._rxBuffer = '';
            if (this._pending) {
                const { resolve, timer } = this._pending;
                this._pending = null;
                clearTimeout(timer);
                resolve(payload);
            }
        }
    }

    // ── internal: ELM-response parsing ───────────────────────────────────
    _parsePIDResponse(pid, raw) {
        if (!raw || raw.includes('NO DATA') || raw.includes('?')) return null;
        const stripped = raw.replace(/\s+/g, '').toUpperCase();
        const header = '41' + pid;
        const idx = stripped.indexOf(header);
        if (idx < 0) return null;
        const body = stripped.slice(idx + header.length);
        const bytes = [];
        for (let i = 0; i < body.length; i += 2) {
            const b = parseInt(body.slice(i, i + 2), 16);
            if (!Number.isFinite(b)) break;
            bytes.push(b);
        }
        try { return PIDS[pid].parse(bytes); } catch { return null; }
    }

    _parseDTCResponse(raw) {
        if (!raw || raw.includes('NO DATA')) return [];
        const stripped = raw.replace(/\s+/g, '').toUpperCase();
        const idx = stripped.indexOf('43');
        if (idx < 0) return [];
        let cursor = stripped.slice(idx + 2);
        if (cursor.length % 4 !== 0) cursor = cursor.slice(2);
        const codes = [];
        for (let i = 0; i + 4 <= cursor.length; i += 4) {
            const hi = parseInt(cursor.slice(i, i + 2), 16);
            const lo = parseInt(cursor.slice(i + 2, i + 4), 16);
            if (hi === 0 && lo === 0) continue;
            codes.push(this._decodeDTCNibbles(hi, lo));
        }
        return codes;
    }

    _decodeDTCNibbles(hi, lo) {
        const prefix = ['P', 'C', 'B', 'U'][(hi >> 6) & 0b11];
        const d1 = (hi >> 4) & 0b11;
        const d2 = hi & 0b1111;
        const d3 = (lo >> 4) & 0b1111;
        const d4 = lo & 0b1111;
        return `${prefix}${d1}${d2.toString(16).toUpperCase()}` +
               `${d3.toString(16).toUpperCase()}${d4.toString(16).toUpperCase()}`;
    }

    _parseVINResponse(raw) {
        if (!raw || raw.includes('NO DATA') || raw.includes('?')) return null;
        const stripped = raw
            .replace(/\d+\s*:/g, '')
            .replace(/\s+/g, '')
            .toUpperCase();
        if (stripped.indexOf('4902') < 0) return null;
        // Strip ALL frame headers — multi-frame KWP responses repeat
        // "4902XX" between data segments, leaking 0x49 ('I') into the VIN.
        const body = stripped.replace(/4902[0-9A-F]{2}/g, '');
        let vin = '';
        for (let i = 0; i + 2 <= body.length && vin.length < 17; i += 2) {
            const b = parseInt(body.slice(i, i + 2), 16);
            if (!Number.isFinite(b) || b === 0x00) continue;
            if (b < 0x20 || b > 0x7E) continue;
            vin += String.fromCharCode(b);
        }
        return vin.length === 17 ? vin : null;
    }

    async readFreezeFrame() {
        // Most useful PIDs at the moment a DTC was set:
        const ff_pids = ['02', '03', '04', '05', '0C', '0D', '0E', '0F',
                         '10', '11', '14', '1C', '21'];
        const out = { _at: Date.now(), frame: 0 };
        for (const pid of ff_pids) {
            try {
                const raw = await this._sendCommand(`02${pid}00`, 3000);
                if (!raw || /NO\s*DATA|UNABLE|\?/i.test(raw)) continue;
                // Mode 02 response header: "42<PID><frame><bytes>"
                const stripped = raw.replace(/\s+/g, '').toUpperCase();
                const headerIdx = stripped.indexOf('42' + pid);
                if (headerIdx < 0) continue;
                const body = stripped.slice(headerIdx + 4 + 2);   // skip 42PID + frame nibble
                const bytes = [];
                for (let i = 0; i < body.length; i += 2) {
                    const b = parseInt(body.slice(i, i + 2), 16);
                    if (!Number.isFinite(b)) break;
                    bytes.push(b);
                }
                // PID 02 in Mode 02 = the DTC that triggered the freeze
                if (pid === '02' && bytes.length >= 2) {
                    out.trigger_dtc = this._decodeDTCNibbles(bytes[0], bytes[1]);
                    continue;
                }
                const def = PIDS[pid];
                if (def) {
                    try {
                        const parsed = def.parse(bytes);
                        if (Number.isFinite(parsed)) out[def.label] = parsed;
                    } catch (_) {}
                }
            } catch (_) { /* per-PID failure non-fatal */ }
        }
        this._emit('freeze_frame', out);
        return out;
    }

    async readReadinessMonitors() {
        const raw = await this._sendCommand('0101', 3000);
        if (!raw || /NO\s*DATA|UNABLE|\?/i.test(raw)) {
            throw new Error('الـ ECU مردش على Mode 01 PID 01.');
        }
        const stripped = raw.replace(/\s+/g, '').toUpperCase();
        const idx = stripped.indexOf('4101');
        if (idx < 0) throw new Error('رد غير صالح من الـ ECU.');
        const body = stripped.slice(idx + 4);
        const A = parseInt(body.slice(0, 2), 16);
        const B = parseInt(body.slice(2, 4), 16);
        const C = parseInt(body.slice(4, 6), 16);
        const D = parseInt(body.slice(6, 8), 16);

        const mil = !!(A & 0x80);                        // Check Engine on?
        const dtcCount = A & 0x7F;
        const isDiesel = !!(B & 0x08);

        // "supported" bit = ECU has this monitor; "complete" bit = test passed.
        // For each monitor we get { supported, complete }. complete = ready.
        const continuous = {
            misfire:        { supported: !!(B & 0x01), complete: !(B & 0x10) },
            fuel_system:    { supported: !!(B & 0x02), complete: !(B & 0x20) },
            components:     { supported: !!(B & 0x04), complete: !(B & 0x40) },
        };

        const onceper_gas = {
            catalyst:           { supported: !!(C & 0x01), complete: !(D & 0x01) },
            heated_catalyst:    { supported: !!(C & 0x02), complete: !(D & 0x02) },
            evap_system:        { supported: !!(C & 0x04), complete: !(D & 0x04) },
            secondary_air:      { supported: !!(C & 0x08), complete: !(D & 0x08) },
            ac_refrigerant:     { supported: !!(C & 0x10), complete: !(D & 0x10) },
            o2_sensor:          { supported: !!(C & 0x20), complete: !(D & 0x20) },
            o2_sensor_heater:   { supported: !!(C & 0x40), complete: !(D & 0x40) },
            egr_system:         { supported: !!(C & 0x80), complete: !(D & 0x80) },
        };

        const onceper_diesel = {
            nmhc_catalyst:      { supported: !!(C & 0x01), complete: !(D & 0x01) },
            nox_aftertreatment: { supported: !!(C & 0x02), complete: !(D & 0x02) },
            boost_pressure:     { supported: !!(C & 0x08), complete: !(D & 0x08) },
            exhaust_gas_sensor: { supported: !!(C & 0x20), complete: !(D & 0x20) },
            pm_filter:          { supported: !!(C & 0x40), complete: !(D & 0x40) },
            egr_vvt:            { supported: !!(C & 0x80), complete: !(D & 0x80) },
        };

        const monitors = { ...continuous, ...(isDiesel ? onceper_diesel : onceper_gas) };
        // IM readiness verdict: all SUPPORTED monitors must be COMPLETE.
        const incomplete = Object.entries(monitors)
            .filter(([_, m]) => m.supported && !m.complete)
            .map(([k]) => k);
        const imReady = incomplete.length === 0;

        const result = { mil, dtcCount, isDiesel, monitors, incomplete, imReady, raw };
        this._emit('readiness', result);
        return result;
    }

    async readVehicleInfo() {
        const out = {};
        const calls = [
            { pid: '04', label: 'cal_id',   ascii: true  },
            { pid: '06', label: 'cvn',      ascii: false },
            { pid: '0A', label: 'ecu_name', ascii: true  },
            { pid: '08', label: 'ipt',      ascii: false },
        ];
        for (const c of calls) {
            try {
                const raw = await this._sendCommand(`09${c.pid}`, 5000);
                if (!raw || /NO\s*DATA|\?/i.test(raw)) continue;
                const data = this._extractMode09Payload(raw, c.pid.toUpperCase());
                if (!data) continue;
                if (c.ascii) {
                    let s = '';
                    for (let i = 0; i + 2 <= data.length; i += 2) {
                        const b = parseInt(data.slice(i, i + 2), 16);
                        if (b >= 0x20 && b <= 0x7E) s += String.fromCharCode(b);
                    }
                    out[c.label] = s.trim() || null;
                } else {
                    out[c.label] = data.match(/.{1,2}/g)?.join(' ').trim() || null;
                }
            } catch (_) { /* per-PID failure non-fatal */ }
        }
        this._emit('vehicle_info', out);
        return out;
    }

    _extractMode09Payload(raw, pidHex) {
        const stripped = raw
            .replace(/\d+\s*:/g, '')        // strip "0:" line numbers
            .replace(/\s+/g, '')             // strip whitespace
            .toUpperCase();
        const header = '49' + pidHex;       // e.g. "4902" for VIN, "4906" for CVN
        if (stripped.indexOf(header) < 0) return null;
        // Strip every "49 PID NN" header (4 hex of "49PID" + 2 hex of message-count nibble).
        const headerRegex = new RegExp(header + '[0-9A-F]{2}', 'g');
        return stripped.replace(headerRegex, '');
    }

    async readMisfireCounts({ cylinders = 8 } = {}) {
        const result = [];
        try { await this._sendCommand('ATH1', 1500); } catch (_) {}
        try {
            for (let cyl = 1; cyl <= cylinders; cyl++) {
                const tid = (0xA0 + cyl).toString(16).toUpperCase().padStart(2, '0');
                let raw;
                try { raw = await this._sendCommand('06' + tid, 3000); }
                catch (_) { continue; }
                if (!raw || raw.includes('NO DATA')) continue;
                // Response format (CAN, single-frame, headers ON):
                //   "7E8 06 46 A1 0B 24 00 28 00 32" → bytes after "46 A1":
                //   [TID, UAS, hi(test), lo(test), hi(min), lo(min), hi(max), lo(max)]
                // We only need the test value (current count).
                const stripped = raw.replace(/\s+/g, '').toUpperCase();
                const m = stripped.match(new RegExp('46' + tid + '([0-9A-F]{2})([0-9A-F]{4})'));
                if (!m) continue;
                const count = parseInt(m[2], 16);
                result.push({ cylinder: cyl, count });
            }
        } finally {
            try { await this._sendCommand('ATH0', 1500); } catch (_) {}
        }
        this._emit('misfire_counts', { cylinders: result });
        return result;
    }

    // ── Mode 05 — O2 Sensor Monitoring Test Results (legacy non-CAN) ────
    // Only K-Line / J1850 ECUs answer; CAN cars use Mode 06 instead.
    async readO2MonitoringResults({ sensors = 4 } = {}) {
        const out = { _at: Date.now(), supported: false, results: [] };
        const tids = ['01', '05', '06', '07', '08'];
        for (let s = 1; s <= sensors; s++) {
            const sensorIdx = s.toString(16).toUpperCase().padStart(2, '0');
            for (const tid of tids) {
                try {
                    const raw = await this._sendCommand(`05${tid}${sensorIdx}`, 3000);
                    if (!raw || /NO\s*DATA|UNABLE|\?/i.test(raw)) continue;
                    out.supported = true;
                    out.results.push({ sensor: s, tid, raw });
                } catch (_) { /* per-TID failure non-fatal */ }
            }
        }
        this._emit('mode05_o2', out);
        return out;
    }

    // ── Mode 08 — Request Control of On-Board System/Test/Component ─────
    // Bidirectional control — DESTRUCTIVE. UI must confirm before call.
    async requestComponentTest(tid, dataBytes = []) {
        if (!/^[0-9A-Fa-f]{2}$/.test(tid)) {
            throw new Error('Mode 08 TID must be 2 hex chars (e.g. "01").');
        }
        const payload = '08' + tid.toUpperCase() +
            dataBytes.map(b => b.toString(16).toUpperCase().padStart(2, '0')).join('');
        const raw = await this._sendCommand(payload, 5000);
        const ok  = !/NO\s*DATA|UNABLE|\?|7F\s*08/i.test(raw);
        const result = { tid, raw, ok };
        this._emit('mode08_response', result);
        return result;
    }

    // ── Mode 22 — UDS Read Data By Identifier (ISO 14229) ───────────────
    // Talks to non-engine modules (ABS, Airbag, BCM, TCM, Cluster, HVAC)
    // by switching ELM headers. See obd_wifi.js for the full doc.
    async readDataByIdentifier(did, { reqHeader = null, respFilter = null } = {}) {
        if (!/^[0-9A-Fa-f]{4}$/.test(did)) {
            throw new Error('UDS DID must be 4 hex chars (e.g. "F190" for VIN).');
        }
        const out = { did: did.toUpperCase(), raw: null, ok: false, data: null, nrc: null };
        try {
            if (reqHeader)  { try { await this._sendCommand('ATSH' + reqHeader, 1500); } catch (_) {} }
            if (respFilter) { try { await this._sendCommand('ATCRA' + respFilter, 1500); } catch (_) {} }

            const raw = await this._sendCommand('22' + did.toUpperCase(), 4000);
            out.raw = raw;
            const stripped = (raw || '').replace(/\s+/g, '').toUpperCase();
            const nrcMatch = stripped.match(/7F22([0-9A-F]{2})/);
            if (nrcMatch) { out.nrc = nrcMatch[1]; return out; }
            const m = stripped.match(new RegExp('62' + did.toUpperCase() + '([0-9A-F]+)'));
            if (!m) return out;
            out.data = m[1];
            out.ok = true;
        } finally {
            try { await this._sendCommand('ATCRA', 1500); } catch (_) {}
            try { await this._sendCommand('ATSH7E0', 1500); } catch (_) {}
        }
        return out;
    }

    async readModuleStandardInfo(moduleKey) {
        const modules = (typeof UDS_MODULES !== 'undefined') ? UDS_MODULES : {};
        const m = modules[moduleKey];
        if (!m) throw new Error(`Unknown module key: ${moduleKey}`);
        const dids = (typeof UDS_STANDARD_DIDS !== 'undefined') ? UDS_STANDARD_DIDS : {};
        const out = { module: moduleKey, label: m.label, responses: {} };
        for (const [did, spec] of Object.entries(dids)) {
            const r = await this.readDataByIdentifier(did, {
                reqHeader: m.request, respFilter: m.response,
            }).catch(() => null);
            if (r && r.ok && r.data) {
                if (spec.ascii) {
                    let s = '';
                    for (let i = 0; i + 2 <= r.data.length; i += 2) {
                        const b = parseInt(r.data.slice(i, i + 2), 16);
                        if (b >= 0x20 && b <= 0x7E) s += String.fromCharCode(b);
                    }
                    out.responses[spec.label] = s.trim() || r.data;
                } else {
                    out.responses[spec.label] = r.data;
                }
            }
        }
        this._emit('module_info', out);
        return out;
    }

    async readFuelSystemHealth() {
        const pids = ['06', '07', '08', '09', '0A', '0B', '0E', '14', '15', '22', '23', '33', '5E'];
        const snapshot = { _at: Date.now() };
        for (const pid of pids) {
            try {
                const raw = await this._sendCommand('01' + pid, 2500);
                const v = this._parsePIDResponse(pid, raw);
                if (v !== null) {
                    const def = PIDS[pid];
                    snapshot[def.label] = { value: v, unit: def.unit };
                }
            } catch (_) { /* tolerate single-PID misses */ }
        }
        // Quick verdict — require at least 2 valid trim values, AND use the
        // SUM-OF-BANK-AVERAGES (not raw sum) so a 2-bank engine isn't unfairly
        // doubled. Previously a single noisy spike on STFT could flip the
        // verdict between calls — now we only commit if the signal is real.
        const tB1 = ['stft_b1', 'ltft_b1']
            .map(k => snapshot[k] && snapshot[k].value)
            .filter(v => Number.isFinite(v));
        const tB2 = ['stft_b2', 'ltft_b2']
            .map(k => snapshot[k] && snapshot[k].value)
            .filter(v => Number.isFinite(v));
        const totalReadings = tB1.length + tB2.length;

        let verdict = 'طبيعي', verdict_severity = 'ok';
        if (totalReadings < 2) {
            // Not enough data — be honest, don't fabricate a verdict.
            verdict = 'بيانات نظام الوقود غير كافية للحكم — جرّب الفحص تاني والمحرك في idle ثابت';
            verdict_severity = 'unknown';
        } else {
            const avgB1 = tB1.length ? tB1.reduce((a,b) => a+b, 0) / tB1.length : 0;
            const avgB2 = tB2.length ? tB2.reduce((a,b) => a+b, 0) / tB2.length : 0;
            // Sum-of-bank-averages: a balanced engine should be near zero.
            const combined = (avgB1 + avgB2);
            if (combined > 18) {
                verdict = 'العربية بتسحب بنزين زيادة (lean) — احتمال شفط هواء أو طلمبة ضعيفة';
                verdict_severity = 'warn';
            } else if (combined < -18) {
                verdict = 'العربية بتحرق بنزين زيادة (rich) — احتمال إنجكتر مكهرب أو حساس O2 تعبان';
                verdict_severity = 'warn';
            }
            snapshot._trim_combined = +combined.toFixed(1);
            snapshot._trim_b1_avg = +avgB1.toFixed(1);
            snapshot._trim_b2_avg = +avgB2.toFixed(1);
        }
        snapshot._verdict = verdict;
        snapshot._verdict_severity = verdict_severity;
        snapshot._readings_count = totalReadings;
        this._emit('fuel_health', snapshot);
        return snapshot;
    }

    async computeHealthScore({ dtcs = null, misfire = null, fuel = null } = {}) {
        const data = {
            dtcs:    dtcs    || (await this.readDTCs().catch(() => [])),
            misfire: misfire || (await this.readMisfireCounts().catch(() => [])),
            fuel:    fuel    || (await this.readFuelSystemHealth().catch(() => ({}))),
        };

        let score = 100;
        const factors = [];

        // DTCs: stored are bad, pending are warnings, permanent are critical
        const stored    = (data.dtcs.byType || []).filter(d => d.type === 'stored').length;
        const pending   = (data.dtcs.byType || []).filter(d => d.type === 'pending').length;
        const permanent = (data.dtcs.byType || []).filter(d => d.type === 'permanent').length;
        const dtcPenalty = Math.min(25, stored * 8 + pending * 4 + permanent * 10);
        score -= dtcPenalty;
        if (dtcPenalty) factors.push(
            `−${dtcPenalty} نقطة بسبب الأكواد (${stored} مخزن، ${pending} pending، ${permanent} دائم)`);

        // Fuel trims: |STFT+LTFT per bank| > 10% is concerning
        const trims = ['stft_b1', 'ltft_b1', 'stft_b2', 'ltft_b2']
            .map(k => (data.fuel && data.fuel[k] && data.fuel[k].value) || 0);
        const trimSum = Math.abs(trims[0] + trims[1]) + Math.abs(trims[2] + trims[3]);
        const trimPenalty = trimSum > 30 ? 20 : trimSum > 20 ? 12 : trimSum > 10 ? 6 : 0;
        score -= trimPenalty;
        if (trimPenalty) factors.push(`−${trimPenalty} نقطة بسبب fuel trim مرتفع (${trimSum.toFixed(1)}%)`);

        // Misfire: any cylinder with count > 0 is bad
        const misfireTotal = (data.misfire || []).reduce((s, c) => s + c.count, 0);
        const misfirePenalty = Math.min(25, misfireTotal * 3);
        score -= misfirePenalty;
        if (misfirePenalty) factors.push(`−${misfirePenalty} نقطة بسبب ${misfireTotal} misfire إجمالي`);

        // O2 voltage: if both pre-cat and post-cat are flat AND similar, cat is dead
        const o2pre  = data.fuel && data.fuel.o2_b1s1_v && data.fuel.o2_b1s1_v.value;
        const o2post = data.fuel && data.fuel.o2_b1s2_v && data.fuel.o2_b1s2_v.value;
        let o2Penalty = 0;
        if (o2pre !== undefined && o2post !== undefined && Math.abs(o2pre - o2post) < 0.05) {
            o2Penalty = 15;
            factors.push(`−15 نقطة بسبب حساس O2 خامل (احتمال كتلست تالف)`);
        }
        score -= o2Penalty;

        // Cylinder balance — standard deviation of misfire counts
        let cylinderBalance = 100;
        if ((data.misfire || []).length >= 2) {
            const counts = data.misfire.map(c => c.count);
            const mean   = counts.reduce((a, b) => a + b, 0) / counts.length;
            const variance = counts.reduce((s, c) => s + (c - mean) ** 2, 0) / counts.length;
            const stdev = Math.sqrt(variance);
            cylinderBalance = Math.max(0, 100 - stdev * 20);
        }

        score = Math.max(0, Math.min(100, Math.round(score)));

        let verdict, color;
        if (score >= 85)      { verdict = 'حالة ممتازة — العربية بصحة جيدة'; color = '#10b981'; }
        else if (score >= 70) { verdict = 'حالة جيدة مع ملاحظات بسيطة'; color = '#84cc16'; }
        else if (score >= 50) { verdict = 'تحتاج صيانة قريبة';            color = '#f59e0b'; }
        else if (score >= 30) { verdict = 'مشاكل حقيقية — صيانة عاجلة'; color = '#ef4444'; }
        else                  { verdict = 'حالة حرجة — متشغّلش العربية على الطريق'; color = '#b91c1c'; }

        const result = {
            score, verdict, color, factors,
            cylinderBalance: Math.round(cylinderBalance),
            details: data,
        };
        this._emit('health_score', result);
        return result;
    }

    async measureMAFHealth({ displacement = 2.0, samples = 5 } = {}) {
        const readings = [];
        for (let i = 0; i < samples; i++) {
            const snap = {};
            for (const pid of ['0C', '0B', '05', '0F', '10', '04', '11']) {
                try {
                    const raw = await this._sendCommand(`01${pid}`, 2000);
                    const v = this._parsePIDResponse(pid, raw);
                    if (v !== null && Number.isFinite(v)) {
                        snap[PIDS[pid].label] = v;
                    }
                } catch (_) {}
            }
            if (snap.rpm && snap.map_kpa && snap.maf_gs !== undefined) {
                readings.push(snap);
            }
            await new Promise(r => setTimeout(r, 250));
        }

        if (!readings.length) {
            const r = { supported: false,
                reason: 'الـ ECU مردش بـ MAF / MAP. هذه السيارة على الأرجح Speed-Density (مفيش MAF فيزيائي).' };
            this._emit('sensor_health', { sensor: 'maf', ...r });
            return r;
        }

        // Use the median sample to avoid spikes.
        readings.sort((a, b) => a.rpm - b.rpm);
        const m = readings[Math.floor(readings.length / 2)];
        const T_K = (m.intake_temp_c ?? 25) + 273.15;
        const VE = 0.85;                          // typical volumetric efficiency
        const R  = 0.287;                          // specific gas constant kJ/kg·K
        const expected = (m.rpm * m.map_kpa * displacement * VE) / (120 * R * T_K);
        const actual = m.maf_gs;
        const ratio = actual / expected;          // 1.0 = ideal

        let verdict, color, severity;
        if (ratio >= 0.85 && ratio <= 1.20) {
            verdict = 'سليم — قراءة MAF متطابقة مع النظري'; color = '#10b981'; severity = 'ok';
        } else if (ratio >= 0.70 && ratio < 0.85) {
            verdict = 'MAF متسخ — نظّفه بـ MAF cleaner'; color = '#f59e0b'; severity = 'warn';
        } else if (ratio < 0.70) {
            verdict = 'MAF تالف أو شفط هواء كبير — يحتاج تغيير أو فحص الـ intake'; color = '#ef4444'; severity = 'critical';
        } else {
            verdict = 'MAF يقرأ أعلى من المتوقع — احتمال شورت أو مشكلة في الـ wiring'; color = '#ef4444'; severity = 'critical';
        }

        const result = {
            sensor: 'maf', supported: true,
            actual_gs: +actual.toFixed(2),
            expected_gs: +expected.toFixed(2),
            ratio: +ratio.toFixed(2),
            verdict, color, severity,
            sample: m,
        };
        this._emit('sensor_health', result);
        return result;
    }

    async measureO2AndCatalyst({ durationMs = 12000, intervalMs = 250 } = {}) {
        const start = Date.now();
        const pre = [], post = [];
        while (Date.now() - start < durationMs) {
            const ts = Date.now() - start;
            try {
                const r1 = await this._sendCommand('0114', 1500);
                const v1 = this._parsePIDResponse('14', r1);
                if (v1 !== null && Number.isFinite(v1)) pre.push({ t: ts, v: v1 });
            } catch (_) {}
            try {
                const r2 = await this._sendCommand('0115', 1500);
                const v2 = this._parsePIDResponse('15', r2);
                if (v2 !== null && Number.isFinite(v2)) post.push({ t: ts, v: v2 });
            } catch (_) {}
            await new Promise(r => setTimeout(r, intervalMs));
        }

        if (!pre.length || !post.length) {
            const r = { supported: false,
                reason: 'حساسات O2 (PID 14/15) لم تستجب. ربما السيارة wide-band فقط (PID 24-2B)؛ غير مدعوم حالياً.' };
            this._emit('sensor_health', { sensor: 'o2', ...r });
            return r;
        }

        // Count crossings of 0.45V — that's how many switches between rich/lean.
        const countCrossings = (arr) => {
            let c = 0;
            for (let i = 1; i < arr.length; i++) {
                if ((arr[i - 1].v < 0.45) !== (arr[i].v < 0.45)) c++;
            }
            return c;
        };
        const crossPre  = countCrossings(pre);
        const crossPost = countCrossings(post);
        const durSec    = durationMs / 1000;
        const hzPre     = crossPre  / (durSec * 2);    // crossings/2 = full cycles
        const hzPost    = crossPost / (durSec * 2);

        const range = (arr) => {
            const vs = arr.map(p => p.v);
            return { min: Math.min(...vs), max: Math.max(...vs), avg: vs.reduce((a, b) => a + b, 0) / vs.length };
        };
        const preR  = range(pre);
        const postR = range(post);

        // Catalyst efficiency: ratio of downstream activity to upstream.
        // Healthy = post Hz << pre Hz (cat dampens oscillation).
        // Dead    = post Hz ≈ pre Hz (cat passes through).
        const catRatio = hzPre > 0 ? hzPost / hzPre : 1;
        const catEfficiency = Math.max(0, Math.min(100, Math.round((1 - catRatio) * 100)));

        let o2Verdict, o2Color, o2Sev;
        if (hzPre >= 0.8) {
            o2Verdict = 'حساس O2 الأمامي سليم — تذبذب سريع طبيعي'; o2Color = '#10b981'; o2Sev = 'ok';
        } else if (hzPre >= 0.3) {
            o2Verdict = 'حساس O2 الأمامي بطيء — احتمال متهالك'; o2Color = '#f59e0b'; o2Sev = 'warn';
        } else {
            o2Verdict = 'حساس O2 الأمامي خامل أو معطل'; o2Color = '#ef4444'; o2Sev = 'critical';
        }

        let catVerdict, catColor, catSev;
        if (catEfficiency >= 75) {
            catVerdict = `كتلست بكفاءة ${catEfficiency}% — سليم`; catColor = '#10b981'; catSev = 'ok';
        } else if (catEfficiency >= 50) {
            catVerdict = `كتلست بكفاءة ${catEfficiency}% — متآكل، راقبه`; catColor = '#f59e0b'; catSev = 'warn';
        } else {
            catVerdict = `كتلست بكفاءة ${catEfficiency}% — تالف، احتمال P0420 قريباً`; catColor = '#ef4444'; catSev = 'critical';
        }

        const result = {
            sensor: 'o2', supported: true,
            upstream:   { hz: +hzPre.toFixed(2),  ...preR },
            downstream: { hz: +hzPost.toFixed(2), ...postR },
            catalyst_efficiency_pct: catEfficiency,
            o2_verdict: o2Verdict, o2_color: o2Color, o2_severity: o2Sev,
            cat_verdict: catVerdict, cat_color: catColor, cat_severity: catSev,
            sample_count: { pre: pre.length, post: post.length },
        };
        this._emit('sensor_health', result);
        return result;
    }

    async measureIdleTpsBattery({ durationMs = 20000, intervalMs = 400 } = {}) {
        const start = Date.now();
        const rpms = [], tpsAbs = [], tpsRel = [], batt = [];
        while (Date.now() - start < durationMs) {
            for (const [pid, bucket] of [['0C', rpms], ['11', tpsAbs], ['45', tpsRel], ['42', batt]]) {
                try {
                    const raw = await this._sendCommand(`01${pid}`, 1500);
                    const v = this._parsePIDResponse(pid, raw);
                    if (v !== null && Number.isFinite(v)) bucket.push(v);
                } catch (_) {}
            }
            await new Promise(r => setTimeout(r, intervalMs));
        }

        // Idle stability
        let idle = { supported: false };
        if (rpms.length >= 5) {
            const mean = rpms.reduce((a, b) => a + b, 0) / rpms.length;
            const variance = rpms.reduce((s, v) => s + (v - mean) ** 2, 0) / rpms.length;
            const stdev = Math.sqrt(variance);
            let v, c, sev;
            if (mean < 1100) {
                if (stdev < 40)      { v = `Idle مستقر جداً (±${stdev.toFixed(0)} rpm)`;          c = '#10b981'; sev = 'ok'; }
                else if (stdev < 80) { v = `Idle مستقر (±${stdev.toFixed(0)} rpm)`;                c = '#84cc16'; sev = 'ok'; }
                else if (stdev < 150){ v = `Idle متذبذب (±${stdev.toFixed(0)} rpm) — راقب`;       c = '#f59e0b'; sev = 'warn'; }
                else                  { v = `Idle مهتز بشدة (±${stdev.toFixed(0)} rpm) — شفط هواء/إشعال`; c = '#ef4444'; sev = 'critical'; }
            } else {
                v = 'السيارة مش على Idle — أعد القياس والمحرك ساكن';
                c = '#94a3b8'; sev = 'unknown';
            }
            idle = { supported: true, mean_rpm: +mean.toFixed(0), stdev_rpm: +stdev.toFixed(1),
                     verdict: v, color: c, severity: sev };
        }

        // TPS sync check
        let tps = { supported: false };
        if (tpsAbs.length && tpsRel.length) {
            const a = tpsAbs.reduce((s, v) => s + v, 0) / tpsAbs.length;
            const b = tpsRel.reduce((s, v) => s + v, 0) / tpsRel.length;
            const delta = Math.abs(a - b);
            let v, c, sev;
            if (delta < 4)       { v = `TPS متزامن (فرق ${delta.toFixed(1)}%)`;           c = '#10b981'; sev = 'ok'; }
            else if (delta < 8)  { v = `TPS بانحراف بسيط (${delta.toFixed(1)}%)`;          c = '#f59e0b'; sev = 'warn'; }
            else                  { v = `TPS غير متزامن (${delta.toFixed(1)}%) — يحتاج adaptation reset`; c = '#ef4444'; sev = 'critical'; }
            tps = { supported: true, absolute_pct: +a.toFixed(1), relative_pct: +b.toFixed(1),
                    delta_pct: +delta.toFixed(1), verdict: v, color: c, severity: sev };
        }

        // Battery state
        let battery = { supported: false };
        if (batt.length) {
            const mean = batt.reduce((s, v) => s + v, 0) / batt.length;
            const stdev = Math.sqrt(batt.reduce((s, v) => s + (v - mean) ** 2, 0) / batt.length);
            let v, c, sev;
            // Engine running → expect 13.8-14.6V (alternator charging)
            // Engine off    → expect 12.4-12.7V
            if (mean >= 13.8 && mean <= 14.8) {
                v = `الدينمو شغّال — جهد ${mean.toFixed(2)}V (سليم)`; c = '#10b981'; sev = 'ok';
            } else if (mean >= 13.0 && mean < 13.8) {
                v = `جهد منخفض ${mean.toFixed(2)}V — احتمال الدينمو ضعيف`; c = '#f59e0b'; sev = 'warn';
            } else if (mean >= 12.3 && mean < 13.0) {
                v = `المحرك مطفي — البطارية ${mean.toFixed(2)}V (مقبول)`; c = '#84cc16'; sev = 'ok';
            } else if (mean < 12.3) {
                v = `بطارية ضعيفة (${mean.toFixed(2)}V) — تحتاج شحن أو تغيير`; c = '#ef4444'; sev = 'critical';
            } else if (mean > 14.8) {
                v = `جهد عالي خطر (${mean.toFixed(2)}V) — منظم الدينمو تالف`; c = '#ef4444'; sev = 'critical';
            }
            battery = { supported: true, mean_v: +mean.toFixed(2), stdev_v: +stdev.toFixed(2),
                        verdict: v, color: c, severity: sev };
        }

        const result = { idle, tps, battery, samples: { rpm: rpms.length, batt: batt.length } };
        this._emit('sensor_sweep', result);
        return result;
    }

    async probeCapabilities() {
        const cap = {
            _at: Date.now(),
            modes: {},
            pid_support_mask: null,
            recommendation: null,
        };

        // Mode 01 PID 00 returns a 32-bit bitmask of supported PIDs 01-20.
        // If this fails, the bus is barely talking — flag the adapter.
        try {
            const raw = await this._sendCommand('0100', 2500);
            const stripped = (raw || '').replace(/\s+/g, '').toUpperCase();
            const m = stripped.match(/4100([0-9A-F]{8})/);
            if (m) {
                cap.modes.mode01 = true;
                cap.pid_support_mask = m[1];
                // Bit 0x20 of byte 4 = PID 0x20 supported → there's an extended set
                cap.has_extended_pids = (parseInt(m[1].slice(6, 8), 16) & 0x01) !== 0;
            } else {
                cap.modes.mode01 = false;
            }
        } catch (_) { cap.modes.mode01 = false; }

        // Mode 03 — stored DTCs. Even with no codes, a CAN ECU echoes "43 00"
        try {
            const raw = await this._sendCommand('03', 2000);
            cap.modes.mode03 =
                !!raw && !/NO\s*DATA|UNABLE|\?|STOPPED/i.test(raw)
                && raw.replace(/\s+/g, '').toUpperCase().indexOf('43') >= 0;
        } catch (_) { cap.modes.mode03 = false; }

        // Mode 06 OBDMID $A1 (cyl 1 misfire) — proves per-cylinder is available
        try { await this._sendCommand('ATH1', 800); } catch (_) {}
        try {
            const raw = await this._sendCommand('06A1', 2500);
            cap.modes.mode06_misfire =
                !!raw && raw.replace(/\s+/g, '').toUpperCase().indexOf('46A1') >= 0;
        } catch (_) { cap.modes.mode06_misfire = false; }
        try { await this._sendCommand('ATH0', 800); } catch (_) {}

        // Mode 09 PID 02 — VIN. Required for any vehicle linking.
        try {
            const raw = await this._sendCommand('0902', 3500);
            cap.modes.mode09 = !!raw && raw.replace(/\s+/g, '').toUpperCase().indexOf('4902') >= 0;
        } catch (_) { cap.modes.mode09 = false; }

        // Mode 22 — UDS. Probe with the OBD-II VIN DID 0xF190 (universally
        // supported by post-2008 UDS-capable ECUs).
        try {
            const raw = await this._sendCommand('22F190', 2500);
            const s = (raw || '').replace(/\s+/g, '').toUpperCase();
            cap.modes.mode22 = s.indexOf('62F190') >= 0 ||
                               (s.indexOf('7F22') < 0 && !/NO\s*DATA/i.test(raw));
        } catch (_) { cap.modes.mode22 = false; }

        // ── Decision tree: what reports can we produce? ─────────────────
        const reports = [];
        if (cap.modes.mode01) reports.push({ key: 'live_data', label_ar: 'القراءات الحية (سرعة، RPM، حرارة)', supported: true });
        if (cap.modes.mode03) reports.push({ key: 'dtcs', label_ar: 'قراءة أكواد الأعطال', supported: true });
        if (cap.modes.mode09) reports.push({ key: 'vin', label_ar: 'قراءة VIN ومعلومات الـ ECU', supported: true });
        reports.push({ key: 'misfire_per_cylinder', label_ar: 'Misfire لكل سلندر',
            supported: cap.modes.mode06_misfire || cap.modes.mode22,
            note: !cap.modes.mode06_misfire && !cap.modes.mode22
                ? 'هنستخدم تحليل تذبذب RPM (تقديري) كبديل' : null });
        reports.push({ key: 'manufacturer_dids', label_ar: 'قراءات خاصة بالماركة (Mode 22)',
            supported: !!cap.modes.mode22,
            note: !cap.modes.mode22 ? 'سيارة قديمة قبل UDS أو الـ ECU محتاج adapter أحدث' : null });
        reports.push({ key: 'emissions', label_ar: 'فحص الانبعاثات (AFR/EGR/EVAP)',
            supported: !!cap.modes.mode01, note: null });
        cap.reports = reports;

        // Adapter recommendation logic
        const supportedReports = reports.filter(r => r.supported).length;
        if (supportedReports >= 5) {
            cap.recommendation = { level: 'excellent',
                msg: 'الـ adapter ممتاز — كل التقارير الشاملة متاحة.' };
        } else if (supportedReports >= 3) {
            cap.recommendation = { level: 'good',
                msg: 'الـ adapter شغّال لكن مش بيدعم Mode 22. للـ BMW/Mercedes يُفضل ELM327 v2.2 أو أعلى.' };
        } else if (supportedReports >= 1) {
            cap.recommendation = { level: 'limited',
                msg: 'الـ adapter محدود — جرّب dongle ELM327 v2.2+ للحصول على تقارير كاملة.' };
        } else {
            cap.recommendation = { level: 'broken',
                msg: 'الـ adapter مش بيرد — تأكد إنه متركّب صح والـ ignition ON.' };
        }

        this._emit('capabilities', cap);
        return cap;
    }

    async readDID(did, parser, { ecuHeader = null, timeoutMs = 2500 } = {}) {
        const hex = String(did).replace(/\s+/g, '').toUpperCase();
        if (!/^[0-9A-F]{4}$/.test(hex)) {
            throw new Error(`Invalid DID format: ${did} (must be 4 hex chars)`);
        }
        // Optionally pin to a specific ECU (BMW DME = 12, Mercedes EZS = 7E0, etc.)
        if (ecuHeader) {
            try { await this._sendCommand('ATSH' + ecuHeader, 800); } catch (_) {}
        }
        let raw;
        try {
            raw = await this._sendCommand('22' + hex, timeoutMs);
        } catch (e) {
            return null;
        }
        if (!raw) return null;
        const stripped = raw.replace(/\s+/g, '').toUpperCase();
        // Negative Response Code → DID not supported on this ECU.
        if (stripped.indexOf('7F22') >= 0) return null;
        if (/NO\s*DATA|UNABLE|\?|STOPPED/i.test(raw)) return null;
        // Locate the positive response header "62" + DID.
        const header = '62' + hex;
        const idx = stripped.indexOf(header);
        if (idx < 0) return null;
        const body = stripped.slice(idx + header.length);
        const bytes = [];
        for (let i = 0; i + 2 <= body.length; i += 2) {
            const b = parseInt(body.slice(i, i + 2), 16);
            if (!Number.isFinite(b)) break;
            bytes.push(b);
        }
        if (!bytes.length) return null;
        try {
            const v = parser(bytes);
            return (v === undefined || v === null ||
                    (typeof v === 'number' && !Number.isFinite(v)))
                ? null : v;
        } catch (_) {
            return null;
        }
    }

    async scanManufacturerDIDs(vin = null) {
        const brand = (window.__detectManufacturerFromVIN || (() => null))(vin) || 'generic';
        const lib = (window.__MANUFACTURER_DIDS || {})[brand] || [];
        const setup = (window.__MANUFACTURER_SETUP || {})[brand] || {};

        const result = {
            _at: Date.now(),
            manufacturer: brand,
            vin: vin || null,
            items: [],
            supported_count: 0,
            unsupported_count: 0,
            ecus_probed: [],
        };

        if (!lib.length) {
            result.note = 'مفيش DIDs مسجّلة للماركة دي حالياً.';
            this._emit('manufacturer_dids', result);
            return result;
        }

        // Most OEMs need ECU pinning to talk to powertrain modules. We loop
        // over the brand's known ECU header/response combinations and try
        // every DID against each ECU until we get a hit.
        const ecuList = setup.ecus || [{ name: 'default', header: null, cra: null }];
        try { await this._sendCommand('ATH0', 800); } catch (_) {}

        const tried = new Set();   // dedupe across ECUs by DID
        for (const ecu of ecuList) {
            // Apply ECU framing (BMW-style: ATSH 6F1 + ATCRA 612).
            if (ecu.header) {
                try { await this._sendCommand('ATSH' + ecu.header, 800); } catch (_) {}
            }
            if (ecu.cra) {
                try { await this._sendCommand('ATCRA' + ecu.cra, 800); } catch (_) {}
            }
            const ecuHit = [];
            for (const def of lib) {
                if (tried.has(def.did)) continue;
                try {
                    const value = await this.readDID(def.did, def.parse,
                        { ecuHeader: null, timeoutMs: 2000 });
                    if (value === null) continue;
                    result.items.push({
                        did:    def.did,
                        label:  def.label,
                        label_ar: def.label_ar || def.label,
                        unit:   def.unit || '',
                        value,
                        ecu:    ecu.name,
                        health: def.health ? def.health(value) : null,
                    });
                    result.supported_count += 1;
                    tried.add(def.did);
                    ecuHit.push(def.did);
                } catch (_) { /* per-DID failure non-fatal */ }
                await new Promise(r => setTimeout(r, 40));
            }
            result.ecus_probed.push({ name: ecu.name, hits: ecuHit.length });
        }

        // Clear ECU pinning so subsequent commands fall back to auto.
        try { await this._sendCommand('ATCRA', 600); } catch (_) {}

        result.unsupported_count = lib.length - result.supported_count;
        this._emit('manufacturer_dids', result);
        return result;
    }

    async detectMisfire({ vin = null, cylinders = 4 } = {}) {
        // ── Tier 1: Mode 06 standard ────────────────────────────────────
        try {
            const std = await this.readMisfireCounts({ cylinders });
            if (std && std.length) {
                const out = {
                    method: 'mode06',
                    method_label_ar: 'القياس القياسي (Mode 06)',
                    confidence: 'high',
                    cylinders: std,
                    total: std.reduce((s, c) => s + c.count, 0),
                };
                this._emit('misfire_diagnosis', out);
                return out;
            }
        } catch (_) {}

        // ── Tier 2: Manufacturer DIDs (Mode 22) ─────────────────────────
        const brand = (window.__detectManufacturerFromVIN || (() => null))(vin);
        const misfireDIDs = (window.__MISFIRE_DIDS || {})[brand] || [];
        if (misfireDIDs.length) {
            const setup = (window.__MANUFACTURER_SETUP || {})[brand] || {};
            const ecuList = setup.ecus || [{ name: 'default', header: null, cra: null }];
            try { await this._sendCommand('ATH0', 600); } catch (_) {}

            const found = [];
            outer: for (const ecu of ecuList) {
                if (ecu.header) {
                    try { await this._sendCommand('ATSH' + ecu.header, 600); } catch (_) {}
                }
                if (ecu.cra) {
                    try { await this._sendCommand('ATCRA' + ecu.cra, 600); } catch (_) {}
                }
                for (const md of misfireDIDs) {
                    const v = await this.readDID(md.did, md.parse, { timeoutMs: 1800 });
                    if (v !== null && Number.isFinite(v)) {
                        found.push({ cylinder: md.cyl, count: v });
                    }
                }
                // Stop after we got at least one cylinder hit on this ECU.
                if (found.length) break outer;
            }
            try { await this._sendCommand('ATCRA', 500); } catch (_) {}

            if (found.length) {
                const out = {
                    method: 'mode22_' + brand,
                    method_label_ar: `Mode 22 — DIDs خاصة بـ ${brand.toUpperCase()}`,
                    confidence: 'high',
                    cylinders: found,
                    total: found.reduce((s, c) => s + c.count, 0),
                };
                this._emit('misfire_diagnosis', out);
                return out;
            }
        }

        // ── Tier 3: RPM variance analysis at idle ───────────────────────
        const computed = await this._computeMisfireFromRPMVariance({ cylinders });
        const out = { method: 'computed', confidence: 'medium',
                      method_label_ar: 'تحليل تذبذب RPM (تقديري)',
                      ...computed };
        this._emit('misfire_diagnosis', out);
        return out;
    }

    async _computeMisfireFromRPMVariance({ cylinders = 4, durationMs = 20000,
                                          intervalMs = 100 } = {}) {
        const samples = [];                 // [{ t, rpm }]
        const start = Date.now();
        while (Date.now() - start < durationMs) {
            const t = Date.now() - start;
            try {
                const raw = await this._sendCommand('010C', 800);
                const rpm = this._parsePIDResponse('0C', raw);
                if (rpm !== null && rpm > 400 && rpm < 2000) {
                    samples.push({ t, rpm });
                }
            } catch (_) {}
            await new Promise(r => setTimeout(r, intervalMs));
        }

        if (samples.length < 30) {
            return { supported: false, cylinders: [],
                     reason: 'مفيش عيّنات كفاية لتحليل التذبذب (شغّل المحرك في idle ثابت).' };
        }

        // Statistics
        const rpms = samples.map(s => s.rpm);
        const mean = rpms.reduce((s, x) => s + x, 0) / rpms.length;
        const variance = rpms.reduce((s, x) => s + (x - mean) ** 2, 0) / rpms.length;
        const stdev = Math.sqrt(variance);

        // Count sudden drops > 80 RPM below mean (likely misfire events).
        let drops = 0;
        for (let i = 1; i < rpms.length; i++) {
            if (rpms[i] < mean - 80 && rpms[i - 1] >= mean - 30) drops++;
        }

        // We can't pinpoint which cylinder without a CKP signal — distribute
        // the suspicion evenly. If stdev is low (<25), declare all clear.
        const perCyl = stdev < 25 ? 0 : Math.round(drops / cylinders);
        const cyls = [];
        for (let c = 1; c <= cylinders; c++) {
            cyls.push({ cylinder: c, count: perCyl, suspected: perCyl > 0 });
        }

        let verdict, color, severity;
        if (stdev < 25) {
            verdict = `Idle ثابت (σ=${stdev.toFixed(1)} rpm) — مفيش misfire واضح`;
            color = '#10b981'; severity = 'ok';
        } else if (stdev < 60) {
            verdict = `تذبذب طفيف (σ=${stdev.toFixed(1)} rpm) — احتمال شفط هواء أو مشكلة وقود بسيطة`;
            color = '#f59e0b'; severity = 'warn';
        } else {
            verdict = `تذبذب شديد (σ=${stdev.toFixed(1)} rpm، ${drops} drop) — مشكلة إشعال أكيدة`;
            color = '#ef4444'; severity = 'critical';
        }

        return {
            supported: true,
            cylinders: cyls,
            total: drops,
            mean_rpm: +mean.toFixed(0),
            stdev_rpm: +stdev.toFixed(1),
            drops_count: drops,
            verdict, color, severity,
            note: 'تقدير من تحليل التذبذب — مش بيحدد السلندر بالظبط زي Mode 06',
        };
    }

    async measureEmissionsHealth({ samples = 8, intervalMs = 600 } = {}) {
        // PIDs we'll probe. Skip silently on unsupported.
        const afrPids   = ['24', '25', '26', '27'];     // wideband λ on banks 1-4
        const fallbackO2 = ['14', '15'];                // narrowband fallback
        const egrPids   = ['2C', '2D'];
        const evapPids  = ['2E', '32', '53', '54'];

        const lam = {}, egr = { cmd: [], err: [] }, evap = {};
        const narrow_fallback = { b1s1: [], b1s2: [] };

        for (let i = 0; i < samples; i++) {
            // AFR — try wideband first
            for (const pid of afrPids) {
                try {
                    const raw = await this._sendCommand(`01${pid}`, 1200);
                    const v = this._parsePIDResponse(pid, raw);
                    if (v !== null && v > 0.3 && v < 2.0) {
                        (lam[pid] = lam[pid] || []).push(v);
                    }
                } catch (_) {}
            }
            // EGR commanded + error
            for (const pid of egrPids) {
                try {
                    const raw = await this._sendCommand(`01${pid}`, 1200);
                    const v = this._parsePIDResponse(pid, raw);
                    if (v !== null && Number.isFinite(v)) {
                        (pid === '2C' ? egr.cmd : egr.err).push(v);
                    }
                } catch (_) {}
            }
            // EVAP — snapshot once (don't average pressure waveforms here)
            if (i === Math.floor(samples / 2)) {
                for (const pid of evapPids) {
                    try {
                        const raw = await this._sendCommand(`01${pid}`, 1500);
                        const v = this._parsePIDResponse(pid, raw);
                        if (v !== null && Number.isFinite(v)) {
                            evap[PIDS[pid].label] = v;
                        }
                    } catch (_) {}
                }
            }
            // Narrowband fallback if no wideband seen yet
            if (!Object.keys(lam).length) {
                for (const pid of fallbackO2) {
                    try {
                        const raw = await this._sendCommand(`01${pid}`, 1200);
                        const v = this._parsePIDResponse(pid, raw);
                        if (v !== null) {
                            (pid === '14' ? narrow_fallback.b1s1 : narrow_fallback.b1s2).push(v);
                        }
                    } catch (_) {}
                }
            }
            await new Promise(r => setTimeout(r, intervalMs));
        }

        // ── AFR verdict ─────────────────────────────────────────────────
        let afr = { supported: false };
        const usedWideband = Object.keys(lam).length > 0;
        if (usedWideband) {
            const banks = {};
            for (const [pid, arr] of Object.entries(lam)) {
                if (!arr.length) continue;
                const mean = arr.reduce((s,x) => s+x, 0) / arr.length;
                const lambdaToAfr = mean * 14.7;
                banks[`o2s${parseInt(pid,16) - 0x23}`] = {
                    lambda: +mean.toFixed(3),
                    afr: +lambdaToAfr.toFixed(2),
                    samples: arr.length,
                };
            }
            const lambdas = Object.values(banks).map(x => x.lambda);
            const avg = lambdas.reduce((s,x) => s+x, 0) / lambdas.length;
            let verdict, color, severity;
            if (avg >= 0.97 && avg <= 1.03) {
                verdict = `خليط سليم (λ=${avg.toFixed(2)} · AFR=${(avg*14.7).toFixed(1)})`;
                color = '#10b981'; severity = 'ok';
            } else if (avg < 0.85) {
                verdict = `خليط غني جداً (λ=${avg.toFixed(2)}) — احتمال بخاخ مفتوح أو حساس MAP/MAF`;
                color = '#ef4444'; severity = 'critical';
            } else if (avg < 0.97) {
                verdict = `خليط غني (λ=${avg.toFixed(2)}) — راجع فلتر هواء و LTFT`;
                color = '#f59e0b'; severity = 'warn';
            } else if (avg > 1.15) {
                verdict = `خليط فقير جداً (λ=${avg.toFixed(2)}) — شفط هواء أو ضعف بنزين`;
                color = '#ef4444'; severity = 'critical';
            } else {
                verdict = `خليط فقير (λ=${avg.toFixed(2)}) — راجع شفط الهواء`;
                color = '#f59e0b'; severity = 'warn';
            }
            afr = { supported: true, wideband: true, mean_lambda: +avg.toFixed(3),
                    mean_afr: +(avg * 14.7).toFixed(2), banks, verdict, color, severity };
        } else if (narrow_fallback.b1s1.length) {
            const avg = narrow_fallback.b1s1.reduce((s,x) => s+x, 0)
                      / narrow_fallback.b1s1.length;
            afr = {
                supported: true, wideband: false, mean_voltage: +avg.toFixed(3),
                verdict: 'الـ ECU narrowband فقط — استخدم "فحص الحسّاسات الشامل" لتقييم تذبذب الـ O2',
                color: '#64748b', severity: 'info',
            };
        } else {
            afr = { supported: false,
                    reason: 'الـ ECU مردش على PIDs الـ O2 (لا wideband ولا narrowband).' };
        }

        // ── EGR verdict ─────────────────────────────────────────────────
        let egrResult = { supported: false };
        if (egr.cmd.length || egr.err.length) {
            const cmdAvg = egr.cmd.length
                ? egr.cmd.reduce((s,x) => s+x, 0) / egr.cmd.length : null;
            const errAvg = egr.err.length
                ? egr.err.reduce((s,x) => s+x, 0) / egr.err.length : null;
            let verdict, color, severity;
            if (errAvg !== null && Math.abs(errAvg) > 25) {
                verdict = `صمام EGR متعطل — الفرق بين المطلوب والفعلي ${errAvg.toFixed(1)}%`;
                color = '#ef4444'; severity = 'critical';
            } else if (errAvg !== null && Math.abs(errAvg) > 10) {
                verdict = `صمام EGR محتاج تنظيف — فرق ${errAvg.toFixed(1)}%`;
                color = '#f59e0b'; severity = 'warn';
            } else if (cmdAvg !== null) {
                verdict = `EGR سليم (المطلوب ${cmdAvg.toFixed(1)}%، الخطأ ${(errAvg||0).toFixed(1)}%)`;
                color = '#10b981'; severity = 'ok';
            } else {
                verdict = 'EGR يقرأ لكن مفيش بيانات خطأ — تأكد على RPM متوسط';
                color = '#64748b'; severity = 'info';
            }
            egrResult = {
                supported: true,
                commanded_avg_pct: cmdAvg !== null ? +cmdAvg.toFixed(1) : null,
                error_avg_pct: errAvg !== null ? +errAvg.toFixed(1) : null,
                verdict, color, severity,
            };
        } else {
            egrResult = { supported: false,
                          reason: 'مفيش EGR في السيارة دي أو الـ ECU مش بيدعم PIDs 2C/2D.' };
        }

        // ── EVAP verdict ────────────────────────────────────────────────
        let evapResult = { supported: false };
        if (Object.keys(evap).length) {
            const purge = evap.evap_purge_cmd_pct;
            const vapPa = evap.evap_vapor_press_pa;
            const vapKpa = evap.evap_vapor_press_kpa;
            const absKpa = evap.evap_abs_vapor_kpa;
            let verdict, color, severity;
            // Rule: a healthy sealed system holds 100-1500 Pa vacuum at idle.
            // A leak shows ~0 Pa. An overpressure shows a stuck purge valve.
            if (vapPa !== undefined) {
                const absV = Math.abs(vapPa);
                if (absV < 50) {
                    verdict = `تسريب EVAP محتمل — ضغط الأبخرة قريب من الصفر (${vapPa.toFixed(0)} Pa)`;
                    color = '#f59e0b'; severity = 'warn';
                } else if (absV > 5000) {
                    verdict = `ضغط EVAP عالي جداً (${vapPa.toFixed(0)} Pa) — صمام purge عالق`;
                    color = '#ef4444'; severity = 'critical';
                } else {
                    verdict = `EVAP سليم — ضغط أبخرة ${vapPa.toFixed(0)} Pa`;
                    color = '#10b981'; severity = 'ok';
                }
            } else if (purge !== undefined) {
                verdict = `Purge مرتفع ${purge.toFixed(1)}% — لو معاها DTC EVAP افحص الكانستر`;
                color = '#64748b'; severity = 'info';
            } else {
                verdict = 'قراءة EVAP موجودة لكن ناقصة (مفيش vapor pressure)';
                color = '#64748b'; severity = 'info';
            }
            evapResult = {
                supported: true,
                purge_cmd_pct: purge !== undefined ? +purge.toFixed(1) : null,
                vapor_press_pa: vapPa !== undefined ? +vapPa.toFixed(0) : null,
                vapor_press_kpa: vapKpa !== undefined ? +vapKpa.toFixed(2) : null,
                abs_vapor_kpa: absKpa !== undefined ? +absKpa.toFixed(2) : null,
                verdict, color, severity,
            };
        } else {
            evapResult = { supported: false,
                           reason: 'مفيش بيانات EVAP — السيارة قديمة أو غير مدعومة.' };
        }

        const result = { afr, egr: egrResult, evap: evapResult,
                         _at: Date.now(),
                         samples: { afr: Object.values(lam).flat().length,
                                    egr: egr.cmd.length + egr.err.length,
                                    evap: Object.keys(evap).length } };
        this._emit('emissions_health', result);
        return result;
    }

    _emit(type, detail) {
        this.dispatchEvent(new CustomEvent(type, { detail }));
    }
}

// Browser exports — picked up by the Diagnostics Room template.
window.OBDBluetooth = OBDBluetooth;
window.humanizeBluetoothError = humanizeBluetoothError;
window.__OBD_TEST__ = {
    PIDS, KNOWN_SERVICE_UUIDS, SERVICE_BLOCKLIST, PAIRING_FILTERS,
    LS_REMEMBERED_DEVICES, _loadRememberedIds, _rememberDeviceId, _forgetDeviceId,
};
