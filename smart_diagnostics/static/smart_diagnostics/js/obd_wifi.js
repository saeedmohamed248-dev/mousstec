/* ============================================================
 * obd_wifi.js — Wi-Fi (TCP-over-WebSocket) transport for ELM327
 * ============================================================
 *
 * Browsers cannot open raw TCP sockets, so we cannot talk to a
 * Wi-Fi ELM327 dongle directly. The mechanic runs a tiny Python
 * helper on the laptop — see
 *   smart_diagnostics/tools/obd_wifi_bridge.py
 * The helper opens TCP to the dongle (default 192.168.0.10:35000)
 * and exposes a WebSocket on localhost (default ws://127.0.0.1:8765).
 *
 * This driver speaks WebSocket to that helper and otherwise behaves
 * exactly like the Bluetooth driver: same AT/ELM protocol, same PID
 * parsers, same event names ('status' / 'connected' / 'disconnected'
 * / 'initialized' / 'live' / 'dtcs' / 'dtcs_cleared' / 'vin').
 *
 * Public API matches OBDBluetooth so the Diagnostics Room UI can swap
 * transports with a single line change.
 */

const OBD_WIFI_DEFAULT_URL = 'ws://127.0.0.1:8765';
const OBD_WIFI_PROMPT = '>';
const OBD_WIFI_CR = '\r';
const OBD_WIFI_DEFAULT_TIMEOUT_MS = 2500;

