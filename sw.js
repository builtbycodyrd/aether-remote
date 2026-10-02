/* Aether Remote - service worker. Only notifications: nothing is cached, so
 * the app is always the live one from the PC.
 *
 * Each push carries {title, body, tag, tier, url, ts, kind}. The tier you
 * picked for that kind decides how it behaves:
 *   quiet      shows up, no sound or buzz
 *   normal     a normal notification
 *   important  stays on screen until you deal with it
 * One tag per thing, so an update replaces the old card instead of stacking.
 */
'use strict';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { title: 'Aether Remote', body: e.data && e.data.text() }; }
  const tier = d.tier || 'normal';
  e.waitUntil(self.registration.showNotification(d.title || 'Aether Remote', {
    body: d.body || '',
    tag: d.tag || d.kind || 'aether',
    icon: '/static/icon-192.png',
    badge: '/static/badge-96.png',
    timestamp: d.ts || Date.now(),
    silent: tier === 'quiet',
    renotify: tier !== 'quiet',
    requireInteraction: tier === 'important',
    data: { url: d.url || '/', kind: d.kind || '' },
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) {
      if (new URL(c.url).origin === self.location.origin) {
        await c.focus();
        c.postMessage({ aetherNotification: url });
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
