// Verifica que guardar el Integration Studio NO borre lo que el editor no
// maneja (el filtro de admisión) y que respete cuándo se emite el evento base.
// Ejecuta el código real del panel, no una copia.
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
const dom = {};
global.document = { getElementById: id => dom[id] || null,
                    querySelectorAll: () => ({ forEach: () => {} }) };
let _currentRules = [];
let _esquemaCargado = {};
function getCurrentBaseMapping() { return { chassis_number: 'DeviceName' }; }
function renderTriggerRules() {}
function _mostrarCamposAuth() {}
eval(extraer('_buildFullPayload'));
const API_BASE = '/api/config';
// El extractor toma desde 'function loadMapping(': se le devuelve el async.
eval('global.loadMapping = async ' + extraer('loadMapping').replace('function loadMapping', 'function'));

let fallas = 0;
const ok = (c, m) => { console.log(`   ${c ? 'OK  ' : 'FALLA'} ${m}`); if (!c) fallas++; };
const esquemaDe = r => (r && r.mapping) ? r.mapping : r;

(async () => {
  console.log('=== Guardar conserva el filtro de admisión ===');
  _esquemaCargado = {
    base_mapping: { chassis_number: 'DeviceName' },
    trigger_rules: [{ id: 'r1', rc_code: '=AlertType' }],
    default_rule: { enabled: true, rc_code: '1', fire_when: 'no_rule_matched' },
    admision: [{ field: 'ShipmentId', operator: 'exists' }],
  };
  _currentRules = _esquemaCargado.trigger_rules;
  dom.default_rc_code = { value: '1' }; dom.default_rc_label = { value: 'Reporte GPS' };
  dom.default_fire_when = { value: 'no_rule_matched' };
  let e = esquemaDe(_buildFullPayload());
  ok(Array.isArray(e.admision) && e.admision.length === 1, 'el filtro de admisión sigue estando');
  ok(e.default_rule.fire_when === 'no_rule_matched', 'se guarda "solo si ninguna regla coincide"');
  ok(e.trigger_rules[0].rc_code === '=AlertType', 'la regla con código desde campo se conserva');

  console.log('=== La regla base deshabilitada no se rehabilita sola ===');
  _esquemaCargado.default_rule.enabled = false;
  e = esquemaDe(_buildFullPayload());
  ok(e.default_rule.enabled === false, 'enabled=false se respeta');

  console.log('=== Cambiar el selector cambia lo guardado ===');
  dom.default_fire_when.value = 'always';
  ok(esquemaDe(_buildFullPayload()).default_rule.fire_when === 'always', 'se guarda "siempre"');

  console.log('=== Cambiar de proveedor con una carga que falla ===');
  global.fetch = async () => { throw new Error('red caída'); };
  try { await global.loadMapping('otro', 'prod'); } catch (_) {}
  e = esquemaDe(_buildFullPayload());
  ok(e.admision === undefined, 'no arrastra el filtro del proveedor anterior');

  console.log(fallas ? `\n${fallas} FALLA(S)` : '\nTodo OK');
  process.exit(fallas ? 1 : 0);
})();
