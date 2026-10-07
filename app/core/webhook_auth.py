"""
Autenticación de webhooks entrantes, transversal a cualquier proveedor.

DOS MODOS

  header  Un secreto fijo viaja en un header y se compara con el guardado.
          Es el modo histórico del webhook dinámico y sigue siendo el valor por
          defecto: una integración sin configuración de autenticación explícita
          se comporta exactamente igual que antes.

  hmac    El proveedor FIRMA cada petición: calcula un HMAC sobre el cuerpo
          (y a veces un timestamp) con una clave compartida, y manda el
          resultado en un header. El secreto nunca viaja; lo que viaja cambia
          en cada envío. Por eso no se puede comparar como un header fijo.

          Es el esquema de Tive, y el de muchos otros (Stripe, GitHub,
          Shopify, etc.), cada uno con su variante. En lugar de programar un
          caso por proveedor, el esquema se describe con cinco parámetros:

            header        dónde viene la firma
            patron        regex que extrae el timestamp (grupo `ts`, opcional)
                          y la firma (grupo `firma`)
            contenido     qué se firmó: "{ts}.{body}", "{body}", etc.
            codificacion  cómo viene la firma: "base64" o "hex"
            tolerancia    cuántos segundos de antigüedad se aceptan
            formato_ts    cómo leer el timestamp, para medir su antigüedad

          Hay presets para los esquemas que se verificaron contra la
          documentación del proveedor. Se pueden sobrescribir campo por campo.

POR QUÉ EL CUERPO TIENE QUE SER EL CRUDO

La firma se calcula sobre los bytes exactos que mandó el proveedor. Si se
parsea el JSON y se vuelve a serializar, cambia el orden de las claves, los
espacios o el escape de caracteres, y la firma deja de coincidir aunque el
contenido sea el mismo. Se verifica sobre lo recibido, antes de parsear.
"""
import base64
import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timezone

MODO_HEADER = "header"
MODO_HMAC = "hmac"

# Esquemas verificados contra la documentación oficial de cada proveedor.
# Agregar uno exige leer su especificación: no se deducen.
PRESETS_HMAC = {
    # https://developers.tive.com/docs/webhook-signatures
    # Formato:   x-tive-signature: t=2022-10-31 20:56:28Z,v1=<base64>
    # Firmado:   "<timestamp>.<cuerpo crudo>"
    # OJO: el timestamp va con ESPACIO entre fecha y hora, no con "T".
    "tive": {
        "header": "x-tive-signature",
        "patron": r"^t=(?P<ts>[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}Z),v1=(?P<firma>\S+)$",
        "contenido": "{ts}.{body}",
        "codificacion": "base64",
        "tolerancia_seg": 300,
        "formato_ts": "%Y-%m-%d %H:%M:%SZ",
    },
}

CAMPOS_HMAC = ("header", "patron", "contenido", "codificacion",
               "tolerancia_seg", "formato_ts")


class FirmaInvalida(Exception):
    """
    La petición no pasó la verificación.

    Lleva el motivo legible, porque un rechazo sin motivo registrado es
    indistinguible de que no haya llegado nada.
    """


