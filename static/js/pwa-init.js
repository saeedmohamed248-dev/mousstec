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
    .mt-pwa-toast,.mt-pwa-install{position:fixed;left:50%;transform:translateX(-50%);
      z-index:99999;font-family:'Cairo','Segoe UI',Tahoma,sans-serif;direction:rtl;
      background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);color:#f1f5f9;
      border:1px solid rgba(139,92,246,.35);border-radius:14px;
      box-shadow:0 18px 40px rgba(0,0,0,.45),0 0 0 1px rgba(255,255,255,.04);
      padding:14px 18px;display:flex;align-items:center;gap:14px;
      max-width:92vw;width:auto;opacity:0;pointer-events:none;
      transition:transform .35s cubic-bezier(.2,.8,.2,1),opacity .35s ease;}
    .mt-pwa-toast{bottom:24px;transform:translate(-50%,40px);}
    .mt-pwa-install{bottom:24px;transform:translate(-50%,40px);}
    .mt-pwa-toast.show,.mt-pwa-install.show{opacity:1;pointer-events:auto;transform:translate(-50%,0);}
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
})();
