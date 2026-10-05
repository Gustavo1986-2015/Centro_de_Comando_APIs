// Verifica los ajustes del panel de la v1.9.3 ejecutando el código REAL de
// dashboard.js (no una copia), con un DOM y un fetch mínimos:
//   1. loadConfig: la columna PUSH API Key muestra "d0•••• cargada" con la
//      pista que manda el servidor, "sin clave" si no hay, N/A en PULL.
//   2. renderRecentTable: con la grilla vacía el contador dice "0 eventos".
// Uso: node tools/verificar_panel_v193.js '<configs en JSON>'
const fs = require('fs');
const src = fs.readFileSync('frontend/static/dashboard.js', 'utf8');

function extraer(n) {
  const marca = src.indexOf(`async function ${n}(`) >= 0 ? `async function ${n}(` : `function ${n}(`;
  const i = src.indexOf(marca);
  if (i < 0) throw new Error(`No encontré ${n}`);
  let nivel = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') nivel++; else if (src[k] === '}' && --nivel === 0) return src.slice(i, k + 1);
  }
}

let fallas = 0;
const ok = (c, m) => { console.log(`   ${c ? 'OK  ' : 'FALLA'} ${m}`); if (!c) fallas++; };

// ── DOM y fetch mínimos ─────────────────────────────────────────────────────
const dom = {};
function elemento(id) {
  if (!dom[id]) {
    dom[id] = { id, innerHTML: '', innerText: '', textContent: 'Cargando...', value: '', style: {},
                hijos: [], appendChild(h) { this.hijos.push(h); } };
  }
  return dom[id];
}
global.document = {
  getElementById: id => elemento(id),
  createElement: () => {
    const el = { innerHTML: '', classList: { add() {} }, cells: [], style: {} };
    Object.defineProperty(el, 'textContent', { set(t) {
      this.innerHTML = String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    } });
    return el;
  },
};
const CONFIGS = JSON.parse(process.argv[2] || '[]');
global.fetch = async () => ({ json: async () => JSON.parse(JSON.stringify(CONFIGS)) });
global.console.error = (...a) => { fallas++; console.log('   FALLA error en el panel:', ...a); };
function formatLatency(s) { return `${s}s`; }
function applyChassisSearch() {}

global.currentConfigs = [];
global.currentStatusFilter = 'all';
global.currentProviderFilter = 'all';
global.currentLatencyFilter = 'all';
global.currentSoloEventos = false;
global.allRecentEvents = [];

for (const f of ['_escapeHtml', 'esPull', '_modoAuthWebhook', '_selectorAuthWebhook', '_opcionesDeModulo',
                 '_celdaClaveWebhook', 'loadConfig', 'renderRecentTable']) {
  const codigo = extraer(f);
  const asincrona = codigo.startsWith('async');
  eval(`global.${f} = ` + codigo.replace(asincrona ? `async function ${f}` : `function ${f}`,
                                         asincrona ? 'async function' : 'function'));
}

(async () => {
  console.log('=== 1. Columna PUSH API Key (loadConfig real) ===');
  await loadConfig();
  const filas = elemento('config-table-body').hijos.map(h => h.innerHTML);
  ok(filas.length === CONFIGS.length, `una fila por integración (${filas.length})`);
  const fila = n => filas[CONFIGS.findIndex(c => c.provider_name === n)] || '';
  ok(fila('TIVE').includes('d0•••• cargada'), 'con clave: "d0•••• cargada"');
  ok(fila('TIVE').includes('btn-ver-clave'), 'con clave: el botón de revelar sigue');
  ok(fila('TIVE').includes('celda-clave-webhook'), 'la celda lleva la clase que la ensancha');
  ok(fila('SCHMITZ').includes('sin clave') && !fila('SCHMITZ').includes('cargada'), 'sin clave: lo dice');
  ok(!fila('SCHMITZ').includes('btn-ver-clave'), 'sin clave: no hay nada que revelar');
  ok(fila('PROTRACK').includes('N/A (Es PULL)') && !fila('PROTRACK').includes('cargada'), 'PULL: N/A');

  const sinPista = { ...CONFIGS[0], webhook_auth_hint: null, _originalIdx: 9 };
  ok(_celdaClaveWebhook(sinPista).includes('•••• cargada'), 'clave sin pista legible: "•••• cargada"');
  const maliciosa = { ...CONFIGS[0], webhook_auth_hint: '<b', _originalIdx: 9 };
  ok(!_celdaClaveWebhook(maliciosa).includes('<b••'), 'la pista se escapa como texto');

  console.log('=== 2. Contador de "Última Actividad Global" (renderRecentTable real) ===');
  allRecentEvents = [];
  elemento('event-count').textContent = 'Cargando...';
  renderRecentTable();
  ok(elemento('event-count').textContent === '0 eventos', `grilla vacía: "${elemento('event-count').textContent}"`);

  allRecentEvents = [{ provider: 'TIVE', status: 'sent', env: 'PROD' }];
  currentStatusFilter = 'failed';
  elemento('event-count').textContent = 'Cargando...';
  renderRecentTable();
  ok(elemento('event-count').textContent === '0 eventos', 'vacía por filtros: "0 eventos"');

  console.log(fallas ? `\n${fallas} FALLA(S)` : '\nTodo OK');
  process.exit(fallas ? 1 : 0);
})();
