/* ============================================================
 *  Mouss Tec — Service Worker (Production)
 *  Strategy:
 *    - install : pre-cache the App Shell + offline.html
 *    - fetch   : Network-First → Cache → offline.html (HTML)
 *                Cache-First for static assets (CSS/JS/img/fonts)
 *                Network-Only for API + non-GET (with JSON offline body)
 *    - message : SKIP_WAITING handler for live updates
 * ============================================================ */

const SW_VERSION   = 'v4.3.1-livedata-fix';
const APP_SHELL    = `mousstec-shell-${SW_VERSION}`;
const RUNTIME      = `mousstec-runtime-${SW_VERSION}`;
const OFFLINE_URL  = '/offline/';

/* ---------- App Shell ----------
 * نضيف '/' و '/secure-portal/' للـ pre-cache عشان لو الـ PWA اتفتحت
 * أوفلاين قبل ما المستخدم يزور أي صفحة، يلاقي بداية فيها معلومات بدل
 * offline.html فقط.
 */
const SHELL_ASSETS = [
    OFFLINE_URL,
    '/',
    '/manifest.json',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/js/pwa-init.js',
    'https://fonts.googleapis.com/css2?family=Cairo:wght@300;400;700;900&display=swap',
    'https://cdn.tailwindcss.com',
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css',
];

/* ---------- INSTALL ---------- */
self.addEventListener('install', (event) => {
    event.waitUntil((async () => {
        const cache = await caches.open(APP_SHELL);
        // addAll fails atomically; fall back to per-item to survive a single 404 / CORS fail
        await Promise.all(
            SHELL_ASSETS.map(async (url) => {
                try { await cache.add(new Request(url, { cache: 'reload' })); }
                catch (_) { /* tolerate individual asset failures */ }
            })
        );
    })());
    // Don't auto skipWaiting — let the client decide via SKIP_WAITING message.
});

/* ---------- ACTIVATE ---------- */
self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const keep = new Set([APP_SHELL, RUNTIME]);
        const keys = await caches.keys();
        await Promise.all(keys.filter(k => !keep.has(k)).map(k => caches.delete(k)));
        await self.clients.claim();
    })());
});

/* ---------- MESSAGE (force-update) ---------- */
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    }
});

/* ---------- FETCH ---------- */
self.addEventListener('fetch', (event) => {
    const req = event.request;
    if (req.method !== 'GET') return; // never intercept POST/PUT/DELETE

    const url = new URL(req.url);
    // Never intercept non-http(s) schemes — chrome-extension://, devtools://,
    // data:, blob: etc. Cache.put() throws on them and the unhandled rejection
    // pollutes the console of every page using extensions.
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return;

    const isSameOrigin = url.origin === self.location.origin;
    const accept = req.headers.get('Accept') || '';
    const isHTML = req.mode === 'navigate' || accept.includes('text/html');
    const isAPI  = isSameOrigin && (url.pathname.startsWith('/api/') ||
                                    url.pathname.startsWith('/system/api/'));

    // 1️⃣  API → network only, JSON fallback when offline
    if (isAPI) {
        event.respondWith(
            fetch(req).catch(() => new Response(
                JSON.stringify({ offline: true, error: 'offline',
                                 message: 'انت غير متصل بالإنترنت. سيتم المزامنة عند عودة الاتصال.' }),
                { status: 503, headers: { 'Content-Type': 'application/json; charset=utf-8' } }
            ))
        );
        return;
    }

    // 2️⃣  HTML navigations → Network-First, Cache fallback, offline.html as last resort
    if (isHTML) {
        event.respondWith((async () => {
            try {
                const fresh = await fetch(req);
                const cache = await caches.open(RUNTIME);
                cache.put(req, fresh.clone());
                return fresh;
            } catch (_) {
                const cached = await caches.match(req);
                if (cached) return cached;
                const offline = await caches.match(OFFLINE_URL);
                return offline || new Response('Offline', { status: 503 });
            }
        })());
        return;
    }

    // 3️⃣  Static assets (same-origin /static/ + CDN fonts/scripts) → Cache-First
    const isStatic = url.pathname.startsWith('/static/')
                  || /\.(css|js|woff2?|ttf|otf|png|jpe?g|svg|webp|gif|ico)$/i.test(url.pathname)
                  || !isSameOrigin;
    if (isStatic) {
        event.respondWith((async () => {
            const cached = await caches.match(req);
            if (cached) return cached;
            try {
                const res = await fetch(req);
                if (res && res.status === 200 && (res.type === 'basic' || res.type === 'cors')) {
                    const cache = await caches.open(RUNTIME);
                    cache.put(req, res.clone());
                }
                return res;
            } catch (_) {
                return caches.match(OFFLINE_URL);
            }
        })());
        return;
    }

    // 4️⃣  Everything else → network with cache fallback
    event.respondWith(
        fetch(req).catch(() => caches.match(req).then(c => c || caches.match(OFFLINE_URL)))
    );
});

/* ---------- BACKGROUND SYNC (optional hook) ---------- */
self.addEventListener('sync', (event) => {
    if (event.tag === 'mousstec-offline-sync') {
        event.waitUntil((async () => {
            const all = await self.clients.matchAll();
            all.forEach(c => c.postMessage({ type: 'SYNC_READY' }));
        })());
    }
});
