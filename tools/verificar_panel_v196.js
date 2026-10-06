// v1.9.6 — Píldoras de salud con el código REAL de dashboard.js.
//
// Recibe la respuesta real de GET /api/stats, hace lo mismo que el SSE del
// panel (setProviderHealth + _throughputServidor) y dibuja las píldoras con
// renderHealthChips. Devuelve, por integración, la clase de la píldora y su
// texto visible.
//
// Uso: node tools/verificar_panel_v196.js <stats.json> <salida.json>
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
function constante(n) {
  const m = src.match(new RegExp(`const ${n}\\s*=\\s*([^;]+);`));
  if (!m) throw new Error(`No encontré la constante ${n}`);
  return eval(m[1]);
}

global.VENTANA_THROUGHPUT_SEG = constante('VENTANA_THROUGHPUT_SEG');
global._providerHealth = [];
global._throughputServidor = {};
for (const n of ['_fmtAge', '_caudalPorMinuto', 'setProviderHealth', 'renderHealthChips']) {
  eval(`global.${n} = ` + extraer(n).replace(`function ${n}`, 'function'));
}

const data = JSON.parse(fs.readFileSync(entrada, 'utf8'));
// Igual que el manejador del SSE del panel.
setProviderHealth(data.provider_health);
_throughputServidor = data.throughput || {};
const html = renderHealthChips([]);

const pildoras = [];
const re = /<div class="health-chip (\w+)" title="([^"]*)">[\s\S]*?<span class="health-name">([^<]*)<\/span>[\s\S]*?<span class="health-env[^"]*">([^<]*)<\/span>[\s\S]*?<span class="health-detail">([^<]*)<\/span>/g;
let m;
while ((m = re.exec(html))) {
  pildoras.push({ clase: m[1], tooltip: m[2], proveedor: m[3], entorno: m[4], texto: m[5] });
}
fs.writeFileSync(salida, JSON.stringify(pildoras, null, 1));
