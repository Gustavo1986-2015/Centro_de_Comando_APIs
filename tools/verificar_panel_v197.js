// v1.9.7 — Cómo pinta el panel un evento simulado, con el código REAL de
// dashboard.js (renderRecentTable), a partir de la respuesta real de
// GET /api/stats.
//
// Uso: node tools/verificar_panel_v197.js <stats.json> <salida.json>
// Escribe, por cada fila dibujada: id, clases de la fila y texto visible.
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

// DOM mínimo: las filas se arman con innerHTML; se guarda lo que se agrega.
const filas = [];
function nodo() {
  const clases = new Set();
  return {
    innerHTML: '', innerText: '', textContent: '', style: {}, value: '',
    classList: { add: c => clases.add(c), remove: c => clases.delete(c), contains: c => clases.has(c) },
    _clases: clases,
    appendChild(hijo) { filas.push(hijo); },
    querySelectorAll: () => [],
    addEventListener() {},
  };
}
const elementos = {};
global.document = {
  getElementById: id => (id === 'search-chassis' ? null : (elementos[id] = elementos[id] || nodo())),
  createElement: () => nodo(),
  querySelectorAll: () => [],
};
global.window = {};
global.currentStatusFilter = 'all';
global.currentProviderFilter = 'all';
global.currentLatencyFilter = 'all';
global.currentSoloEventos = false;

// Se cargan las funciones que pida renderRecentTable, a medida que aparecen.
const cargadas = new Set();
function cargar(n) {
  if (cargadas.has(n)) return;
  cargadas.add(n);
  const codigo = extraer(n);
  eval(`global.${n} = ` + codigo.replace(/^(async )?function \w+/, (m, a) => (a || '') + 'function'));
}
cargar('renderRecentTable');

const data = JSON.parse(fs.readFileSync(entrada, 'utf8'));
global.allRecentEvents = data.recent;
for (let intento = 0; intento < 30; intento++) {
  try {
    filas.length = 0;
    renderRecentTable();
    break;
  } catch (e) {
    const m = /(\w+) is not defined/.exec(e.message);
    if (!m) throw e;
    cargar(m[1]);
  }
}
const quitarHtml = h => h.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
fs.writeFileSync(salida, JSON.stringify(filas.map(f => ({
  clases: [...f._clases], texto: quitarHtml(f.innerHTML),
})), null, 1));
