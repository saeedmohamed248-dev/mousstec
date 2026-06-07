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
    '0C': { label: 'rpm',             unit: 'rpm',  parse: b => ((b[0] << 8) + b[1]) / 4 },
    '0D': { label: 'speed_kph',       unit: 'km/h', parse: b => b[0] },
    '0F': { label: 'intake_temp_c',   unit: '°C',   parse: b => b[0] - 40 },
    '10': { label: 'maf_gs',          unit: 'g/s',  parse: b => ((b[0] << 8) + b[1]) / 100 },
    '11': { label: 'throttle_pct',    unit: '%',    parse: b => b[0] * 100 / 255 },
    '2F': { label: 'fuel_level_pct',  unit: '%',    parse: b => b[0] * 100 / 255 },
    '42': { label: 'control_voltage', unit: 'V',    parse: b => ((b[0] << 8) + b[1]) / 1000 },
    '5C': { label: 'oil_temp_c',      unit: '°C',   parse: b => b[0] - 40 },
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

        this._emit('connected', {
            service:   this.service.uuid,
            writeChar: this.writeChar.uuid,
            notifyChar: this.notifyChar.uuid,
            writeWithoutResponse: this._writeWithoutResponse,
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

    // 3. ELM327 chip init.
    async initialize() {
        const seq = ['ATZ', 'ATE0', 'ATL0', 'ATH0', 'ATS0', 'ATSP0'];
        const out = {};
        for (const cmd of seq) {
            try { out[cmd] = await this._sendCommand(cmd, DEFAULT_TIMEOUT_MS); }
            catch (e) { out[cmd] = `<err:${e.message}>`; }
        }
        // Warm the auto-protocol probe; first 0100 negotiates the bus.
        try { out['0100'] = await this._sendCommand('0100', 4000); }
        catch (e) { out['0100'] = `<err:${e.message}>`; }
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

    // 5. Mode 03 — read stored DTCs.
    async readDTCs() {
        const raw = await this._sendCommand('03', 4000);
        const codes = this._parseDTCResponse(raw);
        this._emit('dtcs', { codes, raw });
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
        if (vin) this._emit('vin', { vin });
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
    async _sendCommand(cmd, timeoutMs) {
        if (!this.writeChar) throw new Error('Not connected.');
        if (this._pending) throw new Error('Another command in flight.');

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

// Browser exports — picked up by the Diagnostics Room template.
window.OBDBluetooth = OBDBluetooth;
window.humanizeBluetoothError = humanizeBluetoothError;
window.__OBD_TEST__ = { PIDS, KNOWN_SERVICE_UUIDS, SERVICE_BLOCKLIST, PAIRING_FILTERS };
