/* ============================================================
 *  Mouss Tec PWA — Client Bootstrapper
 *    1. Registers /sw.js
 *    2. Detects waiting SW   → "New version" toast
 *    3. Captures A2HS prompt → custom "Install App" banner
 *
 *  Include once in <head> (or just before </body>) of base.html:
 *      <script src="/static/js/pwa-init.js" defer></script>
 * ============================================================ */
(function () {
    'use strict';

    /* ---------- 0. CSS (injected once) ---------- */
    const css = `
    .mt-pwa-toast,.mt-pwa-install{position:fixed;right:16px;
      z-index:99999;font-family:'Cairo','Segoe UI',Tahoma,sans-serif;direction:rtl;
      background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);color:#f1f5f9;
      border:1px solid rgba(139,92,246,.35);border-radius:14px;
      box-shadow:0 18px 40px rgba(0,0,0,.45),0 0 0 1px rgba(255,255,255,.04);
      padding:14px 18px;display:flex;align-items:center;gap:14px;
      max-width:min(90vw,360px);width:auto;opacity:0;pointer-events:none;
      transition:transform .35s cubic-bezier(.2,.8,.2,1),opacity .35s ease;}
    .mt-pwa-toast{bottom:24px;transform:translateY(40px);}
    .mt-pwa-install{bottom:24px;transform:translateY(40px);}
    .mt-pwa-toast.show,.mt-pwa-install.show{opacity:1;pointer-events:auto;transform:translateY(0);}
    .mt-pwa-toast .mt-msg,.mt-pwa-install .mt-msg{font-size:14px;font-weight:600;line-height:1.5;}
    .mt-pwa-toast .mt-msg small,.mt-pwa-install .mt-msg small{display:block;font-size:11px;color:#94a3b8;font-weight:400;margin-top:2px;}
    .mt-pwa-btn{background:linear-gradient(135deg,#8b5cf6,#6366f1);color:#fff;border:none;
      padding:9px 18px;border-radius:10px;font-weight:700;font-size:13px;cursor:pointer;
      font-family:inherit;transition:transform .15s,box-shadow .2s;white-space:nowrap;}
    .mt-pwa-btn:hover{transform:translateY(-1px);box-shadow:0 8px 20px rgba(139,92,246,.4);}
    .mt-pwa-close{background:transparent;color:#64748b;border:none;font-size:18px;cursor:pointer;
      padding:4px 8px;border-radius:8px;transition:color .2s,background .2s;}
    .mt-pwa-close:hover{color:#f1f5f9;background:rgba(255,255,255,.05);}
    .mt-pwa-icon{font-size:22px;}`;
    const style = document.createElement('style');
    style.id = 'mt-pwa-style';
    style.textContent = css;
    document.head.appendChild(style);

    /* ---------- Helpers ---------- */
    function buildToast({ icon, title, sub, btnLabel, onClick, klass }) {
        const wrap = document.createElement('div');
        wrap.className = klass;
        wrap.innerHTML =
            `<span class="mt-pwa-icon">${icon}</span>` +
            `<span class="mt-msg">${title}<small>${sub || ''}</small></span>` +
            `<button class="mt-pwa-btn" type="button">${btnLabel}</button>` +
            `<button class="mt-pwa-close" type="button" aria-label="إغلاق">&times;</button>`;
        document.body.appendChild(wrap);
        const [btn, close] = wrap.querySelectorAll('button');
        btn.addEventListener('click', onClick);
        close.addEventListener('click', () => wrap.classList.remove('show'));
        requestAnimationFrame(() => wrap.classList.add('show'));
        return wrap;
    }

    /* ============================================================
     *  1 + 2.  Service Worker registration + update prompt
     * ============================================================ */
    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/sw.js', { scope: '/' })
                .then((reg) => {
                    // If an update was already waiting when we registered
                    if (reg.waiting && navigator.serviceWorker.controller) {
                        showUpdateToast(reg.waiting);
                    }
                    // Listen for new installs
                    reg.addEventListener('updatefound', () => {
                        const newSW = reg.installing;
                        if (!newSW) return;
                        newSW.addEventListener('statechange', () => {
                            if (newSW.state === 'installed' && navigator.serviceWorker.controller) {
                                showUpdateToast(newSW);
                            }
                        });
                    });
                })
                .catch((err) => console.warn('[PWA] SW registration failed:', err));

            // Reload exactly once when the new SW takes control
            let refreshing = false;
            navigator.serviceWorker.addEventListener('controllerchange', () => {
                if (refreshing) return;
                refreshing = true;
                window.location.reload();
            });
        });
    }

    function showUpdateToast(waitingSW) {
        if (document.querySelector('.mt-pwa-toast')) return;
        buildToast({
            klass:    'mt-pwa-toast',
            icon:     '⚡',
            title:    'إصدار جديد متاح',
            sub:      'A new version is available — click to update',
            btnLabel: 'تحديث الآن',
            onClick:  () => waitingSW.postMessage({ type: 'SKIP_WAITING' }),
        });
    }

    /* ============================================================
     *  3.  Custom A2HS (Add-to-Home-Screen) banner
     * ============================================================ */
    let deferredPrompt = null;
    const DISMISS_KEY = 'mt_pwa_install_dismissed_at';
    const DISMISS_TTL = 1000 * 60 * 60 * 24 * 7; // hide for 7 days after dismiss

    function recentlyDismissed() {
        const t = parseInt(localStorage.getItem(DISMISS_KEY) || '0', 10);
        return t && (Date.now() - t) < DISMISS_TTL;
    }

    window.addEventListener('beforeinstallprompt', (e) => {
        e.preventDefault();
        deferredPrompt = e;
        if (recentlyDismissed()) return;
        if (window.matchMedia('(display-mode: standalone)').matches) return; // already installed
        const banner = buildToast({
            klass:    'mt-pwa-install',
            icon:     '📲',
            title:    'ثبّت تطبيق Mouss Tec',
            sub:      'Install Mouss Tec App for a better experience',
            btnLabel: 'تثبيت',
            onClick:  async () => {
                if (!deferredPrompt) return;
                deferredPrompt.prompt();
                const choice = await deferredPrompt.userChoice;
                deferredPrompt = null;
                banner.classList.remove('show');
                if (choice && choice.outcome === 'dismissed') {
                    localStorage.setItem(DISMISS_KEY, String(Date.now()));
                }
            },
        });
        // remember dismiss on the × button as well
        banner.querySelector('.mt-pwa-close').addEventListener('click', () => {
            localStorage.setItem(DISMISS_KEY, String(Date.now()));
        });
    });

    window.addEventListener('appinstalled', () => {
        deferredPrompt = null;
        document.querySelectorAll('.mt-pwa-install').forEach(el => el.classList.remove('show'));
    });

    /* ============================================================
     *  4.  Offline queue + auto-sync engine  (window.MTOffline)
     *
     *  أي صفحة تحمّل هذا الملف تحصل تلقائياً على:
     *    • طابور أوفلاين محفوظ في localStorage
     *    • مزامنة تلقائية عند عودة الاتصال (online event)
     *    • مزامنة عند تحميل الصفحة لو فيه طابور متبقٍّ من جلسة سابقة
     *    • مزامنة عند وصول رسالة SYNC_READY من الـ Service Worker
     *    • Background Sync كخط دفاع أخير عندما يكون التطبيق مغلقاً
     * ============================================================ */
    const QUEUE_KEY = 'mousstec_offline_queue';
    const SYNC_URL  = '/system/api/v1/inventory/offline-sync/';

    function readQueue() {
        try { return JSON.parse(localStorage.getItem(QUEUE_KEY) || '[]'); }
        catch (_) { return []; }
    }
    function writeQueue(q) {
        try { localStorage.setItem(QUEUE_KEY, JSON.stringify(q)); }
        catch (_) { /* حصة التخزين ممتلئة أو الوضع الخاص */ }
    }
    function getCookie(n) {
        const m = document.cookie.match('(^|;)\\s*' + n + '=([^;]*)');
        return m ? decodeURIComponent(m[2]) : '';
    }
    function uuid() {
        if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
            const r = Math.random() * 16 | 0;
            return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
    }

    function pendingCount() {
        return readQueue().length;
    }
    function emitStatus(detail) {
        try { window.dispatchEvent(new CustomEvent('mt-offline-sync', { detail })); } catch (_) {}
    }

    function requestBgSync() {
        if ('serviceWorker' in navigator && 'SyncManager' in window) {
            navigator.serviceWorker.ready
                .then((reg) => reg.sync.register('mousstec-offline-sync').catch(() => {}))
                .catch(() => {});
        }
    }

    /* أضف عملية للطابور. type مثل 'pos_invoice'، و data هو جسم الفاتورة القانوني. */
    function enqueue(type, data) {
        const q = readQueue();
        const local_id = (data && data.local_id) || uuid();
        const payload = Object.assign({}, data, { local_id });
        q.push({ type, local_id, data: payload, ts: new Date().toISOString() });
        writeQueue(q);
        emitStatus({ event: 'queued', pending: q.length });
        requestBgSync();
        // حاول المزامنة فوراً لو فيه اتصال
        if (navigator.onLine) flush();
        return local_id;
    }

    let flushing = false;
    async function flush() {
        if (flushing || !navigator.onLine) return;
        const q = readQueue();
        if (!q.length) return;

        flushing = true;
        emitStatus({ event: 'syncing', pending: q.length });
        try {
            const invoiceEntries = q.filter((e) => e.type === 'pos_invoice');
            if (!invoiceEntries.length) { flushing = false; return; }

            const invoices = invoiceEntries.map((e) =>
                Object.assign({ local_id: e.local_id }, e.data || {}));
            const sentIds = new Set(invoiceEntries.map((e) => e.local_id));

            const resp = await fetch(SYNC_URL, {
                method:  'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken':  getCookie('mt_csrf'),
                    'X-Requested-With': 'XMLHttpRequest',
                },
                body: JSON.stringify({ invoices }),
            });
            if (!resp.ok) throw new Error('sync HTTP ' + resp.status);
            const result = await resp.json().catch(() => ({}));

            if (result && result.status === 'success') {
                // الخادم idempotent (يتخطى المكرر عبر local_id) → نحذف فقط ما أرسلناه،
                // مع الحفاظ على أي عمليات أُضيفت أثناء انتظار الرد.
                const remaining = readQueue().filter(
                    (e) => !(e.type === 'pos_invoice' && sentIds.has(e.local_id)));
                writeQueue(remaining);
                emitStatus({ event: 'synced', synced: result.synced || 0,
                             skipped: result.skipped || 0, pending: remaining.length });
                if (result.synced) showSyncToast(result.synced);
            } else {
                emitStatus({ event: 'error', pending: readQueue().length });
            }
        } catch (_) {
            // نُبقي الطابور كما هو ليعاد المحاولة لاحقاً
            emitStatus({ event: 'error', pending: readQueue().length });
        } finally {
            flushing = false;
        }
    }

    function showSyncToast(n) {
        if (document.querySelector('.mt-pwa-toast')) return;
        const t = buildToast({
            klass:    'mt-pwa-toast',
            icon:     '✅',
            title:    `تمت مزامنة ${n} عملية`,
            sub:      'Offline changes synced successfully',
            btnLabel: 'تمام',
            onClick:  () => t.classList.remove('show'),
        });
        setTimeout(() => t.classList.remove('show'), 4000);
    }

    // مُحفِّزات المزامنة التلقائية
    window.addEventListener('online', flush);
    window.addEventListener('load', () => { if (navigator.onLine) flush(); });
    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.addEventListener('message', (event) => {
            if (event.data && event.data.type === 'SYNC_READY') flush();
        });
    }
    // شبكة أمان: إعادة محاولة دورية طالما هناك طابور متبقٍّ
    setInterval(() => { if (navigator.onLine && pendingCount()) flush(); }, 45000);

    window.MTOffline = { enqueue, flush, pendingCount, uuid, QUEUE_KEY };
})();
