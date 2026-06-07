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
    '04': { label: 'engine_load',     unit: '%',    parse: b => b[0] * 100 / 255 },
    '05': { label: 'coolant_temp_c',  unit: '°C',   parse: b => b[0] - 40 },
    '0C': { label: 'rpm',             unit: 'rpm',  parse: b => ((b[0] << 8) + b[1]) / 4 },
    '0D': { label: 'speed_kph',       unit: 'km/h', parse: b => b[0] },
    '0F': { label: 'intake_temp_c',   unit: '°C',   parse: b => b[0] - 40 },
    '10': { label: 'maf_gs',          unit: 'g/s',  parse: b => ((b[0] << 8) + b[1]) / 100 },
    '11': { label: 'throttle_pct',    unit: '%',    parse: b => b[0] * 100 / 255 },
    '2F': { label: 'fuel_level_pct',  unit: '%',    parse: b => b[0] * 100 / 255 },
    '42': { label: 'control_voltage', unit: 'V',    parse: b => ((b[0] << 8) + b[1]) / 1000 },
    '5C': { label: 'oil_temp_c',      unit: '°C',   parse: b => b[0] - 40 },
};
const OBD_WIFI_DEFAULT_POLL_PIDS = ['0C', '0D', '05', '11', '04', '42'];

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
        if (this._pending) {
            const { reject, timer } = this._pending;
            this._pending = null;
            clearTimeout(timer);
            reject(new Error('انقطع الاتصال بالجسر.'));
        }
        this._emit('disconnected', { transport: 'wifi' });
    }

    _onMessage(ev) {
        const txt = typeof ev.data === 'string'
            ? ev.data
            : new TextDecoder().decode(ev.data);

        // Bridge can prepend an error frame on TCP-connect failure.
        if (txt.startsWith('BRIDGE_ERROR')) {
            if (this._pending) {
                const { reject, timer } = this._pending;
                this._pending = null;
                clearTimeout(timer);
                reject(new Error(txt.replace(/[\r>]/g, '').trim()));
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
    async _sendCommand(cmd, timeoutMs) {
        if (!this.isConnected) throw new Error('Bridge not connected.');
        if (this._pending) throw new Error('Another command in flight.');

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
        const seq = ['ATZ', 'ATE0', 'ATL0', 'ATH0', 'ATS0', 'ATSP0'];
        const out = {};
        for (const cmd of seq) {
            try { out[cmd] = await this._sendCommand(cmd, OBD_WIFI_DEFAULT_TIMEOUT_MS); }
            catch (e) { out[cmd] = `<err:${e.message}>`; }
        }
        try { out['0100'] = await this._sendCommand('0100', 4000); }
        catch (e) { out['0100'] = `<err:${e.message}>`; }
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

    async readDTCs() {
        const raw = await this._sendCommand('03', 4000);
        const codes = this._parseDTCResponse(raw);
        this._emit('dtcs', { codes, raw });
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