def resolver_config(config: dict | None) -> dict:
    """
    Configuración efectiva de autenticación de una integración.

    Sin configuración, modo `header`: el comportamiento histórico. Con un
    preset, se parte de sus valores y se sobrescriben los que se indiquen.
    """
    config = dict(config or {})
    modo = (config.get("modo") or MODO_HEADER).strip().lower()

    if modo != MODO_HMAC:
        return {"modo": MODO_HEADER}

    base = {}
    preset = (config.get("preset") or "").strip().lower()
    if preset:
        if preset not in PRESETS_HMAC:
            raise ValueError(
                f"Preset de firma desconocido: '{preset}'. "
                f"Disponibles: {', '.join(sorted(PRESETS_HMAC))}."
            )
        base = dict(PRESETS_HMAC[preset])

    for campo in CAMPOS_HMAC:
        if config.get(campo) not in (None, ""):
            base[campo] = config[campo]

    faltantes = [c for c in ("header", "patron", "contenido", "codificacion") if not base.get(c)]
    if faltantes:
        raise ValueError(
            f"Configuración de firma incompleta, faltan: {', '.join(faltantes)}. "
            f"Usá un preset o completá los campos."
        )
    if "{body}" not in base["contenido"]:
        raise ValueError("El contenido firmado tiene que incluir {body}.")
    if base["codificacion"] not in ("base64", "hex"):
        raise ValueError("La codificación de la firma tiene que ser base64 o hex.")

    try:
        re.compile(base["patron"])
    except re.error as e:
        raise ValueError(f"El patrón de la firma no es una expresión regular válida: {e}")

    base["modo"] = MODO_HMAC
    return base


def verificar_header(valor_recibido: str, secreto: str, nombre_header: str):
    """Modo `header`: comparación en tiempo constante contra el secreto."""
    if not valor_recibido:
        raise FirmaInvalida(f"falta el header {nombre_header}")
    if not secrets.compare_digest(valor_recibido, secreto):
        raise FirmaInvalida("API key incorrecta")


def _edad_segundos(ts: str, formato: str) -> float:
    if formato == "epoch":
        momento = float(ts)
        if momento > 1e12:                 # vino en milisegundos
            momento /= 1000
        return time.time() - momento
    fecha = datetime.strptime(ts, formato).replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - fecha).total_seconds()


def verificar_hmac(cuerpo_crudo: bytes, headers, secreto: str, config: dict):
    """
    Modo `hmac`: verifica la firma sobre los bytes exactos recibidos.

    Lanza FirmaInvalida con el motivo si algo no cuadra. No devuelve nada si
    la firma es válida.
    """
    nombre = config["header"]
    valor = headers.get(nombre) or ""
    if not valor:
        raise FirmaInvalida(
            f"falta el header {nombre}. Si el proveedor firma solo cuando tiene "
            f"una clave secreta cargada, verificar que la tenga."
        )

    coincidencia = re.match(config["patron"], valor.strip())
    if not coincidencia:
        raise FirmaInvalida(f"el header {nombre} no tiene el formato esperado")

    grupos = coincidencia.groupdict()
    firma_recibida = grupos.get("firma")
    if not firma_recibida:
        raise FirmaInvalida("el patrón no extrajo la firma (falta el grupo 'firma')")
    ts = grupos.get("ts")

    # La antigüedad se controla ANTES del HMAC: es lo que impide reenviar una
    # petición legítima capturada. Una firma válida pero vieja se rechaza.
    tolerancia = config.get("tolerancia_seg")
    if ts and tolerancia:
        try:
            edad = _edad_segundos(ts, config.get("formato_ts") or "epoch")
        except (ValueError, TypeError):
            raise FirmaInvalida(f"no se pudo leer el timestamp de la firma: {ts!r}")
        if abs(edad) > float(tolerancia):
            raise FirmaInvalida(
                f"firma fuera de la ventana de tolerancia "
                f"({edad:.0f}s de diferencia, máximo {tolerancia}s)"
            )

    try:
        cuerpo_txt = cuerpo_crudo.decode("utf-8")
    except UnicodeDecodeError:
        raise FirmaInvalida("el cuerpo no es UTF-8 válido")

    contenido = config["contenido"].replace("{ts}", ts or "").replace("{body}", cuerpo_txt)
    digest = hmac.new(secreto.encode("utf-8"), contenido.encode("utf-8"), hashlib.sha256).digest()

    if config["codificacion"] == "base64":
        esperada = base64.b64encode(digest).decode("ascii")
    else:
        esperada = digest.hex()

    if not secrets.compare_digest(firma_recibida, esperada):
        raise FirmaInvalida("la firma no coincide con el contenido recibido")
