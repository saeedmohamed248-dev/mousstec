// Mouss Tec Service Worker — Offline-First Architecture
const CACHE_NAME = 'mousstec-v2';
const OFFLINE_URL = '/offline/';

// Assets to pre-cache on install
const PRE_CACHE = [
    '/system/dashboard/',
    '/system/pos/',
    '/offline/',
    'https://fonts.googleapis.com/css2?family=Cairo:wght@300;400;700;900&display=swap',
    'https://cdn.tailwindcss.com',
    'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css',
];

// Install: pre-cache essential pages
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(PRE_CACHE).catch(() => {
                // If some assets fail to cache, continue anyway
                return cache.addAll([OFFLINE_URL]);
            });
        })
    );
    self.skipWaiting();
});

// Activate: clean old caches
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) => {
            return Promise.all(
                keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
            );
        })
    );
    self.clients.claim();
});

// Fetch: Network-first for API, Cache-first for static, Stale-While-Revalidate for pages
self.addEventListener('fetch', (event) => {
    const { request } = event;
    const url = new URL(request.url);

    // Skip non-GET requests — let them through (POST for invoices, etc.)
    if (request.method !== 'GET') {
        return;
    }

    // API calls: network only (no caching), fallback to offline response
    if (url.pathname.startsWith('/system/api/') || url.pathname.startsWith('/api/')) {
        event.respondWith(
            fetch(request).catch(() => {
                return new Response(
                    JSON.stringify({ error: 'offline', message: 'انت غير متصل بالانترنت. سيتم مزامنة البيانات عند عودة الاتصال.' }),
                    { headers: { 'Content-Type': 'application/json' }, status: 503 }
                );
            })
        );
        return;
    }

    // Static assets (CDN, fonts, CSS, JS): cache-first
    if (url.hostname !== location.hostname ||
        url.pathname.startsWith('/static/') ||
        url.pathname.endsWith('.css') ||
        url.pathname.endsWith('.js') ||
        url.pathname.endsWith('.woff2')) {
        event.respondWith(
            caches.match(request).then((cached) => {
                if (cached) return cached;
                return fetch(request).then((response) => {
                    if (response.ok) {
                        const clone = response.clone();
                        caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
                    }
                    return response;
                }).catch(() => caches.match(OFFLINE_URL));
            })
        );
        return;
    }

    // HTML pages: Stale-While-Revalidate
    if (request.headers.get('Accept')?.includes('text/html')) {
        event.respondWith(
            caches.match(request).then((cached) => {
                const networkFetch = fetch(request).then((response) => {
                    if (response.ok) {
                        const clone = response.clone();
                        caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
                    }
                    return response;
                }).catch(() => {
                    // Offline: return cached version or offline page
                    return cached || caches.match(OFFLINE_URL);
                });

                // Return cached immediately, update in background
                return cached || networkFetch;
            })
        );
        return;
    }

    // Everything else: network-first
    event.respondWith(
        fetch(request).catch(() => caches.match(request))
    );
});

// Background Sync: queue offline operations for later
self.addEventListener('sync', (event) => {
    if (event.tag === 'mousstec-offline-sync') {
        event.waitUntil(syncOfflineData());
    }
});

async function syncOfflineData() {
    // This will be triggered when connection returns
    // The POS and dashboard JS handle the actual data sync
    const clients = await self.clients.matchAll();
    clients.forEach((client) => {
        client.postMessage({ type: 'SYNC_READY', message: 'الاتصال عاد! جاري مزامنة البيانات...' });
    });
}
