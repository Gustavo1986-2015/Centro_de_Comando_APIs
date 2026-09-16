// Ejecuta las funciones de render con la respuesta real del endpoint.
// `node --check` solo valida sintaxis: una variable no definida pasa el chequeo
// y explota recién al ejecutarse, que es lo que ocurrió con avisoSla.
const fs = require('fs');
const src = fs.readFileSync('frontend/static/dashboard.js', 'utf8');
const datos = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

// Se extraen solo las funciones de render y sus ayudantes.
function extraer(nombre) {
  const i = src.indexOf(`function ${nombre}(`);
  if (i === -1) throw new Error(`No se encontró ${nombre}`);
  let nivel = 0, j = src.indexOf('{', i);
  for (let k = j; k < src.length; k++) {
    if (src[k] === '{') nivel++;
    else if (src[k] === '}') { nivel--; if (nivel === 0) return src.slice(i, k + 1); }
  }
  throw new Error(`Bloque sin cerrar en ${nombre}`);
}

const cuerpo = ['_edad', '_bloqueRetrasoBucle', '_tablaPorHora'].map(extraer).join('\n');

// Se reconstruye el tramo que arma el HTML del diagnóstico.
const ini = src.indexOf('                // Poblar el selector');
const fin = src.indexOf('            } catch (e) {', ini);
let render = src.slice(ini, fin);
render = render.replace(/document\.getElementById\([^)]*\)/g, '({ value:"", innerHTML:"", textContent:"" })');
render = render.replace(/\bcont\.innerHTML\s*=/g, 'globalThis.SALIDA =');
render = render.replace(/\binfo\.textContent\s*=/, 'globalThis.INFO =');
// `sel` y `cont` se declaran dentro del tramo extraido; `info` no.
render = 'const info = { textContent: "" };\nconst cont = { innerHTML: "" };\n' + render;

// Se envuelve en funcion para que los `return` tempranos sean validos.
eval(`${cuerpo}\n(function(){\nconst d = ${JSON.stringify(datos)};\n${render}\n})();`);

if (!globalThis.SALIDA || globalThis.SALIDA.length < 200) {
  console.error('El render produjo HTML vacío o demasiado corto');
  process.exit(1);
}
for (const obligatorio of ['Distribución', 'Evolución por hora', 'Retraso del bucle']) {
  if (!globalThis.SALIDA.includes(obligatorio)) {
    console.error(`Falta la sección: ${obligatorio}`);
    process.exit(1);
  }
}
console.log(`render OK — ${globalThis.SALIDA.length} caracteres, todas las secciones presentes`);
