// 理理喵 Service Worker
const CACHE = 'lili-v1';

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll([
      '/',
      '/static/icon.svg',
      '/static/manifest.json'
    ]))
  );
  self.skipWaiting();
});

self.addEventListener('fetch', e => {
  // API 请求不缓存，直接走网络
  if (e.request.url.includes('/api/')) return;
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request))
  );
});
