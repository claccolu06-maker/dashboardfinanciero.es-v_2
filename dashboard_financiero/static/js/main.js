/**
 * FinDash - JavaScript común a todas las vistas.
 *
 * - El tema inicial lo fija un script en línea del <head> (base.html), antes del
 *   primer pintado, así que aquí solo se gestionan los cambios posteriores.
 * - Todas las llamadas fetch añaden la cabecera X-CSRFToken (ver findashFetch).
 */

/* ---------- CSRF ---------- */
function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
}

/** fetch con CSRF y JSON por defecto. Devuelve {ok, status, data}. */
async function findashFetch(url, opciones = {}) {
    const cabeceras = Object.assign({
        'X-CSRFToken': getCsrfToken(),
        'Accept': 'application/json',
        'X-Requested-With': 'fetch'
    }, opciones.headers || {});
    if (opciones.json !== undefined) {
        cabeceras['Content-Type'] = 'application/json';
        opciones.body = JSON.stringify(opciones.json);
    }
    const respuesta = await fetch(url, Object.assign({ method: 'POST', credentials: 'same-origin' },
        opciones, { headers: cabeceras }));
    let data = null;
    try { data = await respuesta.json(); } catch (e) { data = null; }
    return { ok: respuesta.ok && (!data || data.ok !== false), status: respuesta.status, data: data };
}

