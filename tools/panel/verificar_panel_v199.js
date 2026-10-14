// v1.9.9 — Ejecuta funciones REALES de dashboard.js con argumentos dados y
// devuelve lo que retornan (HTML o datos). Las funciones que una llamada
// necesite se cargan solas, por nombre, a medida que aparecen.
//
// Uso: node tools/panel/verificar_panel_v199.js <entrada.json> <salida.json>
// entrada.json = {
//   "globales": {"nombre": valor, ...},            // estado previo opcional
//   "llamadas": [{"fn": "_renderDescartes", "args": [ ... ]}, ...]
// }
// salida.json = {"resultados": [ ... ], "fetch": [urls pedidas], "consola": [...]}
const fs = require('fs');
const path = require('path');

const [entrada, salida] = process.argv.slice(2);
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'frontend', 'static', 'dashboard.js'), 'utf8');
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
function declaracionGlobal(nombre) {
  const m = src.match(new RegExp('\\n\\s*(?:let|const|var)\\s+' + nombre + '\\s*=\\s*([^;]+);'));
  if (!m) return undefined;
  try { return eval(`(${m[1]})`); } catch (_) { return undefined; }
}

// DOM de juguete: el div de _escapeHtml escapa como el navegador (&, <, >).
const escapar = t => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const elementos = {};
function nodo(id) {
  let texto = '';
  return {
    id, value: '', innerHTML: '', style: {}, dataset: {}, options: [], children: [],
    get textContent() { return texto; },
    set textContent(v) { texto = String(v); if (id === '_div') this.innerHTML = escapar(v); },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, setAttribute() {}, appendChild(h) { this.children.push(h); return h; }, querySelectorAll: () => [],
    querySelector: () => null, focus() {}, select() {},
  };
}
// v1.9.9 (3.8): querySelectorAll('.clase') devuelve las casillas que el panel
// dibujó (en el innerHTML de cualquier elemento), con su estado; el paso
// {"marcar": {"selector": ".clase", "i": "0", "checked": false}} las cambia.
const casillas = {};
function consultar(sel) {
  const m = /^\.([\w-]+)$/.exec(sel);
  if (!m) return [];
  const html = Object.values(elementos).map(e => e.innerHTML || '').join(' ');
  const vistas = [];
  for (const etiqueta of html.match(new RegExp(`<input[^>]*class="${m[1]}"[^>]*>`, 'g')) || []) {
    const i = (/data-i="([^"]*)"/.exec(etiqueta) || [])[1];
    const clave = `${sel}|${i}`;
    if (!casillas[clave]) casillas[clave] = { checked: /\schecked[\s>]/.test(etiqueta), dataset: { i }, value: '' };
    vistas.push(casillas[clave]);
  }
  return vistas;
}
global.document = {
  getElementById: id => (elementos[id] = elementos[id] || nodo(id)),
  createElement: t => nodo('_' + t),
  querySelectorAll: consultar, querySelector: () => null, body: nodo('body'),
};
global.window = { location: { href: '' } };
const consola = [];
console.error = (...a) => consola.push(['error', a.map(String).join(' ')]);
console.warn = (...a) => consola.push(['warn', a.map(String).join(' ')]);
global.alert = m => consola.push(['alert', String(m)]);
console.log = (...a) => consola.push(['log', a.map(String).join(' ').slice(0, 300)]);
const pedidos = [];
global.fetch = async (url, opciones) => {
  pedidos.push({ url, opciones });
  const r = (datos.respuestas || {})[url.split('?')[0]] || { status: 200, cuerpo: {} };
  return { ok: r.status < 400, status: r.status, json: async () => JSON.parse(JSON.stringify(r.cuerpo)) };
};
for (const [k, v] of Object.entries(datos.globales || {})) global[k] = v;
// Funciones reemplazadas por una vacía (ej. una recarga que no interesa al caso).
for (const n of datos.stubs || []) global[n] = () => {};
// Funciones a cargar de antemano: así una llamada con efectos no se repite a
// medias por una dependencia que faltaba.
for (const n of datos.cargar || []) cargar(n);
global.setTimeout = (fn) => 0;
global.requestAnimationFrame = (fn) => 0;   // los temporizadores del panel no corren en el caso

async function llamar(fn, args) {
  for (let intento = 0; intento < 60; intento++) {
    try {
      if (typeof global[fn] !== 'function') cargar(fn);
      const antes = consola.length;
      const valor = await global[fn](...JSON.parse(JSON.stringify(args)));
      // Funciones que atrapan sus errores (renderStats): la dependencia que
      // faltaba aparece en la consola. Se carga y se repite la llamada.
      const falta = consola.slice(antes).map(c => /(\w+) is not defined/.exec(c[1])).find(Boolean);
      if (falta) {
        consola.splice(antes);
        throw new ReferenceError(`${falta[1]} is not defined`);
      }
      return valor;
    } catch (e) {
      const m = /(\w+) is not defined/.exec(e.message);
      if (!m) throw e;
      try { cargar(m[1]); } catch (_) {
        const v = declaracionGlobal(m[1]);
        if (v === undefined) throw e;
        global[m[1]] = v;
      }
    }
  }
  throw new Error('demasiados reintentos');
}

(async () => {
  const resultados = [];
  for (const paso of datos.llamadas) {
    // {"set": {"id": ..., "value": ..., "checked": ...}}: lo que haría el usuario en un campo.
    if (paso.set) { Object.assign(document.getElementById(paso.set.id), paso.set); resultados.push(null); continue; }
    if (paso.marcar) {
      const c = consultar(paso.marcar.selector).find(x => x.dataset.i === String(paso.marcar.i));
      if (!c) throw new Error(`No hay casilla ${paso.marcar.selector} ${paso.marcar.i}`);
      c.checked = paso.marcar.checked; resultados.push(null); continue;
    }
    resultados.push(await llamar(paso.fn, paso.args || []));
  }
  fs.writeFileSync(salida, JSON.stringify({ resultados, fetch: pedidos, consola,
    elementos: Object.fromEntries(Object.entries(elementos).map(([k, v]) => [k, { innerHTML: v.innerHTML, value: v.value, textContent: v.textContent, style: v.style,
      hijos: v.children.map(h => h.innerHTML) }])) }, null, 1));
})().catch(e => { process.stderr.write(String(e.stack || e)); process.exit(1); });
