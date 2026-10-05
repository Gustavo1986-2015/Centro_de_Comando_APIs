// Verifica los cambios del panel de la v1.9.2 ejecutando el código REAL de
// dashboard.js, no una copia:
//   - "Valor de Activación" vacío y deshabilitado con existe / no existe
//   - grilla: velocidad N/A, humedad, temperatura con 2 decimales, envío bajo
//     la patente, etiqueta de evento y filtro "solo eventos"
//   - interruptores del módulo dedicado en la tabla de configuración
const fs = require('fs');
const src = fs.readFileSync('frontend/static/dashboard.js', 'utf8');

function extraer(n) {
  const i = src.indexOf(`function ${n}(`);
  if (i < 0) throw new Error(`No encontré ${n}`);
  let nivel = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') nivel++; else if (src[k] === '}' && --nivel === 0) return src.slice(i, k + 1);
  }
}

let fallas = 0;
const ok = (c, m) => { console.log(`   ${c ? 'OK  ' : 'FALLA'} ${m}`); if (!c) fallas++; };

// ── DOM mínimo ──────────────────────────────────────────────────────────────
const dom = {};
function elemento(id) {
  if (!dom[id]) {
    dom[id] = {
      id, innerHTML: '', innerText: '', textContent: '', value: '', checked: false, style: {},
      hijos: [],
      appendChild(h) { this.hijos.push(h); },
      querySelectorAll() { return []; },
      classList: { toggle() {}, add() {} },
    };
  }
  return dom[id];
}
global.document = {
  getElementById: id => elemento(id),
  createElement: () => {
    const el = { innerHTML: '', classList: { add() {} }, cells: [], style: {} };
    // Como el navegador: asignar textContent deja el texto escapado en innerHTML.
    Object.defineProperty(el, 'textContent', { set(t) {
      this.innerHTML = String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    } });
    return el;
  },
  querySelectorAll: () => ({ forEach: () => {} }),
};
function formatLatency(s) { return `${s}s`; }
function applyChassisSearch() {}
function updateMapCodeVisual() {}

// Las globales que usan las funciones extraídas.
global.currentStatusFilter = 'all';
global.currentProviderFilter = 'all';
global.currentLatencyFilter = 'all';
global.currentSoloEventos = false;
global.allRecentEvents = [];
global._currentRules = [];

const OPERADORES = src.match(/const _OPERADORES_SIN_VALOR = \[[^\]]*\];/);
if (!OPERADORES) throw new Error('No encontré _OPERADORES_SIN_VALOR');
eval(OPERADORES[0].replace('const ', 'global.'));
for (const f of ['_escapeHtml', '_inputValorRegla', 'renderTriggerRules', 'updateRule',
                 'renderRecentTable', '_opcionesDeModulo', '_credencialesDeModulo', '_leerOpcionesDeModulo']) {
  eval(`global.${f} = ` + extraer(f).replace(`function ${f}`, 'function'));
}

// ── Valor de Activación ─────────────────────────────────────────────────────
console.log('=== Valor de Activación con existe / no existe ===');
for (const op of ['exists', 'not_exists']) {
  const html = _inputValorRegla({ id: 'r1', operator: op, value: '1' });
  ok(/disabled/.test(html) && /value=""/.test(html), `${op}: vacío y deshabilitado`);
  ok(!/value="1"/.test(html), `${op}: no muestra "1"`);
}
const eq = _inputValorRegla({ id: 'r1', operator: 'eq', value: '5' });
ok(!/disabled/.test(eq) && /value="5"/.test(eq), 'eq: editable y con su valor');

_currentRules = [{ id: 'r1', field: 'AlertType', operator: 'eq', value: '1', enabled: true }];
updateRule('r1', 'operator', 'exists');
ok(_currentRules[0].value === '', 'pasar a "existe" vacía el valor guardado');
ok(/disabled/.test(elemento('trigger-rules-list').innerHTML), 'y la fila se vuelve a dibujar deshabilitada');

