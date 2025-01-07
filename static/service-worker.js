const CACHE_NAME = 'affinia-cache-v1';
const urlsToCache = [
    '/mini_app',
    '/static/icon-192x192.png',
    '/static/icon-512x512.png',
    '/static/manifest.json',
];

self.addEventListener('install', event => {
    console.log('Service Worker installing.');
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => {
            console.log('Caching app shell');
            return cache.addAll(urlsToCache);
        })
    );
});

self.addEventListener('fetch', event => {
    event.respondWith(
        caches.match(event.request).then(response => {
            return response || fetch(event.request);
        })
    );
});

self.addEventListener('activate', event => {
    console.log('Service Worker activating.');
    event.waitUntil(
        caches.keys().then(keyList => {
            return Promise.all(
                keyList.map(key => {
                    if (key !== CACHE_NAME) {
                        console.log('Removing old cache:', key);
                        return caches.delete(key);
                    }
                })
            );
        })
    );
});
