// v1.9.5 — Idempotencia del guardado de la tabla de configuración.
//
// Ejecuta el código REAL de dashboard.js: loadConfig() dibuja la tabla con lo
// que devolvió GET /api/config y saveConfig() arma el POST leyendo los
// controles. Los controles salen del HTML que generó loadConfig, interpretado
// como lo hace el navegador: value, checked y la opción selected de cada
// <input>/<select> pasan a ser el estado inicial del control.
//
// Uso: node tools/verificar_panel_v195.js <config.json> <salida.json> [cambio...]
//   cambio = "<id del control>=<valor>"  (checkbox: true/false)
// Escribe en <salida.json> el cuerpo del POST /api/config que mandó el panel.
const fs = require('fs');
const path = require('path');

const [entrada, salida, ...cambios] = process.argv.slice(2);
const raiz = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(raiz, 'frontend', 'static', 'dashboard.js'), 'utf8');

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

// ── Mini DOM: lo justo para que el HTML generado se vuelva controles ────────
const controles = {};
const ENTIDADES = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'" };
const decodificar = t => t.replace(/&(amp|lt|gt|quot|#39);/g, m => ENTIDADES[m]);

function atributos(tag) {
  const attrs = {};
  const re = /\s([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
  let m;
  while ((m = re.exec(tag))) {
    const v = m[2] ?? m[3] ?? m[4];
    attrs[m[1].toLowerCase()] = v === undefined ? '' : decodificar(v);
  }
  return attrs;
}
// Recorre las etiquetas respetando comillas: un atributo puede tener '>' adentro.
function etiquetas(html) {
  const salidaTags = [];
  for (let i = 0; i < html.length; i++) {
    if (html[i] !== '<') continue;
    let k = i + 1, comilla = null;
    for (; k < html.length; k++) {
      const ch = html[k];
      if (comilla) { if (ch === comilla) comilla = null; }
      else if (ch === '"' || ch === "'") comilla = ch;
      else if (ch === '>') break;
    }
    salidaTags.push({ inicio: i, fin: k + 1, texto: html.slice(i, k + 1) });
    i = k;
  }
  return salidaTags;
}
function interpretar(html) {
  const tags = etiquetas(html);
  for (let t = 0; t < tags.length; t++) {
    const nombre = (tags[t].texto.match(/^<\s*([\w-]+)/) || [])[1];
    if (!nombre) continue;
    const a = atributos(tags[t].texto.replace(/^<\s*[\w-]+/, ' ').replace(/\/?>$/, ''));
    if (nombre.toLowerCase() === 'input' && a.id) {
      controles[a.id] = { id: a.id, tipo: a.type || 'text', value: a.value ?? '',
                          checked: 'checked' in a, disabled: 'disabled' in a };
    } else if (nombre.toLowerCase() === 'select' && a.id) {
      const opciones = [];
      for (let u = t + 1; u < tags.length && !/^<\s*\/select/i.test(tags[u].texto); u++) {
        if (/^<\s*option\b/i.test(tags[u].texto)) {
          const o = atributos(tags[u].texto.replace(/^<\s*option/i, ' ').replace(/>$/, ''));
          opciones.push({ value: o.value ?? '', selected: 'selected' in o });
        }
      }
      const elegida = opciones.find(o => o.selected) || opciones[0] || { value: '' };
      controles[a.id] = { id: a.id, tipo: 'select', value: elegida.value, opciones };
    }
  }
}
function nodo() {
  let html = '';
  return {
    style: {}, classList: { add() {}, remove() {}, toggle() {} },
    get innerHTML() { return html; },
    set innerHTML(v) { html = String(v); interpretar(html); },
    appendChild() {},
  };
}
global.document = {
  getElementById: id => controles[id] || (id === 'config-table-body' ? (controles[id] = nodo()) : null),
  createElement: () => nodo(),
  querySelectorAll: () => ({ forEach() {} }),
};
global.window = {};

// ── Red simulada ──────────────────────────────────────────────────────────────
const config = JSON.parse(fs.readFileSync(entrada, 'utf8'));
let enviado = null;
global.fetch = async (url, op = {}) => {
  if (url === '/api/config' && (op.method || 'GET') === 'GET') {
    return { ok: true, json: async () => JSON.parse(JSON.stringify(config)) };
  }
  if (url === '/api/config' && op.method === 'POST') {
    enviado = JSON.parse(op.body);
    return { ok: true, json: async () => ({ status: 'ok' }) };
  }
  throw new Error('fetch inesperado: ' + url);
};
let pidioContrasena = false;
global.prompt = () => { pidioContrasena = true; return 'clave-del-test'; };
global.alert = () => {};
global.currentConfigs = [];

for (const n of ['_escapeHtml', 'esPull', '_celdaClaveWebhook', '_modoAuthWebhook', '_selectorAuthWebhook',
                 '_leerAuthWebhook', '_opcionesDeModulo', '_credencialesDeModulo', '_leerCredencialesDeModulo',
                 '_leerOpcionesDeModulo', 'loadConfig', 'saveConfig']) {
  cargar(n);
}

(async () => {
  await loadConfig();
  for (const c of cambios) {
    const [id, valor] = c.split('=');
    const control = controles[id];
    if (!control) throw new Error('No existe el control ' + id);
    if (control.tipo === 'checkbox') control.checked = valor === 'true';
    else control.value = valor;
  }
  await saveConfig();
  if (!enviado) throw new Error('El panel no mandó el guardado');
  fs.writeFileSync(salida, JSON.stringify({ updates: enviado, pidio_contrasena: pidioContrasena }, null, 1));
})().catch(e => { console.error(e.stack || e); process.exit(1); });
