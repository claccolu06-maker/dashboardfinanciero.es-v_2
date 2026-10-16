/**
 * Panel «Importar extracto»: vista previa (dry_run) -> confirmación.
 *
 * - Archivo: POST /panel/importar_csv (multipart)
 * - Texto:   POST /panel/importar_texto (JSON)
 * Cada importación lleva una «clave» aleatoria: si se reenvía por error (doble
 * clic, reintento de red) el servidor devuelve el resultado anterior en vez de
 * volver a procesar. Todo lo que viene del servidor se pinta con textContent.
 */
(function () {
    const panel = document.getElementById('importar');
    if (!panel) return;

    const el = (id) => document.getElementById(id);
    const ui = {
        toggle: el('importarToggle'), modoArchivo: el('impModoArchivo'), modoTexto: el('impModoTexto'),
        drop: el('impDrop'), archivo: el('impArchivo'), nombre: el('impNombre'), texto: el('impTexto'),
        analizar: el('impAnalizar'), estado: el('impEstado'), resultado: el('impResultado'),
        resumen: el('impResumen'), avisos: el('impAvisos'), filas: el('impFilas'), truncado: el('impTruncado'),
        confirmar: el('impConfirmar'), cancelar: el('impCancelar')
    };
    const MAX_BYTES = 10 * 1024 * 1024;
    let modo = 'archivo';
    let clave = nuevaClave();

    function nuevaClave() {
        if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
        return 'k' + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
    }
    const eur = (n) => Number(n).toLocaleString('es-ES', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' €';

    function abrir(abierto) {
        panel.classList.toggle('open', abierto);
        ui.toggle.setAttribute('aria-expanded', abierto ? 'true' : 'false');
    }
    ui.toggle.addEventListener('click', () => abrir(!panel.classList.contains('open')));
    abrir(panel.classList.contains('open') || window.location.hash === '#importar');
    if (window.location.hash === '#importar') panel.scrollIntoView({ behavior: movimientoReducido() ? 'auto' : 'smooth' });

    panel.querySelectorAll('[data-modo]').forEach(b => b.addEventListener('click', () => {
        modo = b.dataset.modo;
        panel.querySelectorAll('[data-modo]').forEach(x => x.classList.toggle('active', x === b));
        ui.modoArchivo.hidden = modo !== 'archivo';
        ui.modoTexto.hidden = modo !== 'texto';
        reiniciar();
    }));

    function reiniciar() {
        ui.resultado.hidden = true;
        ui.estado.textContent = '';
        clave = nuevaClave();
    }

    function elegirArchivo(f) {
        if (!f) return;
        if (f.size > MAX_BYTES) { ui.estado.textContent = 'El archivo supera los 10 MB.'; return; }
        ui.nombre.textContent = f.name + ' (' + Math.max(1, Math.round(f.size / 1024)) + ' KB)';
        reiniciar();
    }
    ui.archivo.addEventListener('change', () => elegirArchivo(ui.archivo.files[0]));
    ['dragenter', 'dragover'].forEach(ev => ui.drop.addEventListener(ev, (e) => { e.preventDefault(); ui.drop.classList.add('drag'); }));
    ['dragleave', 'drop'].forEach(ev => ui.drop.addEventListener(ev, (e) => { e.preventDefault(); ui.drop.classList.remove('drag'); }));
    ui.drop.addEventListener('drop', (e) => {
        const f = e.dataTransfer && e.dataTransfer.files[0];
        if (f) { const dt = new DataTransfer(); dt.items.add(f); ui.archivo.files = dt.files; elegirArchivo(f); }
    });
    ui.texto.addEventListener('input', reiniciar);

    async function enviar(dryRun) {
        if (modo === 'archivo') {
            const f = ui.archivo.files[0];
            if (!f) throw new Error('Selecciona primero un archivo.');
            const datos = new FormData();
            datos.append('archivo', f);
            datos.append('dry_run', dryRun ? '1' : '0');
            datos.append('clave', clave);
            return findashFetch('/panel/importar_csv', { body: datos });
        }
        const texto = ui.texto.value;
        if (!texto.trim()) throw new Error('Pega primero el texto de tu extracto.');
        return findashFetch('/panel/importar_texto', { json: { texto: texto, dry_run: dryRun, clave: clave } });
    }

    // Al terminar NO se borra el texto de estado: así los mensajes de error asignados
    // dentro del try siguen visibles. Cada operación limpia el estado al empezar.
    function ocupado(si, mensaje) {
        ui.analizar.disabled = si; ui.confirmar.disabled = si;
        if (si) ui.estado.textContent = mensaje;
    }

    function celda(fila, texto, clase, estilo) {
        const td = document.createElement('td');
        td.textContent = texto;
        if (clase) td.className = clase;
        if (estilo) td.style.cssText = estilo;
        fila.appendChild(td);
        return td;
    }

    function pintarVistaPrevia(d) {
        ui.resumen.replaceChildren();
        [['Detectados', d.detectados], ['Nuevos', d.nuevos], ['Ya existentes', d.duplicados],
         ['Líneas descartadas', d.descartadas], ['Ingresos', eur(d.total_ingresos)], ['Gastos', eur(d.total_gastos)]
        ].forEach(([k, v]) => {
            const caja = document.createElement('div'); caja.className = 'import-stat';
            const b = document.createElement('b'); b.textContent = v;
            caja.append(b, document.createTextNode(k));
            ui.resumen.appendChild(caja);
        });

        ui.avisos.replaceChildren();
        const avisos = (d.avisos || []).slice();
        avisos.push('Estrategia de lectura: ' + d.estrategia + '.');
        avisos.forEach((a, i) => {
            const aviso = document.createElement('div');
            aviso.className = 'alert-banner ' + (i === avisos.length - 1 ? 'info' : 'warning');
            aviso.textContent = a;
            ui.avisos.appendChild(aviso);
        });

        ui.filas.replaceChildren();
        d.muestra.forEach(m => {
            const tr = document.createElement('tr');
            if (m.duplicado) tr.className = 'dup';
            celda(tr, m.fecha, '', 'white-space:nowrap;');
            celda(tr, m.concepto, '', 'max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;');
            celda(tr, m.categoria || '');
            celda(tr, m.tipo + (m.tipo_adivinado ? ' (?)' : ''));
            celda(tr, (m.tipo === 'ingreso' ? '+' : '-') + eur(m.importe), 'money',
                  'text-align:right;font-weight:800;white-space:nowrap;' + (m.tipo === 'ingreso' ? 'color:var(--success);' : ''));
            celda(tr, m.duplicado ? 'ya existe' : '', '', 'color:var(--text-muted);font-size:.72rem;');
            ui.filas.appendChild(tr);
        });
        ui.truncado.hidden = !d.muestra_truncada;
        ui.confirmar.disabled = d.nuevos === 0;
        ui.confirmar.lastChild.textContent = d.nuevos === 0
            ? ' No hay nada nuevo que importar' : ' Importar ' + d.nuevos + ' movimiento(s)';
        ui.resultado.hidden = false;
    }

    ui.analizar.addEventListener('click', async () => {
        ocupado(true, 'Analizando…');
        try {
            const r = await enviar(true);
            if (!r.ok) { ui.estado.textContent = (r.data && r.data.error) || 'No se pudo analizar el contenido.'; ui.resultado.hidden = true; return; }
            ui.estado.textContent = '';
            pintarVistaPrevia(r.data);
        } catch (err) {
            ui.estado.textContent = err.message || 'Error de conexión.';
        } finally { ocupado(false); }
    });

    ui.confirmar.addEventListener('click', async () => {
        ocupado(true, 'Importando…');
        try {
            const r = await enviar(false);
            if (!r.ok) { ui.estado.textContent = (r.data && r.data.error) || 'No se pudo importar.'; return; }
            const d = r.data;
            let msg = 'Importados ' + d.insertados + ' movimientos';
            if (d.duplicados) msg += ' (' + d.duplicados + ' ya existían)';
            sessionStorage.setItem('findash_toast', msg + '.');
            clave = nuevaClave();
            window.location.href = '/panel/transactions';
        } catch (err) {
            ui.estado.textContent = err.message || 'Error de conexión.';
        } finally { ocupado(false); }
    });

    ui.cancelar.addEventListener('click', reiniciar);
})();