// ── Grilla de eventos ───────────────────────────────────────────────────────
console.log('=== Grilla de eventos ===');
function evento(extra) {
  return Object.assign({
    id: 1, chassis: 'K393478', status: 'sent', provider: 'TIVE', env: 'PROD', device_date: 'x',
    coords: '4.65, -74.07', course: null, altitude: null, speed: null, ignition: 'N/A',
    battery: 80, temperature: 12.371094, humidity: 85.3, odometer: null, code: 'ShockEvents',
    es_evento: true, shipment: 'VIAJE DE PRUEBAS DE ALERTAS', serial: '865648069052454',
    rc_format: {}, raw_data: '{}', time_received: 't', retry_count: 0,
  }, extra || {});
}
allRecentEvents = [
  evento(),
  evento({ id: 2, chassis: 'AB1', provider: 'PROTRACK', code: '1', es_evento: false, speed: 0,
           shipment: null, temperature: null, humidity: null }),
];
const tbody = elemento('recent-table-body');
tbody.hijos = [];
renderRecentTable();
const [fila1, fila2] = tbody.hijos.map(h => h.innerHTML);
ok(/Velocidad: <span[^>]*>N\/A<\/span>/.test(fila1), 'velocidad no medida → N/A');
ok(/Velocidad: <span[^>]*>0 km\/h/.test(fila2), 'velocidad medida en 0 → 0 km/h');
ok(/Humedad: <span[^>]*>85.3%/.test(fila1), 'humedad visible');
ok(/12\.37°/.test(fila1) && !/12\.371094°/.test(fila1), 'temperatura con 2 decimales en pantalla');
ok(/Envío: VIAJE DE PRUEBAS DE ALERTAS/.test(fila1), 'envío bajo la patente');
ok(/etiqueta-evento[^>]*>⚡ ShockEvents/.test(fila1), 'etiqueta de evento');
ok(!/etiqueta-evento/.test(fila2), 'una posición no lleva etiqueta');

currentSoloEventos = true;
tbody.hijos = [];
renderRecentTable();
ok(tbody.hijos.length === 1 && /K393478/.test(tbody.hijos[0].innerHTML), '"solo eventos" deja solo la alerta');
ok(/SOLO EVENTOS/.test(elemento('current-filter-label').innerText), 'y lo indica en el rótulo de filtros');
currentSoloEventos = false;

// ── Interruptores del módulo dedicado ───────────────────────────────────────
console.log('=== Interruptores del módulo ===');
const tive = { _originalIdx: 3, modulo_dedicado: true,
               module_options: { posiciones_terceros: true, alertas_beacons: true, alertas_trackers: false },
               module_options_labels: { posiciones_terceros: 'Posiciones de contenedores y aéreas',
                                        alertas_beacons: 'Alertas de beacons', alertas_trackers: 'Alertas de trackers' } };
const html = _opcionesDeModulo(tive);
ok((html.match(/type="checkbox"/g) || []).length === 3, 'tres interruptores');
ok(/modopt_3_alertas_trackers"[^>]*\n?\s*>/.test(html) && !/modopt_3_alertas_trackers"[\s\S]{0,80}checked/.test(html),
   'alertas de trackers apagado');
ok(/modopt_3_posiciones_terceros"[\s\S]{0,80}checked/.test(html), 'posiciones de terceros encendido');
ok(/Posiciones de contenedores y aéreas/.test(html), 'con su descripción');
ok(/—/.test(_opcionesDeModulo({ modulo_dedicado: false })), 'una integración del Studio no tiene interruptores');

elemento('modopt_3_posiciones_terceros').checked = true;
elemento('modopt_3_alertas_beacons').checked = false;
elemento('modopt_3_alertas_trackers').checked = true;
const leidas = _leerOpcionesDeModulo(tive, 3);
ok(JSON.stringify(leidas) === JSON.stringify({ posiciones_terceros: true, alertas_beacons: false, alertas_trackers: true }),
   'se leen los interruptores para guardar');
ok(_leerOpcionesDeModulo({ modulo_dedicado: false }, 0) === null, 'el Studio no manda interruptores');

console.log(fallas ? `\n${fallas} FALLA(S)` : '\nTodo OK');
process.exit(fallas ? 1 : 0);
