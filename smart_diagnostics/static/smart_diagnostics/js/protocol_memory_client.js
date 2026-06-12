/* ============================================================
 * protocol_memory_client.js
 * ============================================================
 *
 * Thin wrapper around the /api/diagnostics/protocol-memory/
 * endpoints. The OBD drivers call lookup() before starting a
 * protocol sweep and save() after one succeeds — that's how a
 * returning vehicle skips the 30-second exhaustive scan.
 *
 * All calls are fire-and-forget where possible: if the network
 * is down or the user is offline, we fall back to a full sweep
 * silently. Never throw out of these helpers — the diagnostics
 * room must keep working without the cache.
 */

(function () {
    const BASE = '/api/diagnostics/protocol-memory/';

    function _csrf() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : '';
    }

    /** GET { found, protocol_code, protocol_label, hit_count } | { found: false }
     *  Resolves to null on any error — caller falls back to full sweep.
     */
    async function lookupProtocolMemory({ vin = '', dongleId = '' } = {}) {
        if (!vin && !dongleId) return null;
        const qs = new URLSearchParams();
        if (vin) qs.set('vin', vin);
        if (dongleId) qs.set('dongle_id', dongleId);
        try {
            const r = await fetch(BASE + '?' + qs.toString(), {
                credentials: 'same-origin',
                headers: { 'Accept': 'application/json' },
            });
            if (!r.ok) return null;
            const body = await r.json();
            return body && body.found ? body : null;
        } catch (_) {
            return null;
        }
    }

    /** POST a successful protocol so next session skips the sweep.
     *  Fire-and-forget — never throws. */
    async function saveProtocolMemory({ vin = '', dongleId = '', code, label = '', sweepSecondsSaved = 0 } = {}) {
        if (!code) return null;
        if (!vin && !dongleId) return null;
        try {
            const r = await fetch(BASE + 'save/', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': _csrf(),
                },
                body: JSON.stringify({
                    vin, dongle_id: dongleId,
                    protocol_code: code,
                    protocol_label: label,
                    sweep_seconds_saved: sweepSecondsSaved,
                }),
            });
            if (!r.ok) return null;
            return await r.json();
        } catch (_) {
            return null;
        }
    }

    /** Reorder a protocol-sweep list to try `preferredCode` first.
     *  Pure helper — no I/O. Used by both drivers. */
    function reorderProtocols(protocols, preferredCode) {
        if (!preferredCode) return protocols.slice();
        const code = String(preferredCode).toUpperCase();
        const idx = protocols.findIndex(p => p.code === code);
        if (idx < 0) return protocols.slice();
        const head = protocols[idx];
        const tail = protocols.filter((_, i) => i !== idx);
        return [head, ...tail];
    }

    window.ProtocolMemoryClient = {
        lookup: lookupProtocolMemory,
        save: saveProtocolMemory,
        reorder: reorderProtocols,
    };
})();
