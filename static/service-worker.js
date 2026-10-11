// Самоудаляющийся service worker.
// Старая версия (cache-first) могла остаться установленной у пользователей с 2025 года
// и отдавать устаревшие статические файлы. Эта версия при обновлении чистит кэш и снимает регистрацию.
self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', event => {
    event.waitUntil((async () => {
        const keys = await caches.keys();
        await Promise.all(keys.map(key => caches.delete(key)));
        await self.registration.unregister();
        const clients = await self.clients.matchAll({ type: 'window' });
        clients.forEach(client => client.navigate(client.url));
    })());
});