/* ---------- Preferencias visuales ---------- */
function movimientoReducido() {
    return document.documentElement.getAttribute('data-reduce-motion') === '1' ||
        (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

function resolverTema(pref) {
    if (pref === 'system') {
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    return pref === 'light' ? 'light' : 'dark';
}

function aplicarTema(pref) {
    const efectivo = resolverTema(pref);
    const raiz = document.documentElement;
    raiz.setAttribute('data-theme', efectivo);
    raiz.setAttribute('data-theme-pref', pref);
    raiz.style.colorScheme = efectivo;
    try { localStorage.setItem('findash_theme', pref); } catch (e) { /* modo privado */ }
    document.querySelectorAll('.theme-box').forEach(b => b.classList.remove('active'));
    const caja = document.getElementById('theme-' + pref);
    if (caja) caja.classList.add('active');
}

/** Cambia el tema al instante y lo guarda en el servidor. */
function uiSetTheme(tema) {
    aplicarTema(tema);
    guardarPreferenciasCloud(true);
}

let prefSyncTimeout = null;
function guardarPreferenciasCloud(inmediato) {
    const privacidad = document.getElementById('toggle-privacy');
    const animaciones = document.getElementById('toggle-animations');
    const raiz = document.documentElement;
    if (privacidad) {
        if (privacidad.checked) raiz.setAttribute('data-privacy', '1'); else raiz.removeAttribute('data-privacy');
    }
    if (animaciones) {
        if (animaciones.checked) raiz.setAttribute('data-reduce-motion', '1'); else raiz.removeAttribute('data-reduce-motion');
    }
    const enviar = () => {
        findashFetch('/panel/settings/preferencias', {
            json: {
                tema: raiz.getAttribute('data-theme-pref') || 'dark',
                privacidad: privacidad ? privacidad.checked : raiz.hasAttribute('data-privacy'),
                animaciones: animaciones ? animaciones.checked : raiz.hasAttribute('data-reduce-motion')
            }
        }).then(r => { if (!r.ok) console.warn('[FinDash] No se pudieron guardar las preferencias', r.status); })
          .catch(err => console.warn('[FinDash] Error al sincronizar:', err));
    };
    clearTimeout(prefSyncTimeout);
    if (inmediato) enviar(); else prefSyncTimeout = setTimeout(enviar, 400);
}

/* ---------- Sidebar ---------- */
function initSidebar() {
    const boton = document.getElementById('sidebarCollapseBtn');
    const envoltorio = document.getElementById('layoutWrapper');
    const icono = document.getElementById('collapseIcon');
    if (!boton || !envoltorio) return;

    let colapsado = false;
    try { colapsado = localStorage.getItem('findash_sidebar_collapsed') === 'true'; } catch (e) {}
    if (colapsado) {
        envoltorio.classList.add('collapsed');
        if (icono) icono.className = 'ph ph-caret-right';
    }
    boton.addEventListener('click', () => {
        const ahora = envoltorio.classList.toggle('collapsed');
        if (icono) icono.className = ahora ? 'ph ph-caret-right' : 'ph ph-caret-left';
        try { localStorage.setItem('findash_sidebar_collapsed', String(ahora)); } catch (e) {}
        window.dispatchEvent(new Event('resize'));
    });
}

/* ---------- Notificaciones ---------- */
function mostrarToast(mensaje, tipo) {
    const contenedor = document.getElementById('toast-container');
    if (!contenedor) return;
    const toast = document.createElement('div');
    toast.className = 'toast ' + (tipo || 'info');
    toast.textContent = mensaje;               // textContent: nunca HTML
    contenedor.appendChild(toast);
    programarCierre(toast);
}

function programarCierre(toast) {
    const retraso = toast.classList.contains('danger') ? 6500 : 3800;
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(-10px)';
        setTimeout(() => toast.remove(), 400);
    }, retraso);
}

function initToasts() {
    document.querySelectorAll('#toast-container .toast').forEach(programarCierre);
}

/* ---------- Contraseñas ---------- */
function togglePassword(inputId, boton) {
    const input = document.getElementById(inputId);
    const icono = boton.querySelector('i');
    if (!input || !icono) return;
    if (input.type === 'password') {
        input.type = 'text';
        icono.classList.replace('ph-eye', 'ph-eye-slash');
        icono.style.color = 'var(--primary)';
    } else {
        input.type = 'password';
        icono.classList.replace('ph-eye-slash', 'ph-eye');
        icono.style.color = 'var(--text-muted)';
    }
}

/* ---------- Animaciones ---------- */
function animateCounters() {
    document.querySelectorAll('.animate-number').forEach(contador => {
        const textoFinal = contador.getAttribute('data-final-text');
        if (!textoFinal) return;
        try {
            const negativo = textoFinal.includes('-');
            let objetivo = parseFloat(textoFinal.replace(/[^0-9,]/g, '').replace(',', '.')) || 0;
            if (negativo) objetivo = -objetivo;
            if (objetivo === 0 || movimientoReducido()) { contador.textContent = textoFinal; return; }

            const frames = 48;
            let frame = 0;
            const paso = () => {
                frame++;
                if (frame >= frames) { contador.textContent = textoFinal; return; }
                const progreso = 1 - Math.pow(1 - frame / frames, 3);       // ease-out
                contador.textContent = (objetivo * progreso).toLocaleString('es-ES',
                    { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' €';
                requestAnimationFrame(paso);
            };
            paso();
        } catch (err) {
            contador.textContent = textoFinal;
        }
    });
}

function animateAllCards() {
    if (movimientoReducido()) return;
    const tarjetas = document.querySelectorAll('.bento-card, .kpi-card, .analytics-card-kpi, .category-rank-item');
    let visibles = 0;
    tarjetas.forEach(tarjeta => {
        if (getComputedStyle(tarjeta).display !== 'none') {
            if (!tarjeta.classList.contains('fade-in-up')) {
                tarjeta.classList.add('fade-in-up', 'delay-' + Math.min(visibles + 1, 5));
                visibles++;
            }
        } else {
            tarjeta.classList.remove('fade-in-up', 'delay-1', 'delay-2', 'delay-3', 'delay-4', 'delay-5');
        }
    });
}

/* ---------- Arranque ---------- */
document.addEventListener('DOMContentLoaded', () => {
    try {
        const pref = document.documentElement.getAttribute('data-theme-pref') || 'dark';
        const caja = document.getElementById('theme-' + pref);
        if (caja) caja.classList.add('active');

        // Si el sistema cambia de claro a oscuro y el usuario eligió «Auto», seguirlo.
        if (window.matchMedia) {
            const mq = window.matchMedia('(prefers-color-scheme: dark)');
            const alCambiar = () => {
                if (document.documentElement.getAttribute('data-theme-pref') === 'system') aplicarTema('system');
            };
            if (mq.addEventListener) mq.addEventListener('change', alCambiar);
        }
        initSidebar();
        initToasts();
        animateAllCards();
        setTimeout(animateCounters, 120);
    } catch (e) {
        console.error('[FinDash] Error en la inicialización:', e);
    }
});
