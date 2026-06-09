/**
 * Offline queue for the Diagnostics Room.
 *
 * Wraps the network calls that persist a diagnostic session (save / attach-to-
 * invoice). If the request fails because the device is offline (or the server
 * returns 5xx), the payload is stored in IndexedDB and replayed automatically
 * when the connection comes back.
 *
 * Each queued request carries an `idempotency_key` so the backend ignores a
 * duplicate that was already accepted (e.g. the network came back AFTER the
 * server received the first attempt but BEFORE the client got the 201).
 *
 * Exports a single global: `window.DiagOfflineQueue`.
 *   .send(url, payload, opts)   → tries fetch, falls back to queue
 *   .flush()                    → manually drain the queue
 *   .pendingCount()             → number of items waiting
 *   .onChange(cb)               → subscribe to queue-size changes
 */
(function () {
    'use strict';

    const DB_NAME    = 'diag_offline_v1';
    const STORE      = 'pending_requests';
    const DB_VERSION = 1;
    const RETRY_MS   = 30 * 1000;       // background poll while online
    const MAX_ATTEMPTS = 50;            // give up after this many retries

    // ── UUID v4 (RFC 4122, browser-safe) ────────────────────────────────
    function uuidv4() {
        if (crypto && crypto.randomUUID) return crypto.randomUUID();
        const b = crypto.getRandomValues(new Uint8Array(16));
        b[6] = (b[6] & 0x0f) | 0x40;
        b[8] = (b[8] & 0x3f) | 0x80;
        const h = [...b].map(x => x.toString(16).padStart(2, '0'));
        return `${h.slice(0,4).join('')}-${h.slice(4,6).join('')}-` +
               `${h.slice(6,8).join('')}-${h.slice(8,10).join('')}-` +
               `${h.slice(10,16).join('')}`;
    }

    // ── IndexedDB wrapper ───────────────────────────────────────────────
    let _dbPromise = null;
    function openDB() {
        if (_dbPromise) return _dbPromise;
        _dbPromise = new Promise((resolve, reject) => {
            const req = indexedDB.open(DB_NAME, DB_VERSION);
            req.onupgradeneeded = () => {
                const db = req.result;
                if (!db.objectStoreNames.contains(STORE)) {
                    db.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true });
                }
            };
            req.onsuccess = () => resolve(req.result);
            req.onerror   = () => reject(req.error);
        });
        return _dbPromise;
    }

    async function _tx(mode) {
        const db = await openDB();
        return db.transaction(STORE, mode).objectStore(STORE);
    }

    async function putItem(item) {
        const store = await _tx('readwrite');
        return new Promise((res, rej) => {
            const r = store.add(item);
            r.onsuccess = () => res(r.result);
            r.onerror   = () => rej(r.error);
        });
    }

    async function allItems() {
        const store = await _tx('readonly');
        return new Promise((res, rej) => {
            const r = store.getAll();
            r.onsuccess = () => res(r.result || []);
            r.onerror   = () => rej(r.error);
        });
    }

    async function deleteItem(id) {
        const store = await _tx('readwrite');
        return new Promise((res, rej) => {
            const r = store.delete(id);
            r.onsuccess = () => res();
            r.onerror   = () => rej(r.error);
        });
    }

    async function updateItem(item) {
        const store = await _tx('readwrite');
        return new Promise((res, rej) => {
            const r = store.put(item);
            r.onsuccess = () => res();
            r.onerror   = () => rej(r.error);
        });
    }

    // ── Subscriber pattern for queue-size badge ─────────────────────────
    const _subs = new Set();
    function _notify(n) { _subs.forEach(cb => { try { cb(n); } catch (_) {} }); }

    async function _broadcast() {
        try {
            const items = await allItems();
            _notify(items.length);
        } catch (_) { /* IDB unavailable — silent */ }
    }

    // ── Core: try the request; queue on failure ─────────────────────────
    /**
     * @param {string} url
     * @param {object} payload      — will be merged with idempotency_key
     * @param {object} opts
     *    .csrf      — CSRF token (required)
     *    .label     — short human label for diagnostics ("save", "attach")
     *    .onQueued  — called(item) if the request had to be queued
     * @returns {Promise<{ok, queued, data?, error?}>}
     */
    async function send(url, payload, opts = {}) {
        const csrf  = opts.csrf;
        const label = opts.label || 'request';
        const idem  = payload.idempotency_key || uuidv4();
        const body  = { ...payload, idempotency_key: idem };

        // If we know we're offline, skip the fetch entirely.
        if (typeof navigator !== 'undefined' && navigator.onLine === false) {
            const id = await putItem({
                url, body, csrf, label,
                idempotency_key: idem,
                created_at: Date.now(),
                attempts: 0,
            });
            await _broadcast();
            if (opts.onQueued) opts.onQueued({ id, reason: 'offline' });
            return { ok: false, queued: true, idempotency_key: idem };
        }

        try {
            const resp = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrf,
                },
                body: JSON.stringify(body),
            });

            // 5xx → treat as retryable. 4xx → permanent error, do NOT queue.
            if (resp.status >= 500) {
                const id = await putItem({
                    url, body, csrf, label,
                    idempotency_key: idem,
                    created_at: Date.now(),
                    attempts: 1,
                });
                await _broadcast();
                if (opts.onQueued) opts.onQueued({ id, reason: 'server_5xx' });
                return { ok: false, queued: true, idempotency_key: idem };
            }

            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                return { ok: false, queued: false, error: data, status: resp.status };
            }
            return { ok: true, queued: false, data };

        } catch (err) {
            // Network failure (DNS, TCP, CORS-pre-flight loss). Queue it.
            const id = await putItem({
                url, body, csrf, label,
                idempotency_key: idem,
                created_at: Date.now(),
                attempts: 0,
            });
            await _broadcast();
            if (opts.onQueued) opts.onQueued({ id, reason: 'network', error: err.message });
            return { ok: false, queued: true, idempotency_key: idem };
        }
    }

    // ── Drain the queue (best-effort) ────────────────────────────────────
    let _flushing = false;
    async function flush() {
        if (_flushing) return { ok: true, busy: true };
        _flushing = true;
        try {
            const items = await allItems();
            let sent = 0, dropped = 0, kept = 0;

            for (const it of items) {
                if (typeof navigator !== 'undefined' && navigator.onLine === false) {
                    kept += 1;
                    continue;
                }
                try {
                    const resp = await fetch(it.url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': it.csrf,
                        },
                        body: JSON.stringify(it.body),
                    });
                    if (resp.ok || (resp.status >= 400 && resp.status < 500)) {
                        // Success OR permanent error → don't keep retrying.
                        await deleteItem(it.id);
                        if (resp.ok) sent += 1; else dropped += 1;
                    } else {
                        it.attempts = (it.attempts || 0) + 1;
                        if (it.attempts >= MAX_ATTEMPTS) {
                            await deleteItem(it.id);
                            dropped += 1;
                        } else {
                            await updateItem(it);
                            kept += 1;
                        }
                    }
                } catch (_) {
                    it.attempts = (it.attempts || 0) + 1;
                    if (it.attempts >= MAX_ATTEMPTS) {
                        await deleteItem(it.id);
                        dropped += 1;
                    } else {
                        await updateItem(it);
                        kept += 1;
                    }
                    // If we hit a network error mid-flush, stop trying to
                    // avoid hammering — wait for the next 'online' event or
                    // the periodic poll.
                    break;
                }
            }

            await _broadcast();
            return { ok: true, sent, dropped, kept };
        } finally {
            _flushing = false;
        }
    }

    async function pendingCount() {
        try { return (await allItems()).length; } catch (_) { return 0; }
    }

    function onChange(cb) {
        _subs.add(cb);
        _broadcast();
        return () => _subs.delete(cb);
    }

    // ── Auto-flush triggers ─────────────────────────────────────────────
    if (typeof window !== 'undefined') {
        window.addEventListener('online', () => { flush(); });
        // Periodic safety net while online (handles partial connectivity).
        setInterval(() => {
            if (typeof navigator !== 'undefined' && navigator.onLine !== false) {
                flush();
            }
        }, RETRY_MS);
        // Try once on load in case items survived a tab restart.
        setTimeout(() => { _broadcast(); flush(); }, 1500);
    }

    // ── Export ──────────────────────────────────────────────────────────
    window.DiagOfflineQueue = {
        send, flush, pendingCount, onChange, uuidv4,
    };
})();
