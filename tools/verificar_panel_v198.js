// v1.9.8 — La grilla principal con un filtro activo y la actualización en vivo,
// con el código REAL de dashboard.js (renderStats, fetchFilteredEvents,
// renderRecentTable).
//
// Uso: node tools/verificar_panel_v198.js <entrada.json> <salida.json>
// entrada.json = {
//   "filtro": {"proveedor": "tive", "estado": "all", "solo_eventos": false},
//   "filtrada": <respuesta real de GET /api/stats?provider=...>,
//   "sse": <respuesta real de GET /api/stats sin filtros, como la manda el SSE>
// }
// Simula: el usuario eligió el filtro (fetchFilteredEvents trae la lista
// filtrada) y después llega un mensaje SSE. Informa qué quedó en la grilla.
const fs = require('fs');
const path = require('path');

const [entrada, salida] = process.argv.slice(2);
const src = fs.readFileSync(path.join(__dirname, '..', 'frontend', 'static', 'dashboard.js'), 'utf8');

function extraer(n) {
  const marca = src.indexOf(`async function ${n}(`) >= 0 ? `async function ${n}(` : `function ${n}(`;
  const i = src.indexOf(marca);
  if (i < 0) throw new Error(`No encontré ${n}`);
  let nivel = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') nivel++; else if (src[k] === '}' && --nivel === 0) return src.slice(i, k + 1);
  }
}
const cargadas = new Set();
function cargar(n) {
  if (cargadas.has(n)) return;
  cargadas.add(n);
  eval(`global.${n} = ` + extraer(n).replace(/^(async )?function \w+/, (m, a) => (a || '') + 'function'));
}

// DOM de juguete: cualquier propiedad existe; las filas de la grilla se guardan.
const filasGrilla = [];
function nodo(id) {
  const base = {
    id, innerHTML: '', innerText: '', textContent: '', value: '', checked: false, style: {}, dataset: {},
    options: [], children: [], cells: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild(h) { if (id === 'recent-table-body') filasGrilla.push(h); return h; },
    querySelectorAll: () => [], querySelector: () => nodo('_q'), closest: () => nodo('_c'), addEventListener() {}, setAttribute() {}, removeAttribute() {}, remove() {}, insertBefore(h) { return h; }, prepend() {}, append() {},
    getContext: () => new Proxy({}, { get: () => () => {} }),
  };
  return new Proxy(base, { get: (o, k) => (k in o ? o[k] : (typeof k === 'string' ? undefined : o[k])) });
}
const elementos = {};
global.document = {
  getElementById: id => (id === 'search-chassis' ? null : (elementos[id] = elementos[id] || nodo(id))),
  createElement: t => nodo('_' + t),
  createElementNS: (ns, t) => nodo('_' + t),
  querySelectorAll: () => [], querySelector: () => nodo('_q'), body: nodo('body'),
};
global.window = {};
global.localStorage = { getItem: () => null, setItem() {} };
global.alert = () => {};
global.confirm = () => false;
global.prompt = () => null;

const datos = JSON.parse(fs.readFileSync(entrada, 'utf8'));
global.currentProviderFilter = (datos.filtro.proveedor || 'all').toLowerCase();
global.currentStatusFilter = datos.filtro.estado || 'all';
global.currentSoloEventos = !!datos.filtro.solo_eventos;
global.currentLatencyFilter = 'all';
global._ultimaRecargaSoloEventos = 0;
global.allRecentEvents = [];
const pedidos = [];
global.fetch = async (url) => {
  pedidos.push(url);
  return { ok: true, json: async () => JSON.parse(JSON.stringify(datos.filtrada)) };
};

// Las funciones del panel atrapan sus errores con console.error: se capturan
// para no dar por bueno un renderStats que se cortó a mitad de camino.
const errores = [];
console.error = (...a) => errores.push(a.map(String).join(' '));
console.log = () => {};

function declaracionGlobal(nombre) {
  const m = src.match(new RegExp('\\n\\s*(?:let|const|var)\\s+' + nombre + '\\s*=\\s*([^;]+);'));
  if (!m) return {};
  try { return eval(`(${m[1]})`); } catch (_) { return {}; }
}

async function conReintentos(fn) {
  for (let intento = 0; intento < 80; intento++) {
    errores.length = 0;
    let faltante = null;
    try { await fn(); } catch (e) {
      const m = /(\w+) is not defined/.exec(e.message);
      if (!m) throw e;
      faltante = m[1];
    }
    await new Promise(r => setTimeout(r, 30));
    if (!faltante) {
      const m = errores.map(t => /(\w+) is not defined/.exec(t)).find(Boolean);
      if (!m) {
        if (errores.length) throw new Error('El panel registró un error: ' + errores.join(' | '));
        return;
      }
      faltante = m[1];
    }
    try { cargar(faltante); } catch (_) { global[faltante] = declaracionGlobal(faltante); }
  }
  throw new Error('demasiados reintentos');
}

(async () => {
  for (const n of ['fetchFilteredEvents', 'renderRecentTable', 'renderStats']) cargar(n);
  // 1) El usuario eligió el filtro: llega la lista filtrada del servidor.
  await conReintentos(() => fetchFilteredEvents());
  const antes = allRecentEvents.map(e => e.provider);
  // 2) Llega un mensaje SSE (sin filtros, los últimos de todo el hub).
  await conReintentos(() => renderStats(JSON.parse(JSON.stringify(datos.sse))));
  await new Promise(r => setTimeout(r, 50));   // la recarga filtrada es asíncrona
  filasGrilla.length = 0;
  await conReintentos(() => renderRecentTable());
  fs.writeFileSync(salida, JSON.stringify({
    proveedores_antes_del_sse: antes,
    proveedores_despues_del_sse: allRecentEvents.map(e => e.provider),
    filas_en_la_grilla: filasGrilla.length,
    pedidos,
  }, null, 1));
})().catch(e => { process.stderr.write(String(e.stack || e)); process.exit(1); });
