// Verifica el panel de la v1.9.4 ejecutando el código REAL de dashboard.js:
//   - Descartes: switchView('diagnostico') llama a cargarDescartes, que pide
//     el endpoint y dibuja conteo y últimos (equipo, envío, motivo, AlertId).
//   - Credenciales de la API de Tive: el client_id (texto libre con espacios)
//     se muestra; el secreto nunca; vacío = mantener.
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
function cargar(n) {
  const codigo = extraer(n);
  const asincrona = codigo.startsWith('async');
  eval(`global.${n} = ` + codigo.replace(asincrona ? `async function ${n}` : `function ${n}`,
                                          asincrona ? 'async function' : 'function'));
}

let fallas = 0;
const ok = (c, m) => { console.log(`   ${c ? 'OK  ' : 'FALLA'} ${m}`); if (!c) fallas++; };

const dom = {};
function elemento(id) {
  if (!dom[id]) dom[id] = { id, innerHTML: '', innerText: '', textContent: '', value: '', checked: false,
                            style: {}, classList: { add() {}, remove() {}, toggle() {} } };
  return dom[id];
}
global.document = {
  getElementById: id => elemento(id),
  querySelectorAll: () => ({ forEach: () => {} }),
  createElement: () => {
    const el = { innerHTML: '' };
    Object.defineProperty(el, 'textContent', { set(t) {
      this.innerHTML = String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    } });
    return el;
  },
};
global.window = { scrollTo() {} };
global.localStorage = { getItem() { return null; }, setItem() {} };

const pedidos = [];
const DESCARTES = {
  resumen: [{ proveedor: 'tive', env: 'prod', origen: 'tive', motivo: 'posición de tracker, va por RC directo', total: 1153, ultimo: 1791200000 },
            { proveedor: 'schmitz', env: 'prod', origen: 'contrato', motivo: 'falta fecha', total: 2, ultimo: 1791200100 }],
  ultimos: [{ ts: 1791200100, proveedor: 'tive', env: 'prod', origen: 'tive', motivo: 'duplicado de ShockEvents (puntual) ya recibido',
              detalle: null, equipo: 'K393478', envio: 'VIAJE DE PRUEBAS DE ALERTAS', alert_id: 'f2769c5b-c9ca' },
            { ts: 1791200000, proveedor: 'studio', env: 'prod', origen: 'admision', motivo: 'solo eventos con envío',
              detalle: null, equipo: '<img src=x>', envio: null, alert_id: null }],
  perdidos: 0, retencion_dias: 7, max_filas: 50000,
};
global.fetch = async (url) => { pedidos.push(url); return { ok: true, json: async () => DESCARTES }; };

for (const n of ['_escapeHtml', '_horaDescarte', '_renderDescartes', 'cargarDescartes', '_credencialesDeModulo',
                 '_leerCredencialesDeModulo']) cargar(n);

// switchView real, con el resto de las cargas como funciones vacías: lo que
// interesa es que la vista de diagnóstico llame a cargarDescartes.
let llamadaDescartes = 0;
const original = global.cargarDescartes;
for (const n of ['cargarComportamientoRC', 'cargarDiagnosticoLatencia', 'cargarInventarioDeDatos',
                 'cargarOpcionesDeExportacion', 'cargarPrecedencia', 'cargarRedSeguridad', 'initSSE',
                 'initVehicleProviderDropdown', 'loadConfig', 'loadDatabases', 'loadDbStats', 'loadHistory',
                 'loadIntegrationStudio', 'loadMonitor', 'loadRetentionConfig', 'loadSimulator', 'loadVehicles',
                 'startConsole', 'stopConsole', 'toggleMenu']) global[n] = () => {};
global.cargarDescartes = async () => { llamadaDescartes++; return original(); };
cargar('switchView');

(async () => {
  console.log('=== Descartes en Diagnóstico y Salud ===');
  switchView('diagnostico');
  await new Promise(r => setTimeout(r, 20));
  ok(llamadaDescartes === 1, "switchView('diagnostico') llama a cargarDescartes");
  ok(pedidos.some(u => u.startsWith('/api/diagnostico/descartes')), 'y pide el endpoint de descartes');
  const html = elemento('descartes-contenedor').innerHTML;
  ok(html.includes('posición de tracker, va por RC directo') && (html.includes('1,153') || html.includes('1.153')),
     'conteo por motivo');
  ok(html.includes('K393478') && html.includes('VIAJE DE PRUEBAS DE ALERTAS'), 'equipo y envío');
  ok(html.includes('f2769c5b-c9ca'), 'AlertId');
  ok(html.includes('SCHMITZ') && html.includes('contrato'), 'integración y origen');
  ok(!html.includes('<img src=x>'), 'los textos se escapan');
  ok(_renderDescartes({ resumen: [], ultimos: [], retencion_dias: 7 }).includes('Sin descartes'), 'vacío: lo dice');

  console.log('=== Credenciales de la API de Tive ===');
  const tive = { _originalIdx: 4, module_credentials: { client_id: 'Envios Assistcargo', secreto_cargado: true } };
  const cred = _credencialesDeModulo(tive);
  ok(cred.includes('value="Envios Assistcargo"'), 'el client_id con espacios se muestra tal cual');
  ok(cred.includes('credenciales cargadas'), 'estado: cargadas');
  ok(/type="password" id="modcred_4_client_secret"/.test(cred) && !/client_secret"[^>]*value="[^"]+"/.test(cred),
     'el secreto nunca se dibuja');
  ok(_credencialesDeModulo({ _originalIdx: 1 }) === '', 'una integración sin API no muestra el formulario');
  ok(_credencialesDeModulo({ _originalIdx: 2, module_credentials: { client_id: null, secreto_cargado: false } })
       .includes('sin credenciales'), 'sin credenciales: lo dice');

  elemento('modcred_4_client_id').value = 'Envios Assistcargo';
  elemento('modcred_4_client_secret').value = '';
  ok(_leerCredencialesDeModulo(tive, 4) === null, 'sin cambios no se manda nada (no pisa lo guardado)');
  elemento('modcred_4_client_secret').value = 's3cr3t';
  const leido = _leerCredencialesDeModulo(tive, 4);
  ok(leido && leido.client_id === 'Envios Assistcargo' && leido.client_secret === 's3cr3t', 'secreto nuevo: se manda');
  elemento('modcred_4_client_id').value = 'Otro Cliente';
  elemento('modcred_4_client_secret').value = '';
  const soloId = _leerCredencialesDeModulo(tive, 4);
  ok(soloId && soloId.client_id === 'Otro Cliente' && soloId.client_secret === null, 'solo el client_id: secreto se mantiene');

  console.log(fallas ? `\n${fallas} FALLA(S)` : '\nTodo OK');
  process.exit(fallas ? 1 : 0);
})();
