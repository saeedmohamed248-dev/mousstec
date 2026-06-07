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
const OBD_WIFI_DEFAULT_POLL_PIDS = [
    '0C', '0D', '05', '11', '04', '42',          // الأساسي (UI gauges الحالية)
    '06', '07', '0E', '14', '0A', '5E',          // fuel trim + O2 + fuel pressure + fuel rate
];

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
        const seq = ['ATZ', 'ATE0', 'ATL0', 'ATH0', 'ATS0'];
        const out = {};
        let anyOk = false;
        for (const cmd of seq) {
            try {
                out[cmd] = await this._sendCommand(cmd, OBD_WIFI_DEFAULT_TIMEOUT_MS);
                anyOk = true;
            } catch (e) { out[cmd] = `<err:${e.message}>`; }
        }

        // Protocol negotiation — try auto first, then explicit CAN 11/500
        // (covers 99% of post-2008 cars), then 29-bit CAN, then ISO 9141.
        // Each attempt is validated by querying PID 0100 (supported PIDs).
        // The first protocol that returns non-empty data wins.
        const protocols = [
            { code: '0', label: 'auto'        },
            { code: '6', label: 'CAN 11/500'  },
            { code: '7', label: 'CAN 29/500'  },
            { code: '3', label: 'ISO 9141-2'  },
            { code: '5', label: 'KWP2000 fast'},
        ];
        let chosen = null;
        for (const p of protocols) {
            try {
                await this._sendCommand('ATSP' + p.code, 2000);
                const probe = await this._sendCommand('0100', 5000);
                if (probe && !probe.includes('NO DATA') && !probe.includes('?') &&
                    !probe.includes('UNABLE') && /41\s*00/i.test(probe.replace(/\s/g, ''))) {
                    chosen = p;
                    out['protocol']   = p.label;
                    out['0100']       = probe;
                    out['ATSP_chose'] = p.code;
                    anyOk = true;
                    break;
                }
            } catch (_) { /* try next */ }
        }

        if (!chosen) {
            const reason = this._lastBridgeError ||
                'الدونجل بيكلم البريدج بس مش عارف يكلم ECU العربية. ' +
                'الأرجح إن مفتاح السيارة مش على ON — لفّ المفتاح لوضع ON ' +
                '(بدون تشغيل المحرك) واعد المحاولة.';
            throw new Error(reason);
        }
        this._emit('initialized', out);
        return out;
    }

    async streamLiveData({ pids = OBD_WIFI_DEFAULT_POLL_PIDS, intervalMs = 800 } = {}) {
        if (this._streaming) return;
        this._streaming = true;

        const tick = async () => {
            if (!this._streaming || !this.isConnected) return;
            const snapshot = { _at: Date.now() };
            for (const pid of pids) {
                try {
                    const raw = await this._sendCommand(`01${pid}`, OBD_WIFI_DEFAULT_TIMEOUT_MS);
                    const parsed = this._parsePIDResponse(pid, raw);
                    if (parsed !== null) snapshot[OBD_WIFI_PIDS[pid].label] = parsed;
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
        const stripped = raw
            .replace(/\d+\s*:/g, '')
            .replace(/\s+/g, '')
            .toUpperCase();
        const idx = stripped.indexOf('490201');
        if (idx < 0) return null;
        const body = stripped.slice(idx + 6);
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
