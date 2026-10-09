// v1.9.8 — Visor de base de datos y descartes con el código REAL de
// dashboard.js: consulta, celda completa, Copiar, fila completa, descarga CSV,
// origen Respaldo, columna de coordenadas de los descartes y ancho del campo
// "RC Usuario".
//
// Uso: node tools/verificar_visor_v198.js <entrada.json> <salida.json>
// entrada.json = {
//   "base":     {"db": "...", "tabla": "...", "filtros": {...}, "respuesta": <GET /api/db-viewer/query real>,
//                "fila": 0, "columna": "raw_data"},
//   "respaldo": {"integracion": "...", "desde": "...", "hasta": "...", "filtros": {...},
//                "respuesta": <GET /api/db-viewer/respaldo real>},
//   "descartes": <GET /api/diagnostico/descartes real>,
//   "usuario_rc": "AC_avl_SchmitzCargoBull",
//   "contexto_seguro": true
// }
const fs = require('fs');
const path = require('path');

const [entrada, salida] = process.argv.slice(2);
const src = fs.readFileSync(path.join(__dirname, '..', 'frontend', 'static', 'dashboard.js'), 'utf8');
const datos = JSON.parse(fs.readFileSync(entrada, 'utf8'));

function extraer(n) {
  const marca = src.indexOf(`async function ${n}(`) >= 0 ? `async function ${n}(` : `function ${n}(`;
  const i = src.indexOf(marca);
  if (i < 0) throw new Error(`No encontré ${n}`);
  let nivel = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') nivel++; else if (src[k] === '}' && --nivel === 0) return src.slice(i, k + 1);
  }
}
function cargar(n) {
  eval(`global.${n} = ` + extraer(n).replace(/^(async )?function \w+/, (m, a) => (a || '') + 'function'));
}

// DOM de juguete. El div de _escapeHtml escapa como el navegador (&, <, >).
const escapar = t => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const elementos = {};
function nodo(id) {
  let texto = '';
  return {
    id, value: '', innerHTML: '', style: {}, dataset: {}, options: [], selectedIndex: 0,
    get textContent() { return texto; },
    set textContent(v) { texto = String(v); if (id === '_div') this.innerHTML = escapar(v); },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, setAttribute() {}, appendChild(h) { return h; }, select() {}, focus() {},
  };
}
const portapapeles = [];
let ultimoTextarea = null;
global.document = {
  getElementById: id => (elementos[id] = elementos[id] || nodo(id)),
  createElement: t => { const n = nodo('_' + t); if (t === 'textarea') ultimoTextarea = n; return n; },
  body: { appendChild(h) { return h; }, removeChild() {} },
  execCommand: cmd => { if (cmd === 'copy' && ultimoTextarea) { portapapeles.push({ via: 'execCommand', texto: ultimoTextarea.value }); return true; } return false; },
};
global.window = { isSecureContext: datos.contexto_seguro !== false, location: { href: '' } };
// Node trae su propio navigator (solo lectura): se reemplaza con defineProperty.
Object.defineProperty(globalThis, 'navigator', { configurable: true, writable: true,
  value: { clipboard: { writeText: async t => { portapapeles.push({ via: 'clipboard', texto: t }); } } } });
global.alert = m => { throw new Error('alert inesperado: ' + m); };
global.console.error = (...a) => { throw new Error('console.error: ' + a.join(' ')); };

const pedidos = [];
let respuestaSiguiente = null;
global.fetch = async url => {
  pedidos.push(url);
  const r = respuestaSiguiente;
  return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(r)) };
};

for (const n of ['_escapeHtml', '_escAttr', '_origenBd', '_parametrosVisorBd', 'loadQueryData',
                 '_asegurarBuscadorBd', '_textoCeldaBd', '_textoCompletoBd', 'renderVisorBd',
                 'verCeldaBd', 'cerrarDetalleBd', '_copiarTexto', 'copiarCeldaBd', '_filaComoJsonBd',
                 'copiarFilaBd', 'descargarVistaBd', '_renderPagination', 'goToPage',
                 '_coordDescarte', '_horaDescarte', '_renderDescartes', '_anchoUsuarioRc']) cargar(n);
