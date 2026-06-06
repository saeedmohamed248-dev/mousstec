/* ============================================================
 * obd_bluetooth.js — Web Bluetooth driver for ELM327 dongles
 * ============================================================
 *
 * Talks to a generic ELM327 OBD-II Bluetooth Low-Energy adapter from
 * the browser. Built for the Diagnostics Room (غرفة تشخيص الأعطال).
 *
 * Supported flow:
 *   1. requestDevice()   → user picks the dongle from the chooser
 *   2. connect()         → resolves the Nordic UART-style service
 *                          (Service UUID 0000fff0… RX 0000fff1 / TX 0000fff2
 *                          is the most common ELM327-BLE clone layout)
 *   3. initialize()      → ATZ, ATE0, ATL0, ATSP0 (auto protocol)
 *   4. streamLiveData()  → polls a configurable PID set every N ms
 *   5. readDTCs()        → Mode 03 → parses raw P/C/B/U codes
 *
 * Browser support: Chromium-based browsers + HTTPS context. Safari and
 * Firefox do NOT ship Web Bluetooth — the UI must surface that gracefully.
 *
 * Public API: import OBDBluetooth from this module; everything else is
 * an internal helper exposed for unit testing under window.__OBD_TEST__.
 *
 * NOTE on raw-byte assumption: most ELM327-BLE clones expose the UART as
 * NOTIFY+WRITE characteristics that pass ASCII through unchanged, framed
 * by '\r' (CR). A small minority use the Nordic UART UUIDs 6e400001-…
 * The driver probes BOTH service families before giving up.
 */

const PRIMARY_SERVICE_UUIDS = [
    '0000fff0-0000-1000-8000-00805f9b34fb',           // Common ELM327 BLE clones
    '6e400001-b5a3-f393-e0a9-e50e24dcca9e',           // Nordic UART Service (NUS)
    '0000ffe0-0000-1000-8000-00805f9b34fb',           // HM-10 / CC2541 modules
];

// Per-service { write, notify } characteristic UUIDs.
const SERVICE_CHARS = {
    '0000fff0-0000-1000-8000-00805f9b34fb': {
        write:  '0000fff2-0000-1000-8000-00805f9b34fb',
        notify: '0000fff1-0000-1000-8000-00805f9b34fb',
    },
    '6e400001-b5a3-f393-e0a9-e50e24dcca9e': {
        write:  '6e400002-b5a3-f393-e0a9-e50e24dcca9e',
        notify: '6e400003-b5a3-f393-e0a9-e50e24dcca9e',
    },
    '0000ffe0-0000-1000-8000-00805f9b34fb': {
        write:  '0000ffe1-0000-1000-8000-00805f9b34fb',
        notify: '0000ffe1-0000-1000-8000-00805f9b34fb',
    },
};

const PROMPT_BYTE = '>';
const CR = '\r';
const DEFAULT_TIMEOUT_MS = 2500;

// ── Mode 01 PIDs we stream by default ─────────────────────────────────
// Each entry: { pid, label, unit, parse(bytes[]) }. parse() receives the
// hex byte array AFTER the 41 XX header (ELM327 strips the request mode
// echo when we use 'ATH0' implicitly).
const PIDS = {
    '04': { label: 'engine_load',        unit: '%',
            parse: b => b[0] * 100 / 255 },
    '05': { label: 'coolant_temp_c',     unit: '°C',
            parse: b => b[0] - 40 },
    '0C': { label: 'rpm',                unit: 'rpm',
            parse: b => ((b[0] << 8) + b[1]) / 4 },
    '0D': { label: 'speed_kph',          unit: 'km/h',
            parse: b => b[0] },
    '0F': { label: 'intake_temp_c',      unit: '°C',
            parse: b => b[0] - 40 },
    '10': { label: 'maf_gs',             unit: 'g/s',
            parse: b => ((b[0] << 8) + b[1]) / 100 },
    '11': { label: 'throttle_pct',       unit: '%',
            parse: b => b[0] * 100 / 255 },
    '2F': { label: 'fuel_level_pct',     unit: '%',
            parse: b => b[0] * 100 / 255 },
    '42': { label: 'control_voltage',    unit: 'V',
            parse: b => ((b[0] << 8) + b[1]) / 1000 },
    '5C': { label: 'oil_temp_c',         unit: '°C',
            parse: b => b[0] - 40 },
};

