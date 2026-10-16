/**
 * Edición de movimientos en un modal. Usa POST /panel/editar_movimiento (JSON + CSRF).
 * Si el usuario cambia la categoría, ofrece aplicarla a los movimientos similares
 * y el servidor aprende la regla para futuras importaciones.
 */
(function () {
    const modal = document.getElementById('modalEditar');
    if (!modal) return;

    const campos = {
        id: document.getElementById('edId'),
        tipo: document.getElementById('edTipo'),
        fecha: document.getElementById('edFecha'),
        importe: document.getElementById('edImporte'),
        concepto: document.getElementById('edConcepto'),
        categoria: document.getElementById('edCategoria'),
        similares: document.getElementById('edSimilares'),
        filaSimilares: document.getElementById('edSimilaresFila'),
        error: document.getElementById('edError'),
        guardar: document.getElementById('edGuardar')
    };
    let categoriaOriginal = '';
    let ultimoFoco = null;

    function abrir(boton) {
        ultimoFoco = boton;
        const d = boton.dataset;
        campos.id.value = d.id;
        campos.tipo.value = d.tipo;
        campos.fecha.value = d.fecha;
        campos.importe.value = d.importe;
        campos.concepto.value = d.concepto;
        asegurarOpcion(d.categoria);
        campos.categoria.value = d.categoria;
        categoriaOriginal = d.categoria;
        campos.similares.checked = false;
        campos.filaSimilares.hidden = true;
        campos.error.hidden = true;
        modal.classList.add('open');
        campos.concepto.focus();
    }

    function asegurarOpcion(valor) {
        // La categoría puede ser una antigua o propia que no esté en la lista.
        if (!valor) return;
        const existe = Array.from(campos.categoria.options).some(o => o.value === valor);
        if (!existe) {
            const o = document.createElement('option');
            o.value = valor; o.textContent = valor;
            campos.categoria.appendChild(o);
        }
    }

    function cerrar() {
        modal.classList.remove('open');
        if (ultimoFoco) ultimoFoco.focus();
    }

    document.querySelectorAll('.js-editar').forEach(b => b.addEventListener('click', () => abrir(b)));
    document.getElementById('edCancelar').addEventListener('click', cerrar);
    modal.addEventListener('click', (e) => { if (e.target === modal) cerrar(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && modal.classList.contains('open')) cerrar(); });

    campos.categoria.addEventListener('change', () => {
        const cambio = campos.categoria.value !== '' && campos.categoria.value !== categoriaOriginal;
        campos.filaSimilares.hidden = !cambio;
        if (!cambio) campos.similares.checked = false;
    });

    document.getElementById('formEditar').addEventListener('submit', async (e) => {
        e.preventDefault();
        campos.error.hidden = true;
        campos.guardar.disabled = true;
        try {
            const r = await findashFetch('/panel/editar_movimiento', {
                json: {
                    id: campos.id.value, tipo: campos.tipo.value, fecha: campos.fecha.value,
                    importe: campos.importe.value, concepto: campos.concepto.value,
                    categoria: campos.categoria.value, aplicar_similares: campos.similares.checked
                }
            });
            if (!r.ok) {
                campos.error.textContent = (r.data && r.data.error) || 'No se pudo guardar el movimiento.';
                campos.error.hidden = false;
                return;
            }
            sessionStorage.setItem('findash_toast', r.data.mensaje || 'Movimiento actualizado.');
            window.location.reload();
        } catch (err) {
            campos.error.textContent = 'Error de conexión. Inténtalo de nuevo.';
            campos.error.hidden = false;
        } finally {
            campos.guardar.disabled = false;
        }
    });

    // Mensaje tras recargar la página
    try {
        const pendiente = sessionStorage.getItem('findash_toast');
        if (pendiente) { sessionStorage.removeItem('findash_toast'); mostrarToast(pendiente, 'success'); }
    } catch (e) { /* sin sessionStorage */ }
})();
