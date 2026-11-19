"""
Verificación en dos pasos del panel (v1.9.12): TOTP según RFC 6238.

Implementación propia con la biblioteca estándar (hmac, hashlib, struct,
base64, secrets): HMAC-SHA1, 6 dígitos, paso de 30 s, secreto de 20 bytes en
Base32. Es lo que entienden Google Authenticator, Microsoft Authenticator,
Authy, 1Password, Bitwarden y el resto.

Apagada (el valor por defecto) no cambia nada: `exigir()` lee el estado (con
caché de 5 s) y vuelve sin hacer nada.

Encendida, una petición al panel necesita, además de la contraseña (HTTP
Basic, como siempre), la cookie `hub_sesion` que se obtiene escribiendo un
código vigente en la página de verificación. La cookie es
`generación.vencimiento.firma`, firmada con HMAC-SHA256 con una llave propia
(no MASTER_ENC_KEY) que se guarda cifrada. «Cerrar todas las sesiones» sube la
generación: toda cookie anterior deja de valer.

Lo que NUNCA va al log: el secreto, los códigos (de la aplicación o de
recuperación), la llave de firma ni el contenido de la cookie.
"""
import base64
import hashlib
import hmac
import io
import json
import logging
import secrets
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# ── Parámetros ───────────────────────────────────────────────────────────────
EMISOR = "Hub Assistcargo"
PASO_SEG = 30
DIGITOS = 6
VENTANA_PASOS = 1                 # tolerancia de ±1 paso (relojes desfasados)
BYTES_SECRETO = 20

NOMBRE_COOKIE = "hub_sesion"
DURACION_POR_DEFECTO_H = 12
DURACION_MIN_H = 1
DURACION_MAX_H = 168

CANTIDAD_CODIGOS = 10
# Sin caracteres que se confunden al copiarlos a mano: ni 0/O ni 1/I/L.
ALFABETO_RECUPERACION = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ITERACIONES_PBKDF2 = 200_000

MAX_FALLOS = 5
VENTANA_FALLOS_SEG = 600
BLOQUEO_SEG = 600

CACHE_SEG = 5.0
PENDIENTE_VIGENCIA_SEG = 900      # el secreto mostrado vence a los 15 minutos

MENSAJE_SIN_CLAVE = (
    "No se puede activar: MASTER_ENC_KEY no está definida en el .env del servidor, "
    "y el secreto de la verificación en dos pasos quedaría guardado SIN CIFRAR. "
    "Definí MASTER_ENC_KEY (la MISMA que cifró las credenciales actuales) y reiniciá el hub."
)

_reloj = time.time                # los tests lo reemplazan


class ErrorDosPasos(Exception):
    """Error con el mensaje que ve el operador y el código HTTP que corresponde."""

    def __init__(self, status: int, mensaje: str):
        super().__init__(mensaje)
        self.status = status
        self.mensaje = mensaje


class VerificacionRequerida(HTTPException):
    """
    Contraseña correcta pero sin sesión verificada. 403 SIN WWW-Authenticate:
    con 401 el navegador volvería a pedir usuario y contraseña, que ya son
    correctos. GET /dashboard muestra en su lugar la página de verificación.
    """

    def __init__(self):
        super().__init__(status_code=403, detail="verificacion_requerida")


# ═══════════════════════════════════════════════════════════════════════════
# TOTP (RFC 4226 / RFC 6238)
# ═══════════════════════════════════════════════════════════════════════════

def generar_secreto() -> str:
    """20 bytes al azar en Base32, sin relleno (32 caracteres)."""
    return base64.b32encode(secrets.token_bytes(BYTES_SECRETO)).decode("ascii").rstrip("=")


def clave_de_secreto(secreto_b32: str) -> bytes:
    limpio = secreto_b32.strip().replace(" ", "").upper()
    return base64.b32decode(limpio + "=" * (-len(limpio) % 8))


def hotp(clave: bytes, contador: int, digitos: int = DIGITOS) -> str:
    """RFC 4226: HMAC-SHA1 del contador (8 bytes big endian) y truncado dinámico."""
    mac = hmac.new(clave, struct.pack(">Q", contador), hashlib.sha1).digest()
    desplazamiento = mac[-1] & 0x0F
    numero = struct.unpack(">I", mac[desplazamiento:desplazamiento + 4])[0] & 0x7FFFFFFF
    return str(numero % (10 ** digitos)).zfill(digitos)


