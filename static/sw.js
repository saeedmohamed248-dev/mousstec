/* ============================================================
 *  Mouss Tec — Service Worker (Production)
 *  Strategy:
 *    - install : pre-cache the App Shell + offline.html
 *    - fetch   : Network-First → Cache → offline.html (HTML)
 *                Cache-First for static assets (CSS/JS/img/fonts)
 *                Network-Only for API + non-GET (with JSON offline body)
 *    - message : SKIP_WAITING handler for live updates
 * ============================================================ */

const SW_VERSION   = 'v7.4.0-selfheal-js-response';
const APP_SHELL    = `mousstec-shell-${SW_VERSION}`;
const RUNTIME      = `mousstec-runtime-${SW_VERSION}`;
const OFFLINE_URL  = '/offline/';

/* ---------- App Shell ----------
 * ⚠️ لا نـ pre-cache '/' أبداً: لو اتخزّن أثناء عطل (redirect loop / صفحة فاضية)
 * كان بيفضل يقدّم صفحة بيضا حتى بعد إصلاح السيرفر. صفحات HTML دايماً
 * network-first والـ fallback هو offline.html فقط — مش '/' المخزّنة.
 */
const SHELL_ASSETS = [
    OFFLINE_URL,
    '/manifest.json',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/js/pwa-init.js',
    // Self-hosted vendor assets (same-origin → never blocked by a mobile carrier
    // that filters public CDNs, which was leaving the whole layout unstyled).
    '/static/vendor/js/tailwind-3.4.17.min.js',
    '/static/vendor/fontawesome/css/all.min.css',
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
    // فعّل النسخة الجديدة فورًا (إصلاح حرِج للصفحة البيضاء) بدل انتظار قفل كل التابات.
    self.skipWaiting();
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

    // 🩹 شفاء ذاتي: لو حصل تنقّل لملف غير-صفحة (زي /sw.js) — بيحصل لو أيقونة PWA
    // مثبّتة قديمة رابط بدايتها اتخزّن غلط أثناء عطل سابق — حوّل لـ '/' بدل ما
    // نعرض كود الملف كصفحة. (طلبات تحميل الـ SW نفسه mode='script' مش 'navigate'.)
    if (req.mode === 'navigate' && isSameOrigin &&
        /\.(js|css|json|png|jpe?g|svg|webp|gif|ico|woff2?|ttf|otf|map)$/i.test(url.pathname)) {
        event.respondWith(Response.redirect('/', 302));
        return;
    }
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
                // 🚑 طلب واحد بس يتابع التحويل بنفسه (redirect:'follow').
                // ⚠️ [FIX حلقة الدخول على الموبايل]: الكود القديم كان بيعمل طلبين
                // لنفس رابط التنقّل (واحد redirect:'manual' يكتشف التحويل، وواحد
                // يتابعه) — فروابط الدخول/الدخول-للفرع اللي بتوكن يُستهلك مرة واحدة
                // كان بيتفشّل الطلب التاني ويرجّع المستخدم لصفحة الدخول (حلقة دخول).
                // طلب واحد بيتابع التحويل = يستهلك التوكن ويحفظ الكوكي مرة واحدة بس.
                const fresh = await fetch(req.url, { credentials: 'include', redirect: 'follow' });

                // 🩹 شفاء ذاتي إضافي: لو التنقّل رجّع سكربت (كاش مسموم قديم أو
                // ردّ خاطئ) — ما نعرضهوش كصفحة أبداً، نرجّع لـ '/'. مفيش تنقّل
                // شرعي المفروض يتعرض كـ application/javascript.
                const ctype = fresh.headers.get('Content-Type') || '';
                if (ctype.includes('javascript')) {
                    return Response.redirect('/', 302);
                }

                // كاش النسخ الناجحة فقط، وبس لو HTML فعلاً — عشان ما نخزّنش رد
                // غير-HTML تحت مفتاح تنقّل فيتعرض كصفحة بعدين.
                const ct = fresh.headers.get('Content-Type') || '';
                if (fresh.status === 200 && ct.includes('text/html')) {
                    const cache = await caches.open(RUNTIME);
                    cache.put(req, fresh.clone()).catch(() => {});
                }

                // لو الرد جه بعد تحويل، نرجّعه كنسخة نظيفة (بدون علم redirected)
                // عشان المتصفح ما يرفضهوش لطلب تنقّل وضعه redirect:'manual'.
                if (fresh.redirected) {
                    const buf = await fresh.clone().arrayBuffer();
                    const h = new Headers(fresh.headers);
                    h.delete('content-encoding');
                    h.delete('content-length');
                    return new Response(buf, {
                        status: fresh.status || 200,
                        statusText: fresh.statusText || 'OK',
                        headers: h,
                    });
                }
                return fresh;
            } catch (_e1) {
                // فشل الـ follow-fetch غالباً بسبب تحويل لموقع خارجي (زي wa.me في
                // مشاركة واتساب) — cors مش هتقدر تقرأه. نسيب المتصفح يتابع التحويل
                // بنفسه (يفتح واتساب) بدل ما نطلّع صفحة "غير متصل".
                try {
                    const manual = await fetch(req);
                    if (manual.type === 'opaqueredirect' || manual.redirected || manual.ok) {
                        return manual;
                    }
                } catch (_e2) { /* offline فعلاً — نكمّل للـ fallback */ }
                // آخر حل: نسخة مخزّنة لنفس الصفحة، وإلا صفحة الأوفلاين المخصّصة.
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
