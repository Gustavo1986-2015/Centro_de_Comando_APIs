const fs = require('fs');
const src = fs.readFileSync('frontend/static/dashboard.js', 'utf8');
function extraer(n) {
  const i = src.indexOf(`function ${n}(`); let nivel = 0;
  for (let k = src.indexOf('{', i); k < src.length; k++) {
    if (src[k] === '{') nivel++; else if (src[k] === '}' && --nivel === 0) return src.slice(i, k + 1);
  }
}
// DOM mínimo: el selector y el header de cada fila.
const dom = {};
global.document = { getElementById: id => dom[id] || null };
eval(['_modoAuthWebhook','_selectorAuthWebhook','_alCambiarAuthWebhook','_leerAuthWebhook','_mostrarCamposAuth'].map(extraer).join('\n'));

let fallas = 0;
const ok = (cond, msg) => { console.log(`   ${cond ? 'OK  ' : 'FALLA'} ${msg}`); if (!cond) fallas++; };

console.log('=== Proveedor histórico (sin configuración) ===');
const hist = { _originalIdx: 0, webhook_auth_config: null };
ok(_modoAuthWebhook(hist) === 'header', 'se lee como "Clave fija"');
ok(_selectorAuthWebhook(hist).includes('value="header" selected'), 'el selector arranca en "Clave fija"');
dom['webhook_mode_0'] = { value: 'header' };
// v1.9.5: sin tocar el selector no se manda nada y lo guardado queda como
// está (antes se escribía {modo: header} donde no había nada).
ok(_leerAuthWebhook(hist, 0) === null, 'sin tocar el selector no se manda (queda como está)');
dom['webhook_mode_0'] = { value: 'hmac:tive' };
ok(JSON.stringify(_leerAuthWebhook(hist, 0)) === '{"modo":"hmac","preset":"tive"}', 'cambiado a Firma Tive se manda el preset');

console.log('=== Tive ===');
const tive = { _originalIdx: 1, webhook_auth_config: { modo: 'hmac', preset: 'tive' } };
ok(_modoAuthWebhook(tive) === 'hmac:tive', 'se lee como "Firma Tive"');
dom['webhook_mode_1'] = { value: 'hmac:tive' };
ok(_leerAuthWebhook(tive, 1) === null, 'sin tocar no se manda: conserva el preset y sus ajustes');
dom['webhook_mode_1'] = { value: 'header' };
ok(JSON.stringify(_leerAuthWebhook(tive, 1)) === '{"modo":"header"}', 'cambiado a Clave fija se manda header');

console.log('=== Cambiar a Firma Tive completa el header ===');
dom['webhook_header_2'] = { value: 'x-api-key' };
_alCambiarAuthWebhook(2, 'hmac:tive');
ok(dom['webhook_header_2'].value === 'x-tive-signature', 'el header pasa a x-tive-signature');
_alCambiarAuthWebhook(2, 'header');
ok(dom['webhook_header_2'].value === 'x-api-key', 'volver a "Clave fija" lo restaura');

console.log('=== Un esquema HMAC armado a mano no se pierde ===');
const custom = { _originalIdx: 3, webhook_auth_config: { modo: 'hmac', header: 'x-f', patron: 'p', contenido: '{body}', codificacion: 'hex' } };
ok(_selectorAuthWebhook(custom).includes('personalizada'), 'se ofrece conservarlo');
dom['webhook_mode_3'] = { value: 'hmac:personalizado' };
ok(_leerAuthWebhook(custom, 3) === null, 'al guardar no se manda: se conserva intacto');

console.log('=== PULL no se toca ===');
ok(_leerAuthWebhook({ _originalIdx: 9 }, 9) === null, 'sin selector -> null (no tocar lo guardado)');

console.log('=== Campos de OAuth2 ===');
dom['pullAuthFields'] = { style: {} }; dom['pullOAuthFields'] = { style: {} };
_mostrarCamposAuth('pull', 'oauth2_client_credentials');
ok(dom['pullOAuthFields'].style.display === 'block', 'OAuth2 muestra token_url y formato');
_mostrarCamposAuth('pull', 'protrack');
ok(dom['pullOAuthFields'].style.display === 'none', 'Protrack los oculta');
_mostrarCamposAuth('pull', 'none');
ok(dom['pullAuthFields'].style.display === 'none', 'sin autenticación oculta todo');

console.log(fallas ? `\n${fallas} FALLA(S)` : '\nTodo OK');
process.exit(fallas ? 1 : 0);