const DEFAULT_POLL_PIDS = ['0C', '0D', '05', '11', '04', '42'];

// ── Public driver ─────────────────────────────────────────────────────
class OBDBluetooth extends EventTarget {
    constructor() {
        super();
        this.device = null;
        this.server = null;
        this.writeChar = null;
        this.notifyChar = null;
        this._rxBuffer = '';
        this._pending = null;       // { resolve, reject, timer }
        this._streaming = false;
        this._streamHandle = null;
    }

    get isConnected() {
        return !!(this.device && this.device.gatt && this.device.gatt.connected);
    }

    // 1. Pairing — must be called from a user gesture (button click).
    async requestDevice() {
        if (!('bluetooth' in navigator)) {
            throw new Error('Web Bluetooth not supported. Use Chrome / Edge on HTTPS.');
        }
        this.device = await navigator.bluetooth.requestDevice({
            // Show every BLE device — ELM327 clones advertise inconsistent names.
            acceptAllDevices: true,
            optionalServices: PRIMARY_SERVICE_UUIDS,
        });
        this.device.addEventListener('gattserverdisconnected', () => {
            this._emit('disconnected', {});
            this._streaming = false;
        });
        return { name: this.device.name || 'OBD', id: this.device.id };
    }

    // 2. Connect + locate UART characteristics.
    async connect() {
        if (!this.device) throw new Error('Call requestDevice() first.');
        this.server = await this.device.gatt.connect();

        let chosen = null;
        for (const uuid of PRIMARY_SERVICE_UUIDS) {
            try {
                const svc = await this.server.getPrimaryService(uuid);
                const map = SERVICE_CHARS[uuid];
                this.writeChar  = await svc.getCharacteristic(map.write);
                this.notifyChar = await svc.getCharacteristic(map.notify);
                chosen = uuid;
                break;
            } catch (_) { /* try next family */ }
        }
        if (!chosen) throw new Error('No compatible ELM327 service found on device.');

        await this.notifyChar.startNotifications();
        this.notifyChar.addEventListener(
            'characteristicvaluechanged', this._onNotify.bind(this),
        );
        this._emit('connected', { service: chosen });
        return chosen;
    }

    // 3. Initialise the ELM327 chip.
    async initialize() {
        // Reset → echo off → linefeeds off → headers off → spaces off → auto-protocol
        const seq = ['ATZ', 'ATE0', 'ATL0', 'ATH0', 'ATS0', 'ATSP0'];
        const out = {};
        for (const cmd of seq) {
            out[cmd] = await this._sendCommand(cmd, DEFAULT_TIMEOUT_MS);
        }
        // 0100 = "tell me what PIDs you support"; warming the auto-protocol probe
        out['0100'] = await this._sendCommand('0100', 4000);
        this._emit('initialized', out);
        return out;
    }

