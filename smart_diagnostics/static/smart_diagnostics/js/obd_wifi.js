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
    '0B': { label: 'intake_manifold_kpa', unit: 'kPa',  parse: b => b[0] },
    '0C': { label: 'rpm',                 unit: 'rpm',  parse: b => ((b[0] << 8) + b[1]) / 4 },
    '0D': { label: 'speed_kph',           unit: 'km/h', parse: b => b[0] },
    '0E': { label: 'timing_advance_deg',  unit: '°',    parse: b => (b[0] / 2) - 64 },
    '0F': { label: 'intake_temp_c',       unit: '°C',   parse: b => b[0] - 40 },
    '10': { label: 'maf_gs',              unit: 'g/s',  parse: b => ((b[0] << 8) + b[1]) / 100 },
    '06': { label: 'stft_b1',             unit: '%',    parse: b => (b[0] - 128) * 100 / 128 },
    '07': { label: 'ltft_b1',             unit: '%',    parse: b => (b[0] - 128) * 100 / 128 },
    '08': { label: 'stft_b2',             unit: '%',    parse: b => (b[0] - 128) * 100 / 128 },
    '09': { label: 'ltft_b2',             unit: '%',    parse: b => (b[0] - 128) * 100 / 128 },
    '0A': { label: 'fuel_pressure_kpa',   unit: 'kPa',  parse: b => b[0] * 3 },
    '0B': { label: 'map_kpa',             unit: 'kPa',  parse: b => b[0] },
    '0E': { label: 'timing_advance_deg',  unit: '°',    parse: b => (b[0] / 2) - 64 },
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
            { code: '3', label: 'ISO 9141-2 (K-Line)',     probeMs: 12000 },
            { code: '5', label: 'KWP2000 fast init',       probeMs: 7000  },
            { code: '4', label: 'KWP2000 5-baud init',     probeMs: 12000 },
            { code: '1', label: 'SAE J1850 PWM',           probeMs: 5000  },
            { code: '2', label: 'SAE J1850 VPW',           probeMs: 5000  },
        ];

        let chosen = null;
        const probes = [];
        for (const p of protocols) {
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
                'جرّبت 10 بروتوكولات مختلفة كلهم فشلوا. الأسباب الأرجح:\n\n' +
                '1. مفتاح السيارة مش على ON. لفّه لوضع ON (مش لازم تشغّل المحرك) واعد.\n' +
                '2. الفيشة على فيشة OBD غير الـ pins اللي السيارة بتستخدمها (نادر).\n' +
                '3. الفيشة فيها مشكلة hardware.\n\n' +
                'نتايج التجارب:\n' + lines;
            throw new Error(reason);
        }

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
        if (vin) this._emit('vin', { vin });
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
        // Quick verdict (مبدئي — لمساعدة الميكانيكي مش بديل عن الفحص اليدوي)
        const trims = ['stft_b1', 'ltft_b1', 'stft_b2', 'ltft_b2']
            .map(k => snapshot[k] && snapshot[k].value).filter(v => v !== undefined);
        const totalTrim = trims.reduce((a, b) => a + b, 0);
        let verdict = 'طبيعي';
        if (totalTrim > 15)  verdict = 'العربية بتسحب بنزين زيادة (lean) — احتمال شفط هواء أو طلمبة ضعيفة';
        if (totalTrim < -15) verdict = 'العربية بتحرق بنزين زيادة (rich) — احتمال إنجكتر مكهرب أو حساس O2 تعبان';
        snapshot._verdict = verdict;
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

    _emit(type, detail) {
        this.dispatchEvent(new CustomEvent(type, { detail }));
    }
}

window.OBDWiFi = OBDWiFi;
