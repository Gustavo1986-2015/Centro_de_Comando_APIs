"""
Secretos: enmascarado para logs, panel y errores (v1.9.9, B-12) y cifrado de
las credenciales del sincronizador de diccionario (v1.9.9, punto 2.4).

ENMASCARADO
Un token, una clave o un secreto se muestran con sus 2 primeros y sus 2
últimos caracteres ("A1••••02"): alcanza para reconocer cuál es, o ver que
cambió, sin exponerlo. Uno de menos de 8 caracteres no muestra nada.

Antes la consola del panel enmascaraba (dejando 4 caracteres) pero el archivo
de log y la píldora de salud no: un error de Protrack dejaba
`...track?access_token=<token completo>` en los dos (auditoría, B-12), y cada
consulta de httpx lo escribía en el log. Ahora el mismo enmascarado se aplica
en un solo lugar para todos: un filtro en los handlers de log (archivo y
consola, mensaje y traceback), los reportes de salud y `describir_error`.

Es genérico por nombre de parámetro: cubre a todo PULL y OAuth2, presente y
futuro, sin tocar nada por proveedor.
"""
import logging
import re

# Parámetros cuyo valor nunca debe verse. Los más largos van primero para que
# la alternativa no corte un nombre a la mitad.
CLAVES_SECRETAS = (
    "access_token", "refresh_token", "id_token", "client_secret", "bearer_token",
    "webhook_auth_secret", "auth_pass", "x-api-key", "api_key", "apikey",
    "password", "passwd", "pwd", "signature", "secret", "token",
)

_PATRONES = [
    # access_token=XXXX · "password": "XXXX" · 'signature': 'XXXX'
    re.compile(
        r"(?i)\b(" + "|".join(re.escape(c) for c in CLAVES_SECRETAS) + r")"
        r"([\"']?\s*[=:]\s*[\"']?)([^\s&\"',}\]]+)"
    ),
    # Authorization: Bearer XXXX / Basic XXXX
    re.compile(r"(?i)\b(bearer|basic)(\s+)([A-Za-z0-9\-\._~\+/=]+)"),
]

MINIMO_PARA_MOSTRAR_PUNTAS = 8


def enmascarar_valor(valor) -> str:
    """'A1••••02': los 2 primeros y los 2 últimos. Corto: '••••'."""
    texto = str(valor)
    if len(texto) < MINIMO_PARA_MOSTRAR_PUNTAS:
        return "••••"
    return f"{texto[:2]}••••{texto[-2:]}"


def enmascarar(texto):
    """El texto con todo valor secreto enmascarado. No falla nunca."""
    if not texto or not isinstance(texto, str):
        return texto

    def _reemplazo(m):
        nombre, separador, valor = m.group(1), m.group(2), m.group(3)
        if "••••" in valor:
            return m.group(0)   # ya enmascarado
        return f"{nombre}{separador}{enmascarar_valor(valor)}"

    for patron in _PATRONES:
        texto = patron.sub(_reemplazo, texto)
    return texto


class FiltroSecretos(logging.Filter):
    """
    Filtro para los handlers de log: enmascara el mensaje y el traceback antes
    de escribirlos. El registro es el mismo objeto para todos los handlers,
    así que queda enmascarado para todos.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            mensaje = record.getMessage()
            enmascarado = enmascarar(mensaje)
            if enmascarado != mensaje:
                record.msg, record.args = enmascarado, ()
            if record.exc_info:
                # Los formateadores (el estándar y el JSON) usan exc_text
                # cuando exc_info ya no está.
                record.exc_text = enmascarar(logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = enmascarar(record.exc_text)
        except Exception:
            pass   # un log nunca puede romperse por enmascarar
        return True


# ─── Credenciales del sincronizador de diccionario (2.4) ─────────────────────

# Campos de enrichment_config que son credenciales. Se guardan cifrados en
# el mismo JSON, como fetch_config_enc guarda las del PULL de telemetría.
SECRETOS_DICCIONARIO = ("auth_pass", "bearer_token")


def cifrar_diccionario(enrich, anterior=None, estricto: bool = True) -> dict:
    """
    enrichment_config listo para guardar: sus credenciales cifradas.

    Un campo secreto vacío conserva el que ya estaba (`anterior`): el panel ya
    no recibe la contraseña guardada, así que guardar sin tocarla no la borra
    (igual que la contraseña de RC). `estricto` = no guardar en claro en
    producción sin MASTER_ENC_KEY (B-16).
    """
    from app.core.crypto import cifrar_credencial, encrypt, is_encrypted

    salida = dict(enrich or {})
    previo = anterior if isinstance(anterior, dict) else {}
    for clave in SECRETOS_DICCIONARIO:
        valor = salida.get(clave)
        if not valor:
            if previo.get(clave):
                salida[clave] = previo[clave]
            else:
                salida.pop(clave, None)
            continue
        if is_encrypted(valor):
            continue
        salida[clave] = cifrar_credencial(valor) if estricto else encrypt(valor)
    return salida


def descifrar_diccionario(enrich) -> dict:
    """enrichment_config con sus credenciales legibles, para usarlas."""
    from app.core.crypto import decrypt, is_encrypted

    salida = dict(enrich or {})
    for clave in SECRETOS_DICCIONARIO:
        valor = salida.get(clave)
        if valor and is_encrypted(valor):
            salida[clave] = decrypt(valor) or ""
    return salida


def diccionario_para_el_panel(enrich) -> dict:
    """enrichment_config sin sus credenciales: solo si hay una cargada."""
    salida = dict(enrich or {})
    for clave in SECRETOS_DICCIONARIO:
        salida[f"tiene_{clave}"] = bool(salida.pop(clave, None))
    return salida