    // 4. Live data — polls every `intervalMs` until stopStream().
    async streamLiveData({ pids = DEFAULT_POLL_PIDS, intervalMs = 600 } = {}) {
        if (this._streaming) return;
        this._streaming = true;

        const tick = async () => {
            if (!this._streaming || !this.isConnected) return;
            const snapshot = {};
            for (const pid of pids) {
                try {
                    const raw = await this._sendCommand(`01${pid}`, DEFAULT_TIMEOUT_MS);
                    const parsed = this._parsePIDResponse(pid, raw);
                    if (parsed !== null) snapshot[PIDS[pid].label] = parsed;
                } catch (_) { /* tolerate single-PID drop */ }
            }
            snapshot._at = Date.now();
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

    // 5. Mode 03 — read stored DTCs.
    async readDTCs() {
        const raw = await this._sendCommand('03', 4000);
        const codes = this._parseDTCResponse(raw);
        this._emit('dtcs', { codes, raw });
        return codes;
    }

    // 6. Mode 04 — clear DTCs (DESTRUCTIVE; UI must confirm).
    async clearDTCs() {
        const raw = await this._sendCommand('04', 4000);
        this._emit('dtcs_cleared', { raw });
        return raw;
    }

    // 7. Mode 09 PID 02 — vehicle VIN.
    //    Response is multi-frame: ELM returns several lines like
    //    "014\r0: 49 02 01 57 42 41\r1: 56 41 31 30 32 31 33\r2: 34 35 36 37 38 39 00 00"
    //    We strip frame-index nibbles, find the 49 02 01 header, take the
    //    17 ASCII bytes, drop NULs / 0x00 padding.
    async readVIN() {
        // ATSP0 + Mode09 sometimes needs a slightly longer timeout the first
        // time the bus negotiates. 5s is conservative.
        const raw = await this._sendCommand('0902', 5000);
        const vin = this._parseVINResponse(raw);
        if (vin) this._emit('vin', { vin });
        return vin;
    }

    _parseVINResponse(raw) {
        if (!raw || raw.includes('NO DATA') || raw.includes('?')) return null;
        // Strip line numbering (e.g. "0:", "1:", "2:") and whitespace.
        const stripped = raw
            .replace(/\d+\s*:/g, '')
            .replace(/\s+/g, '')
            .toUpperCase();
        const idx = stripped.indexOf('490201');
        if (idx < 0) return null;
        const body = stripped.slice(idx + 6);  // after "49 02 01"
        let vin = '';
        for (let i = 0; i + 2 <= body.length && vin.length < 17; i += 2) {
            const b = parseInt(body.slice(i, i + 2), 16);
            if (!Number.isFinite(b) || b === 0x00) continue;  // skip pad nulls
            if (b < 0x20 || b > 0x7E) continue;               // non-printable
            vin += String.fromCharCode(b);
        }
        return vin.length === 17 ? vin : null;
    }

    async disconnect() {
        this.stopStream();
        if (this.device && this.device.gatt && this.device.gatt.connected) {
            this.device.gatt.disconnect();
        }
    }

    // ── internal: AT/OBD command transport ───────────────────────────
    async _sendCommand(cmd, timeoutMs) {
        if (!this.writeChar) throw new Error('Not connected.');
        if (this._pending) throw new Error('Another command in flight.');

        const fullCmd = cmd + CR;
        const encoder = new TextEncoder();
        this._rxBuffer = '';

        const reply = new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                this._pending = null;
                reject(new Error(`Timeout: ${cmd}`));
            }, timeoutMs);
            this._pending = { resolve, reject, timer };
        });

        await this.writeChar.writeValue(encoder.encode(fullCmd));
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

    // ── internal: ELM-response parsing ───────────────────────────────
    _parsePIDResponse(pid, raw) {
        // Expect "41 <PID> <byte0> <byte1> …" — but we set ATS0 (no spaces)
        // and ATH0 (no headers), so it's "41<PID><bytes>".
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
        // Mode 03 response: "43<N><code1><code2>…" where each code is 2 bytes.
        if (!raw || raw.includes('NO DATA')) return [];
        const stripped = raw.replace(/\s+/g, '').toUpperCase();
        const idx = stripped.indexOf('43');
        if (idx < 0) return [];
        let cursor = stripped.slice(idx + 2);
        // Some adapters insert a count nibble; skip if odd-aligned junk
        if (cursor.length % 4 !== 0) cursor = cursor.slice(2);
        const codes = [];
        for (let i = 0; i + 4 <= cursor.length; i += 4) {
            const hi = parseInt(cursor.slice(i, i + 2), 16);
            const lo = parseInt(cursor.slice(i + 2, i + 4), 16);
            if (hi === 0 && lo === 0) continue;  // padding
            codes.push(this._decodeDTCNibbles(hi, lo));
        }
        return codes;
    }

    _decodeDTCNibbles(hi, lo) {
        const sysIdx = (hi >> 6) & 0b11;     // 00=P 01=C 10=B 11=U
        const prefix = ['P', 'C', 'B', 'U'][sysIdx];
        const d1 = (hi >> 4) & 0b11;          // 0-3
        const d2 = hi & 0b1111;
        const d3 = (lo >> 4) & 0b1111;
        const d4 = lo & 0b1111;
        return `${prefix}${d1}${d2.toString(16).toUpperCase()}` +
               `${d3.toString(16).toUpperCase()}${d4.toString(16).toUpperCase()}`;
    }

    _emit(type, detail) {
        this.dispatchEvent(new CustomEvent(type, { detail }));
    }
}

// Browser export — picked up by the Diagnostics Room template.
window.OBDBluetooth = OBDBluetooth;
window.__OBD_TEST__ = { PIDS, SERVICE_CHARS, PRIMARY_SERVICE_UUIDS };