global._dbPage = { size: 50, offset: 0, total: 0 };
global._dbEditorState = { db: null, table: null, editable: false, pendingEdit: null };
global._dbVista = null;
global._dbSeleccion = null;
global._dbUltimaConsulta = null;
global.window.searchDbTimeout = null;

// Celdas de la grilla tal como quedaron en el HTML: [{atributos, contenido}].
function celdas(html) {
  return html.split('<tr>').slice(1).map(fila =>
    [...fila.matchAll(/<td([^>]*)>([\s\S]*?)<\/td>/g)].map(m => ({ atributos: m[1], contenido: m[2] })));
}
function ponerFiltros(f) {
  for (const [k, id] of Object.entries({ estado: 'db-f-estado', patente: 'db-f-patente', envio: 'db-f-envio',
                                          desde: 'db-f-desde', hasta: 'db-f-hasta' })) {
    document.getElementById(id).value = (f && f[k]) || '';
  }
}

(async () => {
  const resultado = {};

  // 1) Origen Base: consulta, celda completa, copiar, fila completa, descarga.
  const b = datos.base;
  document.getElementById('db-origen').value = 'base';
  document.getElementById('db-select').value = b.db;
  document.getElementById('table-select').value = b.tabla;
  ponerFiltros(b.filtros);
  respuestaSiguiente = b.respuesta;
  await loadQueryData();
  resultado.base = {
    pedido: pedidos[pedidos.length - 1],
    encabezado: document.getElementById('db-viewer-thead').innerHTML,
    filas: celdas(document.getElementById('db-viewer-tbody').innerHTML),
    info: document.getElementById('db-viewer-info').textContent,
  };
  const col = _dbVista.columns.indexOf(b.columna);
  verCeldaBd(b.fila, col);
  resultado.base.detalle_titulo = document.getElementById('db-detalle-titulo').textContent;
  resultado.base.detalle_valor = document.getElementById('db-detalle-valor').textContent;
  resultado.base.detalle_visible = document.getElementById('db-detalle').style.display;
  await copiarCeldaBd();
  await copiarFilaBd();
  resultado.base.copiado = portapapeles.slice();
  descargarVistaBd();
  resultado.base.descarga = window.location.href;

  // Página siguiente: mismo filtro, offset corrido (no vuelve al inicio).
  if (b.respuesta.total > _dbPage.size) {
    goToPage('next');
    await new Promise(r => setTimeout(r, 20));
    resultado.base.pedido_pagina_2 = pedidos[pedidos.length - 1];
  }

  // 2) Origen Respaldo.
  const r = datos.respaldo;
  if (r) {
    portapapeles.length = 0;
    document.getElementById('db-origen').value = 'respaldo';
    document.getElementById('db-respaldo-integracion').value = r.integracion;
    ponerFiltros({ ...r.filtros, desde: r.desde, hasta: r.hasta });
    _dbPage.offset = 0;
    respuestaSiguiente = r.respuesta;
    await loadQueryData();
    resultado.respaldo = {
      pedido: pedidos[pedidos.length - 1],
      filas: celdas(document.getElementById('db-viewer-tbody').innerHTML),
      info: document.getElementById('db-viewer-info').textContent,
      insignia: document.getElementById('db-edit-badge').textContent,
    };
    descargarVistaBd();
    resultado.respaldo.descarga = window.location.href;
  }

  // 3) Descartes con coordenadas.
  if (datos.descartes) {
    const html = _renderDescartes(datos.descartes);
    const tablas = html.split('<table');
    resultado.descartes = {
      encabezado_ultimos: tablas[2] ? tablas[2].split('</thead>')[0] : '',
      filas_ultimos: tablas[2] ? celdas(tablas[2]).filter(f => f.length).map(f => f.map(c => c.contenido.trim())) : [],
    };
  }

  // 4) Ancho del campo "RC Usuario".
  if (datos.usuario_rc !== undefined) resultado.ancho_usuario_rc = _anchoUsuarioRc(datos.usuario_rc);

  fs.writeFileSync(salida, JSON.stringify(resultado, null, 1));
})().catch(e => { process.stderr.write(String(e.stack || e)); process.exit(1); });