def paso_de(instante: float) -> int:
    return int(instante // PASO_SEG)


def totp(clave: bytes, instante: float, digitos: int = DIGITOS) -> str:
    """RFC 6238 con T0 = 0 y paso de 30 s. Los dígitos son parámetro por los
    vectores del apéndice B (8 dígitos); el panel usa 6."""
    return hotp(clave, paso_de(instante), digitos)


def paso_del_codigo(clave: bytes, codigo: str, instante: float, ultimo_paso: int | None) -> int | None:
    """
    El paso (dentro de ±VENTANA_PASOS) al que corresponde el código, o None.
    Un código de un paso igual o anterior al último aceptado se rechaza:
    no se puede usar dos veces el mismo código (anti-repetición).
    """
    actual = paso_de(instante)
    aceptado = None
    for paso in range(actual - VENTANA_PASOS, actual + VENTANA_PASOS + 1):
        if paso < 0:
            continue
        if hmac.compare_digest(hotp(clave, paso).encode(), codigo.encode()):
            if ultimo_paso is None or paso > ultimo_paso:
                aceptado = paso
    return aceptado


def uri_de_alta(secreto_b32: str, usuario: str) -> str:
    emisor = quote(EMISOR)
    return (f"otpauth://totp/{emisor}:{quote(usuario)}?secret={secreto_b32}"
            f"&issuer={emisor}&digits={DIGITOS}&period={PASO_SEG}")


def qr_svg(texto: str) -> str:
    """QR en SVG (segno), para escanear con la aplicación."""
    import segno
    salida = io.BytesIO()
    segno.make(texto, error="m").save(salida, kind="svg", scale=5, border=2,
                                      dark="#000000", light="#ffffff", xmldecl=False)
    return salida.getvalue().decode("utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# Códigos de recuperación
# ═══════════════════════════════════════════════════════════════════════════

def _nuevo_codigo() -> str:
    c = "".join(secrets.choice(ALFABETO_RECUPERACION) for _ in range(8))
    return f"{c[:4]}-{c[4:]}"


def normalizar_recuperacion(texto: str) -> str | None:
    """'abcd-efgh' / 'ABCD EFGH' → 'ABCDEFGH'; None si no tiene la forma."""
    limpio = (texto or "").strip().upper().replace("-", "").replace(" ", "")
    if len(limpio) == 8 and all(c in ALFABETO_RECUPERACION for c in limpio):
        return limpio
    return None


def normalizar_totp(texto: str) -> str | None:
    limpio = (texto or "").strip().replace(" ", "")
    return limpio if len(limpio) == DIGITOS and limpio.isdigit() else None


def _hash_codigo(normalizado: str, sal: bytes, iteraciones: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", normalizado.encode(), sal, iteraciones).hex()


# ═══════════════════════════════════════════════════════════════════════════
# Almacenamiento
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Estado:
    activo: bool
    generacion: int
    duracion_horas: int
    clave_firma: bytes | None


_INACTIVO = Estado(False, 1, DURACION_POR_DEFECTO_H, None)
_cache: dict = {"estado": None, "hasta": 0.0}
_cache_lock = threading.Lock()
_escritura = threading.Lock()     # serializa verificaciones y cambios (anti-repetición)


def _sesion():
    """Sesión propia sobre el motor de la base de configuración. Se usa el
    motor y no database.get_session para que el estado no dependa de lo que
    se haga con esa función (los tests la reemplazan)."""
    from sqlalchemy.orm import Session
    from app import database
    return Session(bind=database.get_engine("system_config", "global"), autoflush=False)


def _modelos():
    from app.models.config_models import SeguridadDosPasos, CodigoRecuperacion
    return SeguridadDosPasos, CodigoRecuperacion


def _ahora_iso() -> str:
    return datetime.fromtimestamp(_reloj(), tz=timezone.utc).isoformat(timespec="seconds")


def _descifrar(valor: str | None) -> str | None:
    if not valor:
        return None
    from app.core.crypto import decrypt
    return decrypt(valor)


def _cifrar(texto: str) -> str:
    """Cifra con MASTER_ENC_KEY y se niega a devolver texto plano."""
    from app.core.crypto import encrypt, is_encrypted
    cifrado = encrypt(texto)
    if not is_encrypted(cifrado):
        raise ErrorDosPasos(409, MENSAJE_SIN_CLAVE)
    return cifrado


def invalidar():
    with _cache_lock:
        _cache["estado"] = None
        _cache["hasta"] = 0.0


def _leer_estado() -> Estado:
    Seg, _ = _modelos()
    with _sesion() as s:
        fila = s.get(Seg, 1)
        if fila is None or not fila.activo:
            return Estado(False,
                          fila.generacion_sesion if fila else 1,
                          fila.duracion_sesion_horas if fila else DURACION_POR_DEFECTO_H,
                          None)
        clave = None
        texto = _descifrar(fila.clave_firma_enc)
        if texto:
            try:
                clave = base64.b64decode(texto)
            except ValueError:
                clave = None
        if clave is None:
            logger.error("Verificación en dos pasos activa, pero la llave de las sesiones no se "
                         "pudo descifrar (¿cambió MASTER_ENC_KEY?). Nadie puede abrir sesión: "
                         "restaurá la llave o usá python -m app.tools.desactivar_dos_pasos.")
        return Estado(True, fila.generacion_sesion, fila.duracion_sesion_horas, clave)


def estado() -> Estado:
    """Estado vigente, con caché de CACHE_SEG segundos (se invalida en cada cambio)."""
    ahora = time.monotonic()
    with _cache_lock:
        if _cache["estado"] is not None and ahora < _cache["hasta"]:
            return _cache["estado"]
        previo = _cache["estado"]
    try:
        nuevo = _leer_estado()
    except Exception as e:
        if previo is not None:
            logger.error(f"No se pudo leer el estado de la verificación en dos pasos ({type(e).__name__}); "
                         "se usa el último conocido.")
            nuevo = previo
        else:
            logger.error(f"No se pudo leer el estado de la verificación en dos pasos: {type(e).__name__}")
            raise HTTPException(status_code=503,
                                detail="No se pudo leer el estado de la verificación en dos pasos.")
    with _cache_lock:
        _cache["estado"] = nuevo
        _cache["hasta"] = ahora + CACHE_SEG
    return nuevo


def puede_activar() -> tuple[bool, str | None]:
    """Sin MASTER_ENC_KEY (o con una inválida) no se activa: el secreto quedaría en claro."""
    from app.core.crypto import get_master_key
    try:
        clave = get_master_key()
    except RuntimeError:
        return False, ("No se puede activar: MASTER_ENC_KEY tiene un formato inválido. "
                       "Corregila en el .env del servidor y reiniciá el hub.")
    if not clave:
        return False, MENSAJE_SIN_CLAVE
    return True, None


def _fila(s):
    Seg, _ = _modelos()
    fila = s.get(Seg, 1)
    if fila is None:
        fila = Seg(id=1, activo=False, generacion_sesion=1,
                   duracion_sesion_horas=DURACION_POR_DEFECTO_H)
        s.add(fila)
        s.flush()
    return fila


# ── Intentos fallidos y bloqueo ─────────────────────────────────────────────

def _fallos(fila, ahora: float) -> list[float]:
    try:
        lista = json.loads(fila.fallos_json or "[]")
    except ValueError:
        lista = []
    return [t for t in lista if isinstance(t, (int, float)) and ahora - t < VENTANA_FALLOS_SEG]


def _segundos_bloqueado(fila, ahora: float) -> int:
    if fila.bloqueado_hasta and fila.bloqueado_hasta > ahora:
        return int(fila.bloqueado_hasta - ahora) + 1
    return 0


def _comprobar_bloqueo(fila, ahora: float):
    restante = _segundos_bloqueado(fila, ahora)
    if restante:
        logger.warning(f"Intento de verificación durante el bloqueo (faltan {restante} s).")
        raise ErrorDosPasos(429, f"Demasiados códigos incorrectos. La verificación está bloqueada; "
                                 f"probá de nuevo en {max(1, restante // 60)} minuto(s).")


def _registrar_fallo(s, fila, ahora: float, que: str):
    lista = _fallos(fila, ahora) + [ahora]
    fila.fallos_json = json.dumps(lista)
    if len(lista) >= MAX_FALLOS:
        fila.bloqueado_hasta = ahora + BLOQUEO_SEG
        fila.fallos_json = "[]"
        s.commit()
        logger.critical(
            f"Verificación en dos pasos BLOQUEADA por {BLOQUEO_SEG // 60} minutos: "
            f"{MAX_FALLOS} códigos incorrectos en {VENTANA_FALLOS_SEG // 60} minutos.")
        raise ErrorDosPasos(429, "Demasiados códigos incorrectos. La verificación quedó bloqueada "
                                 f"{BLOQUEO_SEG // 60} minutos.")
    s.commit()
    logger.warning(f"Código de verificación incorrecto ({que}): {len(lista)} de {MAX_FALLOS} "
                   f"en {VENTANA_FALLOS_SEG // 60} minutos.")
    raise ErrorDosPasos(400, "El código no es correcto.")


def _limpiar_fallos(fila):
    fila.fallos_json = "[]"
    fila.bloqueado_hasta = None


# ── Códigos de recuperación ─────────────────────────────────────────────────

def _crear_codigos(s) -> list[str]:
    _, Cod = _modelos()
    s.query(Cod).delete()
    codigos = [_nuevo_codigo() for _ in range(CANTIDAD_CODIGOS)]
    creado = _ahora_iso()
    for c in codigos:
        sal = secrets.token_bytes(16)
        s.add(Cod(sal=sal.hex(), hash_pbkdf2=_hash_codigo(normalizar_recuperacion(c), sal, ITERACIONES_PBKDF2),
                  iteraciones=ITERACIONES_PBKDF2, usado=False, creado_en=creado))
    return codigos


def _consumir_recuperacion(s, normalizado: str) -> bool:
    _, Cod = _modelos()
    encontrado = None
    for cod in s.query(Cod).filter(Cod.usado.is_(False)).all():
        calculado = _hash_codigo(normalizado, bytes.fromhex(cod.sal), cod.iteraciones)
        if hmac.compare_digest(calculado, cod.hash_pbkdf2) and encontrado is None:
            encontrado = cod
    if encontrado is None:
        return False
    encontrado.usado = True
    encontrado.usado_en = _ahora_iso()
    s.flush()
    quedan = s.query(Cod).filter(Cod.usado.is_(False)).count()
    logger.warning(f"Verificación en dos pasos: se usó un código de recuperación, quedan {quedan}.")
    return True


def _codigos_restantes(s) -> int:
    _, Cod = _modelos()
    return s.query(Cod).filter(Cod.usado.is_(False)).count()


# ═══════════════════════════════════════════════════════════════════════════
# Operaciones
# ═══════════════════════════════════════════════════════════════════════════

def resumen() -> dict:
    """Lo que muestra la sección Seguridad del panel."""
    puede, motivo = puede_activar()
    ahora = _reloj()
    Seg, _ = _modelos()
    with _sesion() as s:
        fila = s.get(Seg, 1)
        activo = bool(fila and fila.activo)
        return {
            "activo": activo,
            "activado_en": fila.activado_en if activo else None,
            "codigos_restantes": _codigos_restantes(s) if activo else 0,
            "duracion_horas": fila.duracion_sesion_horas if fila else DURACION_POR_DEFECTO_H,
            "duracion_min": DURACION_MIN_H,
            "duracion_max": DURACION_MAX_H,
            "puede_activar": puede,
            "motivo_no_puede": motivo,
            "bloqueado_seg": _segundos_bloqueado(fila, ahora) if fila else 0,
        }


def iniciar(usuario: str) -> dict:
    """Genera el secreto PENDIENTE. No protege nada hasta que se confirma."""
    puede, motivo = puede_activar()
    if not puede:
        raise ErrorDosPasos(409, motivo)
    secreto = generar_secreto()
    with _escritura, _sesion() as s:
        fila = _fila(s)
        if fila.activo:
            raise ErrorDosPasos(409, "La verificación en dos pasos ya está activa.")
        fila.secreto_pendiente_enc = _cifrar(secreto)
        fila.pendiente_creado = _reloj()
        fila.actualizado_en = _ahora_iso()
        s.commit()
    logger.info("Verificación en dos pasos: se generó un secreto pendiente de confirmar.")
    uri = uri_de_alta(secreto, usuario)
    return {"secreto": secreto, "uri": uri, "qr_svg": qr_svg(uri),
            "vence_en_seg": PENDIENTE_VIGENCIA_SEG}


def confirmar(codigo: str) -> list[str]:
    """Un código vigente del secreto pendiente activa la verificación. Devuelve
    los códigos de recuperación (la única vez que se ven en claro)."""
    ahora = _reloj()
    with _escritura, _sesion() as s:
        fila = _fila(s)
        if fila.activo:
            raise ErrorDosPasos(409, "La verificación en dos pasos ya está activa.")
        _comprobar_bloqueo(fila, ahora)
        if not fila.secreto_pendiente_enc or not fila.pendiente_creado \
                or ahora - fila.pendiente_creado > PENDIENTE_VIGENCIA_SEG:
            raise ErrorDosPasos(409, "No hay un alta en curso o venció: empezá de nuevo con «Activar».")
        secreto = _descifrar(fila.secreto_pendiente_enc)
        if not secreto:
            raise ErrorDosPasos(409, "No se pudo leer el secreto pendiente: empezá de nuevo.")
        normal = normalizar_totp(codigo)
        paso = paso_del_codigo(clave_de_secreto(secreto), normal, ahora, None) if normal else None
        if paso is None:
            _registrar_fallo(s, fila, ahora, "alta")
        fila.secreto_enc = fila.secreto_pendiente_enc
        fila.secreto_pendiente_enc = None
        fila.pendiente_creado = None
        fila.activo = True
        fila.activado_en = _ahora_iso()
        fila.ultimo_paso = paso
        fila.generacion_sesion = (fila.generacion_sesion or 1) + 1
        fila.clave_firma_enc = _cifrar(base64.b64encode(secrets.token_bytes(32)).decode("ascii"))
        _limpiar_fallos(fila)
        fila.actualizado_en = fila.activado_en
        codigos = _crear_codigos(s)
        s.commit()
    invalidar()
    logger.warning("Verificación en dos pasos ACTIVADA para el panel.")
    return codigos


def verificar_codigo(codigo: str, admitir_recuperacion: bool = True) -> str:
    """
    Comprueba un código de la aplicación (o, si se admite, uno de
    recuperación, que se consume). Devuelve 'totp' o 'recuperacion'; si no
    vale, registra el fallo y lanza ErrorDosPasos (400, o 429 bloqueado).
    """
    ahora = _reloj()
    with _escritura, _sesion() as s:
        fila = _fila(s)
        if not fila.activo:
            raise ErrorDosPasos(409, "La verificación en dos pasos no está activa.")
        _comprobar_bloqueo(fila, ahora)
        normal = normalizar_totp(codigo)
        if normal:
            secreto = _descifrar(fila.secreto_enc)
            if not secreto:
                logger.error("Verificación en dos pasos: el secreto no se pudo descifrar "
                             "(¿cambió MASTER_ENC_KEY?).")
                raise ErrorDosPasos(503, "No se pudo leer el secreto guardado. Revisá MASTER_ENC_KEY.")
            paso = paso_del_codigo(clave_de_secreto(secreto), normal, ahora, fila.ultimo_paso)
            if paso is not None:
                fila.ultimo_paso = paso
                _limpiar_fallos(fila)
                s.commit()
                return "totp"
        elif admitir_recuperacion:
            recup = normalizar_recuperacion(codigo)
            if recup and _consumir_recuperacion(s, recup):
                _limpiar_fallos(fila)
                s.commit()
                return "recuperacion"
        _registrar_fallo(s, fila, ahora, "verificación")
    raise AssertionError("inalcanzable")


def desactivar(motivo: str) -> None:
    """Apaga la verificación y borra secretos y códigos. Cierra todas las sesiones."""
    _, Cod = _modelos()
    with _escritura, _sesion() as s:
        fila = _fila(s)
        fila.activo = False
        fila.secreto_enc = None
        fila.secreto_pendiente_enc = None
        fila.pendiente_creado = None
        fila.ultimo_paso = None
        fila.clave_firma_enc = None
        fila.activado_en = None
        fila.generacion_sesion = (fila.generacion_sesion or 1) + 1
        _limpiar_fallos(fila)
        fila.actualizado_en = _ahora_iso()
        s.query(Cod).delete()
        s.commit()
    invalidar()
    logger.warning(f"Verificación en dos pasos DESACTIVADA ({motivo}).")


def fijar_duracion(horas: int) -> int:
    if not isinstance(horas, int) or not (DURACION_MIN_H <= horas <= DURACION_MAX_H):
        raise ErrorDosPasos(400, f"La duración va de {DURACION_MIN_H} a {DURACION_MAX_H} horas.")
    with _escritura, _sesion() as s:
        fila = _fila(s)
        fila.duracion_sesion_horas = horas
        fila.actualizado_en = _ahora_iso()
        s.commit()
    invalidar()
    logger.info(f"Verificación en dos pasos: la sesión dura ahora {horas} h (las nuevas).")
    return horas


def cerrar_todas() -> None:
    with _escritura, _sesion() as s:
        fila = _fila(s)
        fila.generacion_sesion = (fila.generacion_sesion or 1) + 1
        fila.actualizado_en = _ahora_iso()
        s.commit()
    invalidar()
    logger.warning("Verificación en dos pasos: se cerraron todas las sesiones del panel.")


def regenerar_codigos(codigo_totp: str) -> list[str]:
    """Pide un código vigente de la aplicación (no de recuperación)."""
    verificar_codigo(codigo_totp, admitir_recuperacion=False)
    with _escritura, _sesion() as s:
        codigos = _crear_codigos(s)
        s.commit()
    logger.warning("Verificación en dos pasos: se generaron códigos de recuperación nuevos; "
                   "los anteriores ya no valen.")
    return codigos


# ═══════════════════════════════════════════════════════════════════════════
# Cookie de sesión
# ═══════════════════════════════════════════════════════════════════════════

def _firma(clave: bytes, cuerpo: str) -> str:
    return hmac.new(clave, cuerpo.encode("ascii"), hashlib.sha256).hexdigest()


def emitir_cookie(est: Estado | None = None) -> tuple[str, int]:
    """(valor, Max-Age) de una cookie nueva para el estado vigente."""
    est = est or estado()
    if not est.activo or not est.clave_firma:
        raise ErrorDosPasos(503, "No se puede abrir la sesión: falta la llave de las sesiones.")
    max_age = est.duracion_horas * 3600
    cuerpo = f"{est.generacion}.{int(_reloj()) + max_age}"
    return f"{cuerpo}.{_firma(est.clave_firma, cuerpo)}", max_age


def sesion_valida(valor: str | None, est: Estado) -> bool:
    """Firma (compare_digest), vencimiento y generación. Cualquier falla = sin sesión."""
    if not valor or not est.clave_firma:
        return False
    partes = valor.split(".")
    if len(partes) != 3:
        return False
    gen, venc, firma = partes
    if not (gen.isdigit() and venc.isdigit()):
        return False
    if not hmac.compare_digest(_firma(est.clave_firma, f"{gen}.{venc}"), firma):
        return False
    return int(venc) > _reloj() and int(gen) == est.generacion


def poner_cookie(response, request, valor: str, max_age: int):
    from app.core.seguridad_http import es_https
    response.set_cookie(NOMBRE_COOKIE, valor, max_age=max_age, path="/",
                        httponly=True, samesite="strict", secure=es_https(request))


def borrar_cookie(response, request):
    from app.core.seguridad_http import es_https
    response.delete_cookie(NOMBRE_COOKIE, path="/", httponly=True, samesite="strict",
                           secure=es_https(request))


# ═══════════════════════════════════════════════════════════════════════════
# Exigencia en cada petición autenticada
# ═══════════════════════════════════════════════════════════════════════════

_RUTAS_LIBRES = frozenset({"/Json/Data", "/health"})
_PREFIJOS_LIBRES = ("/webhook/", "/static/", "/auth/")


def ruta_sin_dos_pasos(request) -> bool:
    """
    Rutas que nunca piden la cookie: ingesta, salud, estáticos, la
    verificación misma (/auth/verificar, /auth/salir) y la RECEPCIÓN del
    inspector (POST /inspector/catch/{id}), que la usa un emisor externo. La
    consulta de lo capturado (GET …/latest) es del panel y sí la pide.
    """
    ruta = request.url.path
    if ruta in _RUTAS_LIBRES or ruta.startswith(_PREFIJOS_LIBRES):
        return True
    return request.method == "POST" and ruta.startswith("/inspector/catch/")


def exigir(request) -> None:
    """Con la verificación apagada no hace nada. Encendida, exige la cookie."""
    if ruta_sin_dos_pasos(request):
        return
    est = estado()
    if not est.activo:
        return
    if sesion_valida(request.cookies.get(NOMBRE_COOKIE), est):
        return
    raise VerificacionRequerida()
