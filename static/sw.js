/* ============================================================
 *  Mouss Tec — Service Worker (Production)
 *  Strategy (v8 — نسخة آمنة):
 *    - install : تفعيل فوري (skipWaiting) + precache خفيف لصفحة الأوفلاين والأصول
 *    - activate: مسح كل الكاش القديم بالكامل + السيطرة على كل التابات
 *    - fetch   : ❌ ما نعترضش طلبات التنقّل (HTML) إطلاقاً — المتصفح يتعامل معاها
 *                مباشرة زي أي موقع عادي. ده بيمنع نهائياً: الصفحة البيضا،
 *                ظهور كود sw.js كصفحة، وحلقة تسجيل الدخول (كلها كانت بسبب
 *                اعتراض الـ SW للتنقّل ومعالجة التحويلات).
 *                الأصول الثابتة (CSS/JS/img/fonts) → Cache-First.
 *                الـ API → Network-Only مع ردّ JSON عند عدم الاتصال.
 * ============================================================ */

const SW_VERSION   = 'v8.0.0-no-nav-intercept';
const RUNTIME      = `mousstec-runtime-${SW_VERSION}`;
const OFFLINE_URL  = '/offline/';

// أصول ثابتة خفيفة نخزّنها مقدماً (بدون '/' أبداً — عشان ما نخزّنش صفحة فاضية)
const SHELL_ASSETS = [
    OFFLINE_URL,
    '/manifest.json',
    '/static/js/pwa-init.js',
    '/static/vendor/js/tailwind-3.4.17.min.js',
    '/static/vendor/fontawesome/css/all.min.css',
];

/* ---------- INSTALL ---------- */
self.addEventListener('install', (event) => {
    event.waitUntil((async () => {
        const cache = await caches.open(RUNTIME);
        await Promise.all(SHELL_ASSETS.map(async (url) => {
            try { await cache.add(new Request(url, { cache: 'reload' })); }
            catch (_) { /* نتحمّل فشل أي أصل مفرد */ }
        }));
    })());
    self.skipWaiting();
});

/* ---------- ACTIVATE ---------- */
self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        // 🧹 مسح كل الكاش القديم بالكامل (بما فيه أي كاش مسموم من نسخ سابقة)
        const keys = await caches.keys();
        await Promise.all(keys.filter(k => k !== RUNTIME).map(k => caches.delete(k)));
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
    if (req.method !== 'GET') return; // لا نعترض POST/PUT/DELETE

    const url = new URL(req.url);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return;

    const isSameOrigin = url.origin === self.location.origin;
    const accept = req.headers.get('Accept') || '';
    const isHTML = req.mode === 'navigate' || accept.includes('text/html');

    // 1️⃣  طلبات التنقّل (صفحات HTML) → ❌ لا نعترض إطلاقاً.
    //     المتصفح بيجيبها من الشبكة بنفسه (بما فيها التحويلات، الكوكيز،
    //     تسجيل الدخول، اختيار الفرع) — فمستحيل الـ SW يطلّع صفحة غلط.
    if (isHTML) return;

    // 2️⃣  API → شبكة فقط، ردّ JSON عند عدم الاتصال
    const isAPI = isSameOrigin && (url.pathname.startsWith('/api/') ||
                                   url.pathname.startsWith('/system/api/'));
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

    // 3️⃣  الأصول الثابتة (same-origin /static/ + خطوط/سكربتات CDN) → Cache-First
    const isStatic = url.pathname.startsWith('/static/')
                  || /\.(css|js|woff2?|ttf|otf|png|jpe?g|svg|webp|gif|ico|map)$/i.test(url.pathname)
                  || !isSameOrigin;
    if (isStatic) {
        event.respondWith((async () => {
            const cached = await caches.match(req);
            if (cached) return cached;
            try {
                const res = await fetch(req);
                if (res && res.status === 200 && (res.type === 'basic' || res.type === 'cors')) {
                    const cache = await caches.open(RUNTIME);
                    cache.put(req, res.clone()).catch(() => {});
                }
                return res;
            } catch (_) {
                return caches.match(OFFLINE_URL);
            }
        })());
        return;
    }

    // 4️⃣  أي حاجة تانية → شبكة مع رجوع للكاش
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
