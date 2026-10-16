/*
 * FinDash - service worker.
 *
 * Reglas (son datos financieros privados):
 *  - NUNCA se cachea HTML ni respuestas de la API: lo autenticado siempre va a la red.
 *  - Solo se cachean archivos estáticos (CSS, JS, iconos) con estrategia cache-first.
 *  - Sin conexión, las navegaciones muestran una página de aviso.
 *  - Para publicar cambios en los estáticos sube CACHE_VERSION.
 */
const CACHE_VERSION = 'findash-static-v3';
const PRECACHE = [
    '/static/css/style.css?v=3',
    '/static/js/main.js?v=3',
    '/static/manifest.json',
    '/static/icon-192.png'
];

const PAGINA_OFFLINE = `<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sin conexión - FinDash</title>
<style>
 body{font-family:-apple-system,"Segoe UI",sans-serif;background:#0a0a0b;color:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;text-align:center;padding:2rem}
 h1{font-size:1.5rem;margin:.5rem 0}p{color:#a1a1aa;font-size:.95rem}.dot{width:12px;height:12px;background:#00E599;border-radius:50%;display:inline-block}
 button{margin-top:1rem;background:#00E599;border:0;border-radius:10px;padding:.7rem 1.4rem;font-weight:800;cursor:pointer}
</style></head><body><div><span class="dot"></span><h1>FinDash sin conexión</h1>
<p>Comprueba tu conexión a internet e inténtalo de nuevo.</p>
<button onclick="location.reload()">Reintentar</button></div></body></html>`;

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_VERSION)
            .then((cache) => cache.addAll(PRECACHE))
            .catch(() => { /* un recurso caído no debe impedir instalar el SW */ })
    );
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((claves) => Promise.all(claves.filter((c) => c !== CACHE_VERSION).map((c) => caches.delete(c))))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const peticion = event.request;
    if (peticion.method !== 'GET') return;                 // POST (formularios, API): directo a la red
    const url = new URL(peticion.url);
    if (url.origin !== self.location.origin) return;       // fuentes, CDN, Google...: no tocar

    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(peticion).then((guardado) => {
                if (guardado) return guardado;
                return fetch(peticion).then((respuesta) => {
                    if (respuesta.ok && respuesta.type === 'basic') {
                        const copia = respuesta.clone();
                        caches.open(CACHE_VERSION).then((cache) => cache.put(peticion, copia));
                    }
                    return respuesta;
                });
            })
        );
        return;
    }

    if (peticion.mode === 'navigate') {                    // páginas: solo red, con aviso sin conexión
        event.respondWith(
            fetch(peticion).catch(() => new Response(PAGINA_OFFLINE, {
                status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' }
            }))
        );
    }
    // Resto (fetch de JSON, etc.): sin respondWith -> comportamiento normal del navegador.
});