// Mode 01 PIDs — kept in sync with obd_bluetooth.js intentionally.
// (Small duplication beats import-order coupling between the two files.)
const OBD_WIFI_PIDS = {
    '04': { label: 'engine_load',         unit: '%',    parse: b => b[0] * 100 / 255 },
    '05': { label: 'coolant_temp_c',      unit: '°C',   parse: b => b[0] - 40 },
    '06': { label: 'stft_b1',             unit: '%',    parse: b => (b[0] - 128) * 100 / 128 },
    '07': { label: 'ltft_b1',             unit: '%',    parse: b => (b[0] - 128) * 100 / 128 },
    '08': { label: 'stft_b2',             unit: '%',    parse: b => (b[0] - 128) * 100 / 128 },
    '09': { label: 'ltft_b2',             unit: '%',    parse: b => (b[0] - 128) * 100 / 128 },
    '0A': { label: 'fuel_pressure_kpa',   unit: 'kPa',  parse: b => b[0] * 3 },
    '0B': { label: 'map_kpa',             unit: 'kPa',  parse: b => b[0] },
    '0C': { label: 'rpm',                 unit: 'rpm',  parse: b => ((b[0] << 8) + b[1]) / 4 },
    '0D': { label: 'speed_kph',           unit: 'km/h', parse: b => b[0] },
    '0E': { label: 'timing_advance_deg',  unit: '°',    parse: b => (b[0] / 2) - 64 },
    '0F': { label: 'intake_temp_c',       unit: '°C',   parse: b => b[0] - 40 },
    '10': { label: 'maf_gs',              unit: 'g/s',  parse: b => ((b[0] << 8) + b[1]) / 100 },
    '11': { label: 'throttle_pct',        unit: '%',    parse: b => b[0] * 100 / 255 },
    '14': { label: 'o2_b1s1_v',           unit: 'V',    parse: b => b[0] / 200 },
    '15': { label: 'o2_b1s2_v',           unit: 'V',    parse: b => b[0] / 200 },
    '22': { label: 'fuel_rail_rel_kpa',   unit: 'kPa',  parse: b => ((b[0] << 8) + b[1]) * 0.079 },
    '23': { label: 'fuel_rail_abs_kpa',   unit: 'kPa',  parse: b => ((b[0] << 8) + b[1]) * 10 },
    '2F': { label: 'fuel_level_pct',      unit: '%',    parse: b => b[0] * 100 / 255 },
    '33': { label: 'baro_kpa',            unit: 'kPa',  parse: b => b[0] },
    '42': { label: 'control_voltage',     unit: 'V',    parse: b => ((b[0] << 8) + b[1]) / 1000 },
    '46': { label: 'ambient_temp_c',      unit: '°C',   parse: b => b[0] - 40 },
    '5C': { label: 'oil_temp_c',          unit: '°C',   parse: b => b[0] - 40 },
    '5E': { label: 'fuel_rate_lh',        unit: 'L/h',  parse: b => ((b[0] << 8) + b[1]) * 0.05 },

    // ── Wideband O2 sensors (Equivalence Ratio + Voltage) — PIDs 24-2B ──
    // ER = lambda. Stoich = 1.0. Lean > 1.0, rich < 1.0.
    // AFR_gasoline = ER * 14.7. Formula per SAE J1979.
    '24': { label: 'o2s1_lambda', unit: 'λ',
        parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 2 / 65535 : null },
    '25': { label: 'o2s2_lambda', unit: 'λ',
        parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 2 / 65535 : null },
    '26': { label: 'o2s3_lambda', unit: 'λ',
        parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 2 / 65535 : null },
    '27': { label: 'o2s4_lambda', unit: 'λ',
        parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 2 / 65535 : null },

    // EGR system
    '2C': { label: 'egr_commanded_pct', unit: '%',
        parse: b => b.length >= 1 ? b[0] * 100 / 255 : null },
    '2D': { label: 'egr_error_pct',     unit: '%',
        parse: b => b.length >= 1 ? (b[0] - 128) * 100 / 128 : null },

    // EVAP system
    '2E': { label: 'evap_purge_cmd_pct', unit: '%',
        parse: b => b.length >= 1 ? b[0] * 100 / 255 : null },
    '32': { label: 'evap_vapor_press_pa', unit: 'Pa',
        // Signed 16-bit, 0.25 Pa/bit. ECU may return positive or negative.
        parse: b => {
            if (b.length < 2) return null;
            const raw = (b[0] << 8) + b[1];
            const signed = raw >= 0x8000 ? raw - 0x10000 : raw;
            return signed * 0.25;
        }},
    '53': { label: 'evap_abs_vapor_kpa', unit: 'kPa',
        parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) * 0.005 : null },
    '54': { label: 'evap_vapor_press_kpa', unit: 'kPa',
        parse: b => {
            if (b.length < 2) return null;
            const raw = (b[0] << 8) + b[1];
            const signed = raw >= 0x8000 ? raw - 0x10000 : raw;
            return signed * 1.0;
        }},

    // ── Status / accumulators — non-numeric but vital for diagnosis ─────
    '03': { label: 'fuel_system_status', unit: '',
        parse: b => {
            const code = (b[0] || 0).toString(16).toUpperCase().padStart(2, '0');
            const map = { '01': 'open_low_temp', '02': 'closed_loop',
                          '04': 'open_load_or_decel', '08': 'open_failure',
                          '10': 'closed_with_fault' };
            return map[code] || code;
        }},
    '1C': { label: 'obd_standard', unit: '', parse: b => b[0] },
    '1F': { label: 'run_time_s', unit: 's',
        parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
    '21': { label: 'dist_with_mil_km', unit: 'km',
        parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
    '30': { label: 'warmups_since_clear', unit: '', parse: b => b[0] },
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
// Fast loop — only the 6 PIDs the gauges actually render. Polling more
// PIDs on a slow K-Line bus (Nissan/Hyundai 2011) takes 4+ seconds per
// cycle and makes the gauges feel laggy.
const OBD_WIFI_DEFAULT_POLL_PIDS = [
    '0C',   // rpm
    '0D',   // speed_kph
    '05',   // coolant_temp_c
    '11',   // throttle_pct
    '04',   // engine_load
    '42',   // control_voltage (battery)
];

// Slow loop — fuel-system PIDs polled every few seconds for the fuel
// health card. Not in the fast gauges path.
const OBD_WIFI_SLOW_PIDS = ['06', '07', '0E', '14', '0A', '5E'];

class OBDWiFi extends EventTarget {
    constructor() {
        super();
        this.ws = null;
        this.url = null;
        this._rxBuffer = '';
        this._pending = null;
        this._streaming = false;
        this._streamHandle = null;
        this._opened = false;
        this._lastBridgeError = null;     // last BRIDGE_ERROR reason from bridge
    }

    get isConnected() {
        return !!(this.ws && this.ws.readyState === WebSocket.OPEN && this._opened);
    }

    // ── Connect to the local Python bridge ──────────────────────────────
    async connect({ url = OBD_WIFI_DEFAULT_URL, timeoutMs = 4000 } = {}) {
        this.url = url;
        this._emit('status', { phase: 'connecting',
            message: `الاتصال بجسر Wi-Fi المحلي (${url})…` });

        await new Promise((resolve, reject) => {
            let settled = false;
            const watchdog = setTimeout(() => {
                if (settled) return;
                settled = true;
                try { ws.close(); } catch (_) {}
                reject(new Error(
                    'لا يمكن الوصول للجسر المحلي. تأكد أنك شغّلت ' +
                    'obd_wifi_bridge.py على اللاب توب وأن اللاب متصل ' +
                    'بشبكة Wi-Fi الفيشة.',
                ));
            }, timeoutMs);

            const ws = new WebSocket(url);
            ws.onopen = () => {
                if (settled) return;
                settled = true;
                clearTimeout(watchdog);
                this.ws = ws;
                this._opened = true;
                ws.onmessage = (ev) => this._onMessage(ev);
                ws.onclose  = () => this._onClose();
                ws.onerror  = (e) => this._emit('error', {
                    title: 'انقطع الجسر المحلي',
                    hint:  'تحقق أن obd_wifi_bridge.py لسه شغّال.',
                });
                resolve();
            };
            ws.onerror = () => {
                if (settled) return;
                settled = true;
                clearTimeout(watchdog);
                reject(new Error(
                    'فشل الاتصال بـ ' + url + '. ابدأ الجسر بـ ' +
                    '`python obd_wifi_bridge.py` ثم حاول مرة أخرى.',
                ));
            };
        });

        this._emit('connected', { transport: 'wifi', url });
        return { url };
    }

    async disconnect() {
        this.stopStream();
        this._opened = false;
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            try { this.ws.close(); } catch (_) {}
        }
        this.ws = null;
    }

    _onClose() {
        this._opened = false;
        this._streaming = false;
        const reason = this._lastBridgeError ||
            'انقطع الاتصال بالجسر — تأكد من اتصال اللاب بشبكة Wi-Fi الفيشة.';
        if (this._pending) {
            const { reject, timer } = this._pending;
            this._pending = null;
            clearTimeout(timer);
            reject(new Error(reason));
        }
        this._emit('disconnected', { transport: 'wifi', reason });
    }

    _onMessage(ev) {
        const txt = typeof ev.data === 'string'
            ? ev.data
            : new TextDecoder().decode(ev.data);

        // Bridge can prepend an error frame on TCP-connect failure.
        if (txt.startsWith('BRIDGE_ERROR')) {
            const reason = txt.replace(/[\r>]/g, '').trim();
            this._lastBridgeError = reason;
            if (this._pending) {
                const { reject, timer } = this._pending;
                this._pending = null;
                clearTimeout(timer);
                reject(new Error(reason));
            }
            return;
        }

        this._rxBuffer += txt;
        if (this._rxBuffer.includes(OBD_WIFI_PROMPT)) {
            const payload = this._rxBuffer
                .split(OBD_WIFI_PROMPT)[0]
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

    // ── ELM327 protocol — identical semantics to obd_bluetooth.js ───────
    // Commands are serialized through a promise chain so the live-data
    // stream cannot collide with a user-triggered Mode 03 / Mode 04 request.
    // Each queued command waits for the previous one to settle (success OR
    // failure) before sending bytes on the wire.
    async _sendCommand(cmd, timeoutMs) {
        const queued = (this._cmdChain || Promise.resolve())
            .catch(() => {})                    // shield from prior failure
            .then(() => this._sendCommandRaw(cmd, timeoutMs));
        this._cmdChain = queued.catch(() => {});
        return queued;
    }

    async _sendCommandRaw(cmd, timeoutMs) {
        if (!this.isConnected) throw new Error('Bridge not connected.');

        const fullCmd = cmd + OBD_WIFI_CR;
        this._rxBuffer = '';

        const reply = new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                this._pending = null;
                reject(new Error(`Timeout waiting for ELM reply to "${cmd}".`));
            }, timeoutMs);
            this._pending = { resolve, reject, timer, cmd };
        });

        this.ws.send(fullCmd);
        const response = await reply;
        return response.replace(cmd, '').trim();
    }

    async initialize() {
        // ── 1. Reset + basic config ──────────────────────────────────────
        const seq = [
            { cmd: 'ATD',  timeout: 1500 },   // set all defaults
            { cmd: 'ATZ',  timeout: 3000 },   // hardware reset
            { cmd: 'ATE0', timeout: 1500 },   // echo off
            { cmd: 'ATL0', timeout: 1500 },   // linefeeds off
            { cmd: 'ATH0', timeout: 1500 },   // headers off (we re-enable for Mode 06)
            { cmd: 'ATS0', timeout: 1500 },   // spaces off
            { cmd: 'ATAT1', timeout: 1500 },  // ADAPTIVE timing — speeds up reads
        ];
        const out = {};
        let anyOk = false;
        for (const step of seq) {
            try {
                out[step.cmd] = await this._sendCommand(step.cmd, step.timeout);
                this._emit('init_step', { cmd: step.cmd, response: out[step.cmd] });
                anyOk = true;
            } catch (e) {
                out[step.cmd] = `<err:${e.message}>`;
                this._emit('init_step', { cmd: step.cmd, response: out[step.cmd], failed: true });
            }
        }

        // ── 2. Protocol negotiation — exhaustive sweep with PROPER timeouts ─
        // Validated against EVERY OBD-II protocol family. The probe `0100`
        // returns "41 00 BE 7F B8 13" on success — we accept anything with
        // "41 00" after stripping whitespace. NEGATIVE markers (NO DATA, ?,
        // UNABLE, SEARCHING) are explicit failures.
        //
        // ORDER matters: cars made for the Egyptian market in 2011 (Nissan
        // Sunny, Tiida, older Hyundai/Kia) often use ISO 9141-2 K-Line, not
        // CAN. We probe both families with realistic timeouts:
        //   • CAN protocols: ~3s probe (fast init)
        //   • ISO 9141-2: ~10s probe (5-baud slow init takes ~5s alone)
        //   • KWP2000 slow: ~10s probe
        //   • KWP2000 fast: ~6s probe
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

        // ── Protocol memory — same scheme as obd_bluetooth.js. dongle_id
        // here is the bridge URL (one bridge ⇄ one Wi-Fi dongle).
        const dongleId = this.url || '';
        const sweepStart = Date.now();
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
                // SUCCESS = ECU returned a valid Mode 01 PID 0100 response.
                // Pattern: "41 00 XX XX XX XX" (header + 4 bytes of bitmask).
                // Some ELM revisions prefix slow-protocol responses with
                // "BUS INIT: OK" — that prefix is NOISE we strip, not a
                // failure. Real failures are explicit error words.
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
                    out['protocol']   = p.label;
                    out['protocol_id']= p.code;
                    out['0100']       = probe;
                    anyOk = true;
                    break;
                }
            } catch (e) {
                probes.push({ protocol: p.label, code: p.code, response: `<err:${e.message}>`, ok: false });
                this._emit('protocol_probe', { protocol: p.label, error: e.message, ok: false });
            }
        }
        out['_probes'] = probes;

        if (!chosen) {
            // Build a detailed, actionable error so the mechanic sees WHY
            // every protocol failed.
            const lines = probes.map(p =>
                `  • ${p.protocol}: ${p.ok ? 'OK' : (p.response || 'no response').slice(0, 60)}`).join('\n');
            const reason = this._lastBridgeError ||
                'الدونجل بيكلم البريدج بس مش عارف يكلم ECU العربية. ' +
                'جرّبت كل بروتوكولات OBD-II المتاحة وكلهم فشلوا. الأسباب الأرجح:\n\n' +
                '1. مفتاح السيارة مش على ON. لفّه لوضع ON (مش لازم تشغّل المحرك) واعد.\n' +
                '2. الفيشة على فيشة OBD غير الـ pins اللي السيارة بتستخدمها (نادر).\n' +
                '3. الفيشة فيها مشكلة hardware.\n\n' +
                'نتايج التجارب:\n' + lines;
            throw new Error(reason);
        }

        // Persist the successful protocol — see obd_bluetooth.js for rationale.
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

    async streamLiveData({ pids = OBD_WIFI_DEFAULT_POLL_PIDS, intervalMs = 600 } = {}) {
        if (this._streaming) return;
        this._streaming = true;

        // Only poll PIDs we actually have parsers for — protects against
        // typos in the caller and trims wasted bus time on slow K-Line.
        const valid = pids.filter(p => OBD_WIFI_PIDS[p]);

        const tick = async () => {
            if (!this._streaming || !this.isConnected) return;
            const snapshot = { _at: Date.now() };
            for (const pid of valid) {
                try {
                    let raw = await this._sendCommand(`01${pid}`, OBD_WIFI_DEFAULT_TIMEOUT_MS);
                    // The first response after protocol negotiation is often
                    // NO DATA — retry once before giving up.
                    if (raw && /NO\s*DATA/i.test(raw)) {
                        raw = await this._sendCommand(`01${pid}`, OBD_WIFI_DEFAULT_TIMEOUT_MS)
                            .catch(() => raw);
                    }
                    const parsed = this._parsePIDResponse(pid, raw);
                    if (parsed !== null && Number.isFinite(parsed)) {
                        snapshot[OBD_WIFI_PIDS[pid].label] = parsed;
                    }
                } catch (_) { /* tolerate single-PID dropouts */ }
            }
            // Only emit when we got at least one value — empty snapshots
            // make the gauges flash to idle and back.
            if (Object.keys(snapshot).length > 1) {
                this._emit('live', snapshot);
            }
            this._streamHandle = setTimeout(tick, intervalMs);
        };
        tick();
    }

    stopStream() {
        this._streaming = false;
        if (this._streamHandle) clearTimeout(this._streamHandle);
        this._streamHandle = null;
    }

    // Reads STORED (Mode 03) + PENDING (Mode 07) + PERMANENT (Mode 0A) DTCs
    // and emits a flat tagged list so the UI can show all three categories.
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
            } catch (e) {
                raws[b.mode] = `<err:${e.message}>`;
            }
        }
        // Keep the existing dtcs event shape (codes: string[]) for backwards
        // compatibility with the UI, but pass the typed list as detail.byType.
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

    async clearDTCs() {
        const raw = await this._sendCommand('04', 4000);
        this._emit('dtcs_cleared', { raw });
        return raw;
    }

    // ── Freeze Frame (Mode 02) ──────────────────────────────────────────
    // The ECU stores a snapshot of running conditions at the EXACT instant
    // a confirmed DTC was set. This is THE most valuable diagnostic tool in
    // ISTA/Xentry — knowing whether the engine was cold/hot, idle/highway,
    // open/closed loop tells the mechanic where to look. We read the same
    // PIDs as Mode 01 but prefixed with `02` and frame index `00`.
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
                const def = OBD_WIFI_PIDS[pid];
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

    // ── Readiness Monitors + I/M Readiness (Mode 01 PID 01) ─────────────
    // 4 bytes that encode: MIL state, DTC count, AND 11 self-test bits.
    // EU/Egypt vehicle inspection looks at these — all "Complete" = pass.
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

    // ── Mode 09 — ECU identification (CalID, CVN, ECU name) ─────────────
    // Goes BEYOND VIN. CalID = the calibration version, CVN = checksum of
    // the calibration software, ECU name = manufacturer ECU identifier.
    // ISTA shows these on the Vehicle Information screen.
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

    // Multi-frame Mode 09 responses on KWP2000/CAN repeat the "49 <PID> NN"
    // header at the start of EVERY frame. The ELM327 concatenates them and
    // strips the ISO-TP/KWP framing, but the application-layer headers
    // remain in the byte stream. We strip them ALL, not just the first one,
    // otherwise 0x49 bytes leak into ASCII output as 'I' characters
    // (the bug the user spotted on the BMW E60 VIN/CalID).
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

    async readVIN() {
        const raw = await this._sendCommand('0902', 5000);
        const vin = this._parseVINResponse(raw);
        if (vin) {
            this._emit('vin', { vin });
            // Link VIN to the protocol memory row we wrote at init.
            const dongleId = this.url || '';
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

    // Mode 06 — On-Board Monitoring Test Results.
    // Generic SAE J1979 reserves OBDMID $A1..$A8 for cylinder 1..8 misfire
    // counters (count over the last 10 driving cycles). The exact framing
    // depends on transport (CAN ISO 15765-4 vs ISO 9141), so we ask the
    // dongle to enable headers (ATH1) for this read only, then restore
    // ATH0 afterwards. Returns: [{cylinder, count}].
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
    // ONLY relevant on ISO 9141-2 / KWP2000 / J1850 buses. CAN cars return
    // NO DATA — they use Mode 06 instead. We probe a small set of standard
    // Test IDs across O2 sensors 1-4 and report whatever the ECU answers.
    //   TID 0x01: rich-to-lean threshold voltage
    //   TID 0x05: rich-to-lean switch time (ms)
    //   TID 0x06: lean-to-rich switch time (ms)
    //   TID 0x07: min voltage for test cycle
    //   TID 0x08: max voltage for test cycle
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
    // Bidirectional control. Standard test IDs (SAE J1979):
    //   TID 0x01: EVAP leak test — seal canister vent
    //   TID 0x02-FF: manufacturer defined
    // DESTRUCTIVE — actually commands hardware. UI must confirm before call.
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

    // ── Mode 01 PID 00 / 20 / 40 / ... — Supported PIDs bitmask ─────────
    // The first thing a serious scan tool does. Asks the ECU which Mode 01
    // PIDs it actually answers. Saves us polling for PIDs the car doesn't
    // implement (every poll on an unimplemented PID burns ~500ms of bus
    // time waiting for NO DATA on slow K-Line).
    //
    // Each base PID (00, 20, 40, 60, 80, A0, C0, E0) returns a 4-byte
    // bitmask describing the next 32 PIDs (e.g. PID 00 → PIDs 01-20).
    // Byte 3 LSB = "next bitmask supported" — we walk the chain until it
    // clears or we hit NO DATA.
    async readSupportedPIDs() {
        const supported = new Set();
        const bases = ['00', '20', '40', '60', '80', 'A0', 'C0', 'E0'];
        for (const base of bases) {
            let raw;
            try { raw = await this._sendCommand('01' + base, 3000); }
            catch (_) { break; }
            if (!raw || /NO\s*DATA|UNABLE|\?/i.test(raw)) break;
            const stripped = raw.replace(/\s+/g, '').toUpperCase();
            const m = stripped.match(new RegExp('41' + base + '([0-9A-F]{8})'));
            if (!m) break;
            const baseInt = parseInt(base, 16);
            const bytes = m[1].match(/.{2}/g).map(h => parseInt(h, 16));
            // 32 bits, MSB first; bit i.b → PID = baseInt + 1 + i*8 + (7 - b)
            for (let i = 0; i < 4; i++) {
                for (let b = 7; b >= 0; b--) {
                    if (bytes[i] & (1 << b)) {
                        const pid = baseInt + 1 + (i * 8) + (7 - b);
                        supported.add(pid.toString(16).toUpperCase().padStart(2, '0'));
                    }
                }
            }
            // LSB of last byte = is next range supported?
            if (!(bytes[3] & 0x01)) break;
        }
        const list = Array.from(supported).sort();
        this._emit('supported_pids', { pids: list, count: list.length });
        return list;
    }

    // ════════════════════════════════════════════════════════════════════
    // ── Mode 22 — UDS Read Data By Identifier (ISO 14229) ───────────────
    // ════════════════════════════════════════════════════════════════════
    // Talks to NON-engine modules (ABS, Airbag, BCM, TCM, Cluster, HVAC).
    // For each call we:
    //   1. ATSH to the target module's CAN request ID (e.g. 7E2 for ABS).
    //   2. ATCRA to filter responses to its reply ID (e.g. 7EA).
    //   3. Send `22 <DID_hi> <DID_lo>`, parse `62 <DID> <data...>`.
    //   4. Restore header to 7E0 (engine) so subsequent Mode 01 calls work.
    // Negative response `7F 22 <NRC>` means the ECU rejected — common NRCs:
    //   11=service-not-supported, 12=sub-function-not-supported,
    //   31=request-out-of-range, 7E=session-conflict, 7F=conditions-not-met.

    async readDataByIdentifier(did, { reqHeader = null, respFilter = null } = {}) {
        if (!/^[0-9A-Fa-f]{4}$/.test(did)) {
            throw new Error('UDS DID must be 4 hex chars (e.g. "F190" for VIN).');
        }
        // Mode 22 + ATSH/ATCRA assume an ISO 15765-4 CAN transport. K-Line
        // and J1850 buses don't carry UDS at the ELM level — bail early
        // with a clear message instead of letting the request fail opaquely.
        const CAN_PROTOCOLS = new Set(['6', '7', '8', '9', 'B']);
        if (this._lastProtocolCode && !CAN_PROTOCOLS.has(this._lastProtocolCode)) {
            throw new Error(
                `Mode 22 (UDS) شغّال على CAN فقط. العربية دي على ${this._lastProtocolLabel || this._lastProtocolCode} — مينفعش.`
            );
        }
        const out = { did: did.toUpperCase(), raw: null, ok: false, data: null, nrc: null };
        try {
            if (reqHeader)   { try { await this._sendCommand('ATSH' + reqHeader, 1500); } catch (_) {} }
            if (respFilter)  { try { await this._sendCommand('ATCRA' + respFilter, 1500); } catch (_) {} }

            const raw = await this._sendCommand('22' + did.toUpperCase(), 4000);
            out.raw = raw;
            const stripped = (raw || '').replace(/\s+/g, '').toUpperCase();
            const nrcMatch = stripped.match(/7F22([0-9A-F]{2})/);
            if (nrcMatch) {
                out.nrc = nrcMatch[1];
                return out;
            }
            const m = stripped.match(new RegExp('62' + did.toUpperCase() + '([0-9A-F]+)'));
            if (!m) return out;
            // Strip multi-frame headers (ISO-TP "01:", "02:", etc.)
            out.data = m[1];
            out.ok = true;
        } finally {
            // Always restore engine header so Mode 01 streams aren't broken.
            try { await this._sendCommand('ATCRA', 1500); } catch (_) {}
            try { await this._sendCommand('ATSH7E0', 1500); } catch (_) {}
        }
        return out;
    }

    // Probe ISO 14229 standard DIDs (F186-F19D) on any module. Useful to
    // tell the mechanic "Yes, this ABS module is alive and here's its
    // software version" before requesting proprietary data.
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

    // ── UDS Mode 0x2E — Write Data By Identifier (ISO 14229) ────────────
    // The destructive counterpart of Mode 0x22. Required for things like
    // BMW battery registration. The ECU usually demands an extended
    // diagnostic session first (Mode 0x10 sub-function 03) and may need
    // security access (Mode 0x27) for protected DIDs. ALWAYS gate calls
    // behind explicit user confirmation — a bad write can brick a module.
    async writeDataByIdentifier(did, dataHex, { reqHeader = null, respFilter = null } = {}) {
        if (!/^[0-9A-Fa-f]{4}$/.test(did)) {
            throw new Error('UDS DID must be 4 hex chars.');
        }
        if (!/^[0-9A-Fa-f]+$/.test(dataHex) || dataHex.length % 2 !== 0) {
            throw new Error('Data must be even-length hex.');
        }
        const CAN_PROTOCOLS = new Set(['6', '7', '8', '9', 'B']);
        if (this._lastProtocolCode && !CAN_PROTOCOLS.has(this._lastProtocolCode)) {
            throw new Error(`Mode 2E (UDS write) شغّال على CAN فقط.`);
        }
        const out = { did: did.toUpperCase(), ok: false, raw: null, nrc: null };
        try {
            if (reqHeader)  { try { await this._sendCommand('ATSH' + reqHeader, 1500); } catch (_) {} }
            if (respFilter) { try { await this._sendCommand('ATCRA' + respFilter, 1500); } catch (_) {} }
            const payload = '2E' + did.toUpperCase() + dataHex.toUpperCase();
            const raw = await this._sendCommand(payload, 5000);
            out.raw = raw;
            const stripped = (raw || '').replace(/\s+/g, '').toUpperCase();
            const nrc = stripped.match(/7F2E([0-9A-F]{2})/);
            if (nrc) { out.nrc = nrc[1]; return out; }
            // Positive response: 6E <DID>
            if (stripped.includes('6E' + did.toUpperCase())) out.ok = true;
        } finally {
            try { await this._sendCommand('ATCRA', 1500); } catch (_) {}
            try { await this._sendCommand('ATSH7E0', 1500); } catch (_) {}
        }
        this._emit('uds_write', out);
        return out;
    }

    // ── UDS Mode 0x10 — Diagnostic Session Control ──────────────────────
    // 01 = default, 02 = programming, 03 = extendedDiagnostic.
    // Most write/relearn flows need 03 because the default session
    // refuses everything except plain reads.
    async setDiagnosticSession(sub, { reqHeader = null } = {}) {
        if (!/^[0-9A-Fa-f]{2}$/.test(sub)) {
            throw new Error('Session sub-function must be 2 hex chars (e.g. "03").');
        }
        try {
            if (reqHeader) { try { await this._sendCommand('ATSH' + reqHeader, 1500); } catch (_) {} }
            const raw = await this._sendCommand('10' + sub.toUpperCase(), 3000);
            return { ok: /^50/.test(raw.replace(/\s+/g, '').toUpperCase()), raw };
        } finally {
            try { await this._sendCommand('ATSH7E0', 1500); } catch (_) {}
        }
    }

    // ════════════════════════════════════════════════════════════════════
    // ── Battery & Charging Health (BCH) ─────────────────────────────────
    // ════════════════════════════════════════════════════════════════════
    // PID 42 (control_voltage) is the ECU's read of battery/charging
    // system voltage. It's NOT a direct battery measurement — it's
    // measured AT the ECU, after fuses/wiring drop. Still: it's the only
    // reliable voltage signal we have without a multimeter, and the
    // delta between phases (off → cranking → idle → rev) is highly
    // diagnostic.

    // Read a single voltage sample, retrying once if NO DATA.
    async _readBatteryVoltage(timeoutMs = 2500) {
        let raw;
        try { raw = await this._sendCommand('0142', timeoutMs); }
        catch (_) { return null; }
        if (!raw || /NO\s*DATA|UNABLE|\?/i.test(raw)) {
            try { raw = await this._sendCommand('0142', timeoutMs); } catch (_) {}
            if (!raw || /NO\s*DATA|UNABLE|\?/i.test(raw)) return null;
        }
        const stripped = raw.replace(/\s+/g, '').toUpperCase();
        const m = stripped.match(/4142([0-9A-F]{4})/);
        if (!m) return null;
        const raw16 = parseInt(m[1], 16);
        return raw16 / 1000;
    }

    // Sample voltage over `durationMs` and return { min, max, mean, samples }.
    // Used by the cranking phase (we need the dip) and parasitic draw test.
    async sampleBatteryVoltage(durationMs = 3000, intervalMs = 100) {
        const samples = [];
        const end = Date.now() + durationMs;
        while (Date.now() < end) {
            const v = await this._readBatteryVoltage(1500);
            if (v != null) samples.push({ t: Date.now(), v });
            await new Promise(r => setTimeout(r, intervalMs));
        }
        if (!samples.length) return { samples: [], min: null, max: null, mean: null };
        const vs = samples.map(s => s.v);
        return {
            samples,
            min:  Math.min(...vs),
            max:  Math.max(...vs),
            mean: vs.reduce((a, b) => a + b, 0) / vs.length,
        };
    }

    // Multi-phase battery & charging health test. The UI drives this — it
    // calls runBatteryChargingTest({phase: 'rest'}) first, then prompts
    // the mechanic to crank, then idle, then rev. Each phase returns its
    // voltage + verdict; the UI accumulates a full report.
    async runBatteryChargingTest({ phase = 'rest', durationMs = null } = {}) {
        const phaseDurations = {
            rest:          1500,
            crank:         4000,   // capture the dip during a full crank
            idle_charging: 3000,
            rev_charging:  3000,
        };
        const dur = durationMs || phaseDurations[phase] || 2000;
        const verdictFn = (typeof window !== 'undefined' && window.verdictForPhase)
            ? window.verdictForPhase : (() => ({ level: 'unknown', message: '' }));

        const data = await this.sampleBatteryVoltage(dur, 100);
        if (!data.samples.length) {
            const out = { phase, voltage: null, verdict: { level: 'unknown', message: 'لا توجد قراءة من الـ ECU.' } };
            this._emit('battery_phase', out);
            return out;
        }
        // For the cranking phase, the lowest dip is the diagnostic signal.
        // For everything else, the mean is what matters.
        const voltage = phase === 'crank' ? data.min : data.mean;
        const verdict = verdictFn(phase, voltage);
        const out = { phase, voltage, min: data.min, max: data.max, mean: data.mean,
                      sample_count: data.samples.length, verdict };
        this._emit('battery_phase', out);
        return out;
    }

    // ════════════════════════════════════════════════════════════════════
    // ── Adaptation / Relearn Procedure Runner ───────────────────────────
    // ════════════════════════════════════════════════════════════════════
    // Walks a procedure from ADAPTATION_PROCEDURES step-by-step. Manual
    // steps just emit events the UI renders as instructions; automated
    // steps (clear/wait/session/write) actually fire commands. The UI is
    // responsible for prompting the mechanic to confirm before each
    // destructive step (write) and for collecting any data_input values
    // (e.g. battery capacity for BMW registration).
    async runAdaptationStep(step, { onPromptForData = null } = {}) {
        const out = { type: step.type, ok: false, message: '' };
        try {
            if (step.type === 'manual') {
                out.ok = true; out.message = step.text || '';
            } else if (step.type === 'wait') {
                await new Promise(r => setTimeout(r, (step.seconds || 1) * 1000));
                out.ok = true;
            } else if (step.type === 'clear') {
                const raw = await this._sendCommand('04', 4000);
                out.ok = !/NO\s*DATA|UNABLE|7F\s*04/i.test(raw || '');
                out.raw = raw;
            } else if (step.type === 'session') {
                const r = await this.setDiagnosticSession(step.session || '03');
                out.ok = r.ok; out.raw = r.raw;
            } else if (step.type === 'write') {
                let data = step.data;
                if (step.data_input && onPromptForData) {
                    data = await onPromptForData(step.data_input, step);
                }
                if (!data || /^TBD_/.test(data)) {
                    throw new Error('قيمة الكتابة مش متوفّرة — الـ UI لازم يجمعها من الميكانيكي.');
                }
                const r = await this.writeDataByIdentifier(step.did, data);
                out.ok = r.ok; out.raw = r.raw; out.nrc = r.nrc;
            } else {
                throw new Error(`نوع خطوة غير معروف: ${step.type}`);
            }
        } catch (e) { out.message = e.message; }
        this._emit('adaptation_step', { step, result: out });
        return out;
    }

    // ════════════════════════════════════════════════════════════════════
    // ── Adapter & ECU capability probe ──────────────────────────────────
    // ════════════════════════════════════════════════════════════════════
    // Runs once on connect to tell the technician up-front:
    //   • Which OBD modes the ECU answers
    //   • Whether the bus supports Mode 22 (manufacturer DIDs)
    //   • Whether per-cylinder misfire is going to work
    //   • A recommended adapter if their current one is too limited
    //
    // No measurement requests — only existence checks. Total time ~3s.

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

    // ════════════════════════════════════════════════════════════════════
    // ── Cascading Misfire Detection (3-tier fallback) ───────────────────
    // ════════════════════════════════════════════════════════════════════
    // Standard Mode 06 OBDMID $A1-$A8 covers ~50% of cars on the road.
    // For the rest we cascade:
    //   1. Manufacturer DIDs (BMW/Mercedes/Toyota/Ford/GM)  — covers ~40% more
    //   2. RPM variance analysis at idle                    — covers everything
    //
    // Each tier annotates the result with `method` so the UI can show the
    // technician how the data was obtained.

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
        const brand = detectManufacturerFromVIN(vin);
        const misfireDIDs = MISFIRE_DIDS[brand] || [];
        if (misfireDIDs.length) {
            const setup = MANUFACTURER_SETUP[brand] || {};
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

    /**
     * Sample idle RPM at ~10 Hz for 20 seconds, then look for cyclical
     * drops that match cylinder firing intervals. Returns a per-cylinder
     * "suspicion score" (NOT an exact count — the math can only estimate).
     */
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

    // ════════════════════════════════════════════════════════════════════
    // SENSOR HEALTH DIAGNOSTICS — competitor parity (Carista / Torque / OBD11)
    // ════════════════════════════════════════════════════════════════════
    //
    // Each method samples standardised OBD-II PIDs over time, computes the
    // signal vs an expected/theoretical baseline, and returns a verdict the
    // mechanic can act on. Engine displacement (in litres) is needed for
    // MAF math — defaults to 2.0L which covers most 4-cyl sedans. Pass
    // { displacement: 3.0 } for V6s, { displacement: 4.4 } for E60 V8, etc.

    // ── MAF Sensor Health ───────────────────────────────────────────────
    // Computes expected MAF (g/s) from the speed-density formula:
    //   m_dot = (RPM/2) × (MAP × disp_L / (R × T)) × VE × air_density
    // Simplified for OBD-II:
    //   expected = (RPM × MAP × disp_L × VE) / (120 × R × T_K)
    // For a typical engine at idle ~3-5 g/s, at WOT ~30-80 g/s depending
    // on displacement. If actual is < 70% of expected → MAF dirty/failing.
    async measureMAFHealth({ displacement = 2.0, samples = 5 } = {}) {
        const readings = [];
        for (let i = 0; i < samples; i++) {
            const snap = {};
            for (const pid of ['0C', '0B', '05', '0F', '10', '04', '11']) {
                try {
                    const raw = await this._sendCommand(`01${pid}`, 2000);
                    const v = this._parsePIDResponse(pid, raw);
                    if (v !== null && Number.isFinite(v)) {
                        snap[OBD_WIFI_PIDS[pid].label] = v;
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

    // ── O2 + Catalyst Efficiency ────────────────────────────────────────
    // Samples upstream (B1S1) and downstream (B1S2) O2 voltages over ~12s.
    // Healthy upstream: oscillates rapidly 0.1V ↔ 0.9V (>= 1 Hz at idle).
    // Healthy downstream after good cat: nearly flat ~0.6-0.7V.
    // Failing cat: downstream mimics upstream (both oscillate).
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

    // ── Idle Stability + TPS Sanity + Battery State ────────────────────
    // Samples RPM over 20s — std deviation > 80 rpm at idle = rough idle
    // (vacuum leak, weak coil, dirty injector, EGR stuck open).
    // Also reads PID 0x11 (absolute TP) vs PID 0x45 (relative TP) — if they
    // diverge > 8%, the TPS module is out of sync (needs adaptation reset).
    // Battery: PID 0x42 classified by zone.
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

    // ── Emissions Health: AFR (wideband O2) + EGR + EVAP ────────────────
    // Three of the most expensive diagnoses in any workshop:
    //   • AFR drift     → wrong fuel mixture (rich/lean) → cat damage + DTC P017X
    //   • EGR stuck     → P0401/P0402/P0404 → emissions fail + reduced power
    //   • EVAP leak     → P0442/P0455/P0456 → emissions fail (canister/purge)
    //
    // We avoid the 12-second O2 oscillation sweep here (that's covered in
    // measureO2AndCatalyst). Instead this is a focused snapshot of EMISSIONS
    // sub-systems — runs in ~6 seconds.
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
                            evap[OBD_WIFI_PIDS[pid].label] = v;
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

    // ── Vehicle Health Score (0-100) ─────────────────────────────────────
    // Combines four signals into a single number a non-technician can read:
    //   • stored DTC count                (-25 max, -8 per code)
    //   • |fuel trim sum| > thresholds    (-20 max)
    //   • misfire total over 10 cycles    (-25 max)
    //   • O2 sensor voltage health        (-15 max — flat lambda = bad cat)
    // Plus per-cylinder balance and a verdict string in Arabic.
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

    // One-shot fuel-system snapshot the mechanic can read at a glance:
    // fuel pressure, fuel rate, both O2 voltages, both bank fuel trims.
    // Useful to decide between a weak fuel pump vs a clogged injector vs
    // a failing lambda sensor when the engine is misfiring or running rich.
    async readFuelSystemHealth() {
        const pids = ['06', '07', '08', '09', '0A', '0B', '0E', '14', '15', '22', '23', '33', '5E'];
        const snapshot = { _at: Date.now() };
        for (const pid of pids) {
            try {
                const raw = await this._sendCommand('01' + pid, 2500);
                const v = this._parsePIDResponse(pid, raw);
                if (v !== null) {
                    const def = OBD_WIFI_PIDS[pid];
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

    // ── parsers (identical semantics to BLE driver) ─────────────────────
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
        try { return OBD_WIFI_PIDS[pid].parse(bytes); } catch { return null; }
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
        // Strip line numbering ("0:", "1:") + whitespace + ALL Mode 09 PID 02
        // frame headers ("4902XX"). Without stripping every header, the 0x49
        // bytes from subsequent frames pollute the VIN as 'I' characters.
        const stripped = raw
            .replace(/\d+\s*:/g, '')
            .replace(/\s+/g, '')
            .toUpperCase();
        if (stripped.indexOf('4902') < 0) return null;
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

    // ════════════════════════════════════════════════════════════════════
    // ── Mode 22 — Read Data By Identifier (UDS / manufacturer DIDs) ─────
    // ════════════════════════════════════════════════════════════════════
    // Standard OBD-II (Mode 01) exposes ~150 PIDs that every car must
    // support. Each OEM exposes thousands more via Mode 22 (UDS ISO 14229
    // service 0x22) — oil quality, DPF soot mass, cam phaser angle, trans
    // fluid temp, adaptation tables, etc. These are the SAME values that
    // Carista / OBD11 / VagCom read; they're not secret, just per-brand.
    //
    // Request:   "22 HH LL"          (DID = 2 bytes hex, e.g. 4204 = BMW oil temp)
    // Response:  "62 HH LL DD DD..." (62 = 22+0x40, DID echoed, then data)
    // Negative:  "7F 22 NN"          (NN = NRC; we silently skip these)

    /**
     * Read one Mode 22 DID. Returns parsed value or null.
     * @param {string} did      — 4-hex string "4204"
     * @param {function(bytes):number|string|object} parser
     * @param {object} opts
     *    .ecuHeader   — optional "ATSH XXXXXXXX" to target a specific ECU
     *    .timeoutMs   — default 2500
     */
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

    /**
     * Sweep all known DIDs for the detected manufacturer.
     * Emits `manufacturer_dids` event with the result.
     * @param {string|null} vin — used to auto-detect WMI. If null, uses lastVIN/UI hint.
     */
    async scanManufacturerDIDs(vin = null) {
        const brand = detectManufacturerFromVIN(vin) || 'generic';
        const lib = MANUFACTURER_DIDS[brand] || [];
        const setup = MANUFACTURER_SETUP[brand] || {};

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

    _emit(type, detail) {
        this.dispatchEvent(new CustomEvent(type, { detail }));
    }
}

// ─── Manufacturer detection from VIN (WMI = first 3 chars) ──────────────
function detectManufacturerFromVIN(vin) {
    if (!vin || typeof vin !== 'string' || vin.length < 3) return null;
    const wmi = vin.slice(0, 3).toUpperCase();
    const wmi2 = vin.slice(0, 2).toUpperCase();
    // Common WMIs (not exhaustive — covers ~95% of Egyptian market).
    const map = {
        // BMW (Germany + Spartanburg + Mexico)
        WBA:'bmw', WBS:'bmw', WBY:'bmw', WBX:'bmw',
        '4US':'bmw', '5UX':'bmw', '5YM':'bmw', '5UM':'bmw',
        // Mercedes
        WDB:'mercedes', WDC:'mercedes', WDD:'mercedes', WDF:'mercedes',
        '4JG':'mercedes', '55S':'mercedes',
        // VW
        WVW:'vag', WV1:'vag', WV2:'vag', '3VW':'vag', '1VW':'vag',
        // Audi
        WAU:'vag', WA1:'vag', TRU:'vag',
        // Toyota / Lexus
        JT2:'toyota', JT3:'toyota', JT4:'toyota', JT6:'toyota', JTJ:'toyota',
        '4T1':'toyota', '4T3':'toyota', '5TD':'toyota', '5TF':'toyota',
        '2T1':'toyota', JTH:'toyota',
        // Hyundai
        KMH:'hyundai', '5NM':'hyundai', KM8:'hyundai', '5NP':'hyundai',
        // Kia
        KNA:'kia', KND:'kia', '5XX':'kia', KNH:'kia',
        // Ford
        '1FA':'ford', '1FB':'ford', '1FC':'ford', '1FD':'ford', '1FT':'ford',
        '2FA':'ford', '3FA':'ford',
        // GM / Chevrolet
        '1G1':'gm', '1G6':'gm', '2G1':'gm', '3G1':'gm', KL1:'gm', KL8:'gm',
    };
    return map[wmi] || ({ JT:'toyota', '4T':'toyota', '5T':'toyota',
                         KM:'hyundai', KN:'kia',
                         '1G':'gm', '2G':'gm',
                         '1F':'ford', '2F':'ford' })[wmi2] || null;
}

// ─── Per-brand UDS framing (ECU headers + response addresses) ───────────
// Most OEMs reserve specific 11-bit / 29-bit IDs for diagnostic addressing.
// Without the correct ATSH + ATCRA pair the ECU silently ignores Mode 22
// frames — exactly the "ECU مردش على أي DID" symptom.
const MANUFACTURER_SETUP = {
    bmw: {
        // BMW Diagnostic CAN-bus convention: tester=0x6F1, ECU+0x600 = reply.
        // DME (engine)        request 6F1 → reply 612 (target byte 12)
        // EGS (transmission)  request 6F1 → reply 618 (target byte 18)
        // FRM (front module)  request 6F1 → reply 640
        // KOMBI (cluster)     request 6F1 → reply 660
        // Each request prepends the target byte: e.g. "12 03 22 42 04".
        // The ELM327 wraps in ISO-TP for us. We just need ATSH + ATCRA.
        ecus: [
            { name: 'DME',   header: '6F1', cra: '612' },
            { name: 'EGS',   header: '6F1', cra: '618' },
            { name: 'KOMBI', header: '6F1', cra: '660' },
        ],
    },
    mercedes: {
        // 7E0 = OBD-II physical request, 7E8 = response.
        ecus: [{ name: 'ME', header: '7E0', cra: '7E8' }],
    },
    vag: {
        // VW/Audi UDS: 7E0/7E8 for engine, 7E1/7E9 for trans.
        ecus: [
            { name: 'ENGINE', header: '7E0', cra: '7E8' },
            { name: 'TRANS',  header: '7E1', cra: '7E9' },
        ],
    },
    toyota: {
        ecus: [
            { name: 'ENGINE', header: '7E0', cra: '7E8' },
            { name: 'TRANS',  header: '7E1', cra: '7E9' },
            { name: 'HYBRID', header: '7E2', cra: '7EA' },
        ],
    },
    hyundai: { ecus: [{ name: 'ENGINE', header: '7E0', cra: '7E8' }] },
    kia:     { ecus: [{ name: 'ENGINE', header: '7E0', cra: '7E8' }] },
    ford:    { ecus: [{ name: 'PCM',    header: '7E0', cra: '7E8' }] },
    gm:      { ecus: [{ name: 'PCM',    header: '7E0', cra: '7E8' }] },
    generic: { ecus: [{ name: 'default', header: null, cra: null }] },
};

// ─── Manufacturer DID library ───────────────────────────────────────────
// Each entry: { did, label, label_ar, unit, parse(bytes), health?, ecuHeader? }
//   parse() returns a number/string/null
//   health() returns 'good' | 'warn' | 'bad' | null
//
// These DIDs are publicly documented (BimmerCode, VCDS wiki, OBDeleven KB,
// Toyota Techstream PIDs list). They are READ-ONLY — no actuation/coding.
const MANUFACTURER_DIDS = {
    // ── BMW (E/F/G series, DME + EGS + KOMBI) ───────────────────────────
    // DIDs sourced from BimmerCode public KB + ISTA workshop manual.
    // Read-only — no actuation. Health thresholds tuned for petrol N-series.
    bmw: [
        // Engine — temperatures & oil
        { did: '4204', label: 'Engine oil temperature', label_ar: '🛢️ حرارة الزيت',
          unit: '°C',
          parse: b => b.length >= 2 ? (((b[0] << 8) + b[1]) / 100) - 50 : null,
          health: v => v > 130 ? 'bad' : (v > 115 ? 'warn' : 'good') },
        { did: '4936', label: 'Engine oil level', label_ar: '🛢️ مستوى الزيت',
          unit: 'mm',
          parse: b => b.length >= 1 ? b[0] : null,
          health: v => v < 5 ? 'bad' : (v < 10 ? 'warn' : 'good') },
        { did: '4214', label: 'Coolant temperature', label_ar: '🌡️ حرارة الماء',
          unit: '°C',
          parse: b => b.length >= 2 ? (((b[0] << 8) + b[1]) / 100) - 50 : null,
          health: v => v > 110 ? 'bad' : (v > 100 ? 'warn' : 'good') },
        { did: '4264', label: 'Fuel pressure (HPFP)', label_ar: '⛽ ضغط البنزين العالي',
          unit: 'bar',
          parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 10 : null,
          health: v => (v > 50 && v < 200) ? 'good' : 'warn' },

        // Battery & charging
        { did: '4911', label: 'Battery voltage', label_ar: '🔋 فولت البطارية',
          unit: 'V',
          parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 1000 : null,
          health: v => v < 11.8 ? 'bad' : (v < 12.4 ? 'warn' : 'good') },
        { did: '4914', label: 'Battery SoC', label_ar: '🔋 شحن البطارية',
          unit: '%',
          parse: b => b.length >= 1 ? b[0] : null,
          health: v => v < 40 ? 'bad' : (v < 65 ? 'warn' : 'good') },
        { did: '4915', label: 'Battery SoH', label_ar: '🔋 صحة البطارية',
          unit: '%',
          parse: b => b.length >= 1 ? b[0] : null,
          health: v => v < 60 ? 'bad' : (v < 80 ? 'warn' : 'good') },

        // Mixture & emissions
        { did: 'DD01', label: 'Ethanol content', label_ar: '⛽ نسبة الإيثانول',
          unit: '%',
          parse: b => b.length >= 1 ? b[0] : null },
        { did: '405A', label: 'MAF actual', label_ar: '🌬️ إيرماس فعلي',
          unit: 'kg/h',
          parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 10 : null },

        // Transmission (EGS — accessed via ECU 18)
        { did: '4F4A', label: 'ATF temperature', label_ar: '⚙️ حرارة زيت الجير',
          unit: '°C',
          parse: b => b.length >= 1 ? b[0] - 50 : null,
          health: v => v > 110 ? 'bad' : (v > 95 ? 'warn' : 'good') },
        { did: '4F4B', label: 'Trans input speed', label_ar: '⚙️ سرعة دخل الجير',
          unit: 'rpm',
          parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },

        // Cluster (KOMBI — via ECU 60)
        { did: '5050', label: 'Mileage', label_ar: '🛣️ العدّاد',
          unit: 'km',
          parse: b => b.length >= 4
              ? (b[0] << 24) + (b[1] << 16) + (b[2] << 8) + b[3] : null },
        { did: '5072', label: 'Service distance', label_ar: '🔧 المتبقي للصيانة',
          unit: 'km',
          parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null,
          health: v => v < 1000 ? 'warn' : 'good' },
    ],

    // ── Mercedes (powertrain CAN, header 7E0) ───────────────────────────
    mercedes: [
        { did: '4F00', label: 'Engine oil level', label_ar: 'مستوى الزيت',
          unit: 'mm',
          parse: b => b.length >= 1 ? b[0] : null,
          health: v => v < 5 ? 'bad' : (v < 10 ? 'warn' : 'good') },
        { did: '4F01', label: 'Oil temperature', label_ar: 'حرارة الزيت',
          unit: '°C',
          parse: b => b.length >= 2 ? (((b[0] << 8) + b[1]) / 10) - 40 : null,
          health: v => v > 130 ? 'bad' : (v > 115 ? 'warn' : 'good') },
        { did: 'F18A', label: 'ECU serial number', label_ar: 'رقم الـ ECU',
          unit: '',
          parse: b => b.map(x => x >= 0x20 && x <= 0x7E ? String.fromCharCode(x) : '').join('') },
    ],

    // ── VAG (VW / Audi / Skoda / Seat — KWP2000 + UDS) ──────────────────
    vag: [
        { did: '1011', label: 'Engine oil temp', label_ar: 'حرارة الزيت',
          unit: '°C',
          parse: b => b.length >= 1 ? b[0] - 40 : null,
          health: v => v > 130 ? 'bad' : (v > 115 ? 'warn' : 'good') },
        { did: '02A1', label: 'DPF soot mass', label_ar: 'كتلة السخام في الـ DPF',
          unit: 'g',
          parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 100 : null,
          health: v => v > 24 ? 'bad' : (v > 18 ? 'warn' : 'good') },
        { did: '02A2', label: 'DPF distance since regen',
          label_ar: 'كم منذ آخر تنظيف DPF', unit: 'km',
          parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null,
          health: v => v > 1000 ? 'warn' : 'good' },
        { did: '22F1', label: 'Adblue level', label_ar: 'مستوى الـ AdBlue',
          unit: '%',
          parse: b => b.length >= 1 ? b[0] : null,
          health: v => v < 10 ? 'bad' : (v < 25 ? 'warn' : 'good') },
    ],

    // ── Toyota / Lexus (CAN, header 7E0, also uses Mode 21 historically) ─
    toyota: [
        { did: '0140', label: 'A/T fluid temperature', label_ar: 'حرارة زيت الجير',
          unit: '°C',
          parse: b => b.length >= 1 ? b[0] - 40 : null,
          health: v => v > 110 ? 'bad' : (v > 95 ? 'warn' : 'good') },
        { did: '0142', label: 'A/T input speed', label_ar: 'سرعة دخل الجير',
          unit: 'rpm',
          parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 4 : null },
        { did: '014C', label: 'Hybrid battery SoC', label_ar: 'شحن بطارية الهايبرد',
          unit: '%',
          parse: b => b.length >= 1 ? b[0] / 2 : null,
          health: v => v < 30 ? 'bad' : (v < 50 ? 'warn' : 'good') },
    ],

    // ── Hyundai / Kia ───────────────────────────────────────────────────
    hyundai: [
        { did: '0101', label: 'Engine oil temperature', label_ar: 'حرارة الزيت',
          unit: '°C',
          parse: b => b.length >= 1 ? b[0] - 40 : null,
          health: v => v > 130 ? 'bad' : (v > 115 ? 'warn' : 'good') },
        { did: '0110', label: 'Battery voltage', label_ar: 'فولت البطارية',
          unit: 'V',
          parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 1000 : null,
          health: v => v < 11.8 ? 'bad' : (v < 12.4 ? 'warn' : 'good') },
    ],
    kia: [
        { did: '0101', label: 'Engine oil temperature', label_ar: 'حرارة الزيت',
          unit: '°C',
          parse: b => b.length >= 1 ? b[0] - 40 : null,
          health: v => v > 130 ? 'bad' : (v > 115 ? 'warn' : 'good') },
        { did: '0110', label: 'Battery voltage', label_ar: 'فولت البطارية',
          unit: 'V',
          parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 1000 : null,
          health: v => v < 11.8 ? 'bad' : (v < 12.4 ? 'warn' : 'good') },
    ],

    // ── Ford (UDS, mostly post-2008) ────────────────────────────────────
    ford: [
        { did: '110E', label: 'Trans fluid temperature', label_ar: 'حرارة زيت الجير',
          unit: '°C',
          parse: b => b.length >= 1 ? b[0] - 40 : null,
          health: v => v > 110 ? 'bad' : (v > 95 ? 'warn' : 'good') },
        { did: '1505', label: 'Battery voltage', label_ar: 'فولت البطارية',
          unit: 'V',
          parse: b => b.length >= 2 ? ((b[0] << 8) + b[1]) / 1000 : null,
          health: v => v < 11.8 ? 'bad' : (v < 12.4 ? 'warn' : 'good') },
    ],

    // ── GM (UDS) ────────────────────────────────────────────────────────
    gm: [
        { did: '125A', label: 'Trans fluid temperature', label_ar: 'حرارة زيت الجير',
          unit: '°C',
          parse: b => b.length >= 1 ? b[0] - 40 : null,
          health: v => v > 110 ? 'bad' : (v > 95 ? 'warn' : 'good') },
    ],

    // ── Generic (try OBD-II standard DIDs that some ECUs expose via 22) ─
    generic: [
        { did: 'F190', label: 'VIN (UDS DID)', label_ar: 'VIN عبر UDS',
          unit: '',
          parse: b => b.map(x => x >= 0x20 && x <= 0x7E ? String.fromCharCode(x) : '').join('') },
        { did: 'F195', label: 'ECU SW version', label_ar: 'نسخة برنامج الـ ECU',
          unit: '',
          parse: b => b.map(x => x >= 0x20 && x <= 0x7E ? String.fromCharCode(x) : '').join('') },
    ],
};

// ─── Per-manufacturer MISFIRE DIDs (fallback when Mode 06 fails) ────────
// Each entry: { cyl: 1-8, did: '4540', parse: bytes → count }
// Sources: BimmerCode public KB, Mercedes Star Diagnosis docs,
// Toyota Techstream PIDs list, Ford FORScan community.
const MISFIRE_DIDS = {
    bmw: [
        // DME DIDs 0x4540-0x4547 = cylinder 1-8 misfire counter (last 1000 revs)
        { cyl: 1, did: '4540', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 2, did: '4541', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 3, did: '4542', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 4, did: '4543', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 5, did: '4544', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 6, did: '4545', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 7, did: '4546', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 8, did: '4547', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
    ],
    mercedes: [
        // ME ECU DIDs 0x4601-0x4608 (M271/M272/M273/M278 families)
        { cyl: 1, did: '4601', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 2, did: '4602', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 3, did: '4603', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 4, did: '4604', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 5, did: '4605', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 6, did: '4606', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 7, did: '4607', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 8, did: '4608', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
    ],
    toyota: [
        // Techstream LID range 0x1A03-0x1A0A (engine misfire counts)
        { cyl: 1, did: '1A03', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 2, did: '1A04', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 3, did: '1A05', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 4, did: '1A06', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 5, did: '1A07', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 6, did: '1A08', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 7, did: '1A09', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 8, did: '1A0A', parse: b => b.length >= 1 ? b[0] : null },
    ],
    ford: [
        // PCM DIDs 0x1010-0x1018 (FORScan-validated, EcoBoost & Coyote)
        { cyl: 1, did: '1011', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 2, did: '1012', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 3, did: '1013', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 4, did: '1014', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 5, did: '1015', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 6, did: '1016', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 7, did: '1017', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
        { cyl: 8, did: '1018', parse: b => b.length >= 2 ? (b[0] << 8) + b[1] : null },
    ],
    gm: [
        // PCM DIDs 0x1240-0x1247 (LS/LT family — Tech2 documented)
        { cyl: 1, did: '1240', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 2, did: '1241', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 3, did: '1242', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 4, did: '1243', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 5, did: '1244', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 6, did: '1245', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 7, did: '1246', parse: b => b.length >= 1 ? b[0] : null },
        { cyl: 8, did: '1247', parse: b => b.length >= 1 ? b[0] : null },
    ],
};

// Expose for unit tests / external introspection.
if (typeof window !== 'undefined') {
    window.__MANUFACTURER_DIDS = MANUFACTURER_DIDS;
    window.__MISFIRE_DIDS = MISFIRE_DIDS;
    window.__MANUFACTURER_SETUP = MANUFACTURER_SETUP;
    window.__detectManufacturerFromVIN = detectManufacturerFromVIN;
}

window.OBDWiFi = OBDWiFi;
