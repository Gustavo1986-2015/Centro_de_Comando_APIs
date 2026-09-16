"""
Desglose de latencia: en qué tramo se va el tiempo de una petición PUSH.

POR QUÉ EXISTE

Una certificación de alto caudal puede fallar por milisegundos sin que se
sepa en qué tramo se van. Optimizar sin ese dato es adivinar.

El dato que lo destrabó fue una comparación, no una medición: TripData se
descarta antes de leer el cuerpo del request —no parsea, no encola, no toca
disco— y aun así promedió 288 ms. O sea que ~288 ms eran red, TLS y nginx, y
solo ~39 ms era trabajo del hub.

Esta instrumentación mide esos 39 ms por dentro, para saber si están en el
parseo, en la validación o en el encolado, en vez de suponerlo.

QUÉ MIDE Y QUÉ NO

Mide desde que FastAPI entrega la petición al handler hasta que el handler
devuelve. NO incluye:

  · La red entre el proveedor y el servidor
  · La terminación TLS
  · nginx
  · El tiempo de ASGI antes del handler

Esos tramos no son observables desde adentro del proceso. Para estimarlos está
la comparación con el total que reporta el proveedor: lo que el proveedor mide
menos lo que se mide acá es, por descarte, infraestructura.

Por eso el panel muestra las dos cosas: el desglose interno y el recordatorio
de que el resto es red.
"""
import threading
import time
from collections import deque

# Igual criterio que el store de latencias push: tope duro de muestras.
# Un deque sin límite acumulando 24 horas a 40 msg/s llega a millones de
# entradas, y cada consulta copia la lista entera. Eso fue exactamente lo que
# degradó la latencia durante una corrida sostenida.
MAX_MUESTRAS = 5_000

# Tramos del camino de recepción, en orden. El nombre es el que ve el operador.
# Cada tramo con su nombre y con la explicación que ve el operador.
#
# La explicación importa tanto como el número: si un proveedor pide datos, hay
# que poder decir qué mide cada fila sin abrir el código.
TRAMOS = (
    ("auth", "1. Autenticación",
     "Verificar la API key del proveedor contra la configurada. Usa una caché, "
     "así que normalmente es instantáneo."),
    ("rate_limit", "2. Control de caudal",
     "Comprobar que el proveedor no supere el límite de peticiones por minuto "
     "que tiene configurado."),
    ("parseo", "3. Lectura del mensaje",
     "Leer el cuerpo de la petición y convertirlo a datos. Es el tramo que más "
     "crece con el caudal, porque depende del tamaño del mensaje y de cuán "
     "ocupado esté el proceso."),
    ("encolado", "4. Puesta en cola",
     "Dejar el mensaje en la cola interna para que el worker lo procese. Acá "
     "termina la petición: el proveedor recibe su respuesta en este punto."),
    ("respaldo", "5. Respaldo de contingencia",
     "Solo ocurre si la cola está llena o se superó el caudal: el mensaje se "
     "guarda en disco para no perderlo. En operación normal no aparece."),
    ("total_handler", "TOTAL — trabajo del hub",
     "Suma de los tramos anteriores. Es el tiempo que el hub tarda en aceptar "
     "un mensaje. Lo que el proveedor mida por encima de este valor corresponde "
     "a la red, el TLS y el proxy, que no son medibles desde acá."),
)

_muestras: dict[str, deque] = {}

# Acumuladores de TODA la corrida, en memoria fija.
#
# El deque acotado cubre 10.000 muestras: a 40 msg/s son apenas 4 minutos. Sirve
# para los percentiles —que necesitan las muestras— pero no para responder
# "¿cuál fue el promedio de las 24 horas del test?".
#
# Estos cuatro números se actualizan en tiempo constante y cubren la corrida
# entera sin importar cuánto dure. El promedio sale de dividir suma por
# cantidad. No se pierde nada y no cuesta nada.
_totales: dict[str, dict] = {}

# Acumuladores corridos: suma, cuenta, peor y mejor por tramo.
#
# No hace falta guardar las muestras para promediar. Si solo se acotara el
# deque, el "promedio" sería el de los últimos 10.000 eventos — a 40 msg/s,
# cuatro minutos — y no habría forma de compararlo contra el número que el
# proveedor informa al cabo de 24 horas.
#
# Con suma y cuenta el promedio es EXACTO desde el arranque y ocupa memoria
# constante. El deque queda solo para los percentiles, que sí necesitan las
# muestras y para los que una ventana reciente es lo apropiado.
_acumulado: dict[str, dict[str, dict]] = {}

# Distribución del total por tramos de milisegundos, acumulada.
#
# Es la evidencia que pide una certificación: no "el promedio dio 327 ms" sino
# "el 94% estuvo bajo 250 ms y el 6% se fue a más de un segundo". Un promedio
# esconde la forma; los tramos la muestran.
#
# Se guarda como contadores, no como muestras: cubre toda la corrida con
# memoria fija, igual criterio que los acumuladores.
BUCKETS_MS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)

# Umbral de una certificación de alto caudal. El corte de 250 está entre los tramos
# a propósito: permite responder "qué porcentaje cumplió" sin recalcular nada.
SLA_MS = 250

# Porcentaje mínimo bajo el umbral antes de avisar.
#
# Existe porque de la certificación fallida te enteraste cuando lo dijo el
# proveedor. Un aviso en el panel apenas el cumplimiento cae te da horas en vez
# de días. Se necesita un mínimo de peticiones para no gritar por una muestra
# de tres.
UMBRAL_ALERTA_PCT = 95.0
MINIMO_PARA_ALERTAR = 500

_distribucion: dict[str, dict[str, int]] = {}

# Cortes por hora, para poder decir "entre las 10 y las 11 el promedio fue X".
#
# El acumulado desde el arranque es un número plano: no muestra si el problema
# apareció a media corrida ni si empeoró. En una corrida sostenida la latencia
# creció a lo largo del día y eso solo se ve con la evolución.
#
# Contadores por hora, en memoria fija: cantidad, suma, peor y cuántos bajo el
# umbral. Se conservan 48 horas, que cubre con margen una corrida de
# certificación y su día previo.
HORAS_RETENIDAS = 48

_por_hora: dict[str, dict[str, dict]] = {}


def _clave_hora(momento: float | None = None) -> str:
    t = time.gmtime(momento if momento is not None else time.time())
    return time.strftime("%Y-%m-%d %H:00", t)


def _etiqueta_bucket(ms: float) -> str:
    anterior = 0
    for limite in BUCKETS_MS:
        if ms < limite:
            return f"{anterior}-{limite} ms"
        anterior = limite
    return f"> {BUCKETS_MS[-1]} ms"

_LOCK = threading.Lock()


class Cronometro:
    """
    Mide tramos dentro de una petición.

    Uso:
        crono = Cronometro("schmitz", "prod")
        ...
        crono.marca("auth")
        ...
        crono.marca("parseo")
        crono.cerrar()

    Cada marca registra el tiempo transcurrido DESDE LA MARCA ANTERIOR, no
    desde el inicio: así los tramos suman el total y se ve cuál pesa.

    El costo de medir es un perf_counter por tramo, del orden de decenas de
    nanosegundos. No altera lo que mide.
    """

    __slots__ = ("provider", "env", "_inicio", "_ultimo", "_tramos")

    def __init__(self, provider: str, env: str):
        self.provider = provider.lower()
        self.env = env.lower()
        ahora = time.perf_counter()
        self._inicio = ahora
        self._ultimo = ahora
        self._tramos: dict[str, float] = {}

    def marca(self, tramo: str):
        ahora = time.perf_counter()
        self._tramos[tramo] = (ahora - self._ultimo) * 1000.0
        self._ultimo = ahora

    def cerrar(self):
        """Registra la muestra completa. Llamar siempre, incluso si hubo error."""
        self._tramos["total_handler"] = (time.perf_counter() - self._inicio) * 1000.0
        clave = f"{self.provider}:{self.env}"
        with _LOCK:
            if clave not in _muestras:
                _muestras[clave] = deque(maxlen=MAX_MUESTRAS)
                _acumulado[clave] = {}
                _distribucion[clave] = {}
                _por_hora[clave] = {}
            _muestras[clave].append(self._tramos)

            # La distribución se lleva sobre el total del handler, que es el
            # número comparable contra lo que mide el proveedor.
            etiqueta = _etiqueta_bucket(self._tramos["total_handler"])
            dist = _distribucion[clave]
            dist[etiqueta] = dist.get(etiqueta, 0) + 1

            # Corte horario del total, con poda de lo que ya no interesa.
            hora = _clave_hora()
            horas = _por_hora[clave]
            h = horas.get(hora)
            total_ms = self._tramos["total_handler"]
            if h is None:
                h = horas[hora] = {"cuenta": 0, "suma": 0.0, "peor": 0.0, "bajo_sla": 0}
                if len(horas) > HORAS_RETENIDAS:
                    for vieja in sorted(horas)[:-HORAS_RETENIDAS]:
                        del horas[vieja]
            h["cuenta"] += 1
            h["suma"] += total_ms
            if total_ms > h["peor"]:
                h["peor"] = total_ms
            if total_ms <= SLA_MS:
                h["bajo_sla"] += 1

            acum = _acumulado[clave]
            for tramo, ms in self._tramos.items():
                a = acum.get(tramo)
                if a is None:
                    acum[tramo] = {"suma": ms, "cuenta": 1, "peor": ms, "mejor": ms}
                else:
                    a["suma"] += ms
                    a["cuenta"] += 1
                    if ms > a["peor"]:
                        a["peor"] = ms
                    if ms < a["mejor"]:
                        a["mejor"] = ms


def _fmt_edad(seg: float) -> str:
    """Antigüedad en palabras, para que un pico viejo se lea como viejo."""
    seg = int(seg)
    if seg < 60:
        return f"{seg}s"
    if seg < 3600:
        return f"{seg // 60} min"
    if seg < 86400:
        return f"{seg // 3600} h"
    return f"{seg // 86400} d"


def _percentil(valores: list[float], p: float) -> float:
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    indice = min(int(len(ordenados) * p), len(ordenados) - 1)
    return ordenados[indice]


def desglose(provider: str | None = None, env: str | None = None) -> dict:
    """
    Promedio, mediana y p95 de cada tramo.

    Se devuelve p95 además del promedio porque un promedio bajo con un p95 alto
    señala picos ocasionales, que es un problema distinto de una lentitud
    pareja. En una corrida sostenida el promedio era 327 ms y el máximo 6,8 s:
    solo con el promedio esa diferencia no se ve.
    """
    with _LOCK:
        if provider and env:
            claves = [f"{provider.lower()}:{env.lower()}"]
        elif provider:
            claves = [k for k in _muestras if k.split(":")[0] == provider.lower()]
        else:
            claves = list(_muestras.keys())

        recolectadas: list[dict] = []
        acumulados: dict[str, dict] = {}
        dist_total: dict[str, int] = {}
        horas_total: dict[str, dict] = {}
        for k in claves:
            for etiqueta, n in _distribucion.get(k, {}).items():
                dist_total[etiqueta] = dist_total.get(etiqueta, 0) + n
            for hora, h in _por_hora.get(k, {}).items():
                acc_h = horas_total.setdefault(
                    hora, {"cuenta": 0, "suma": 0.0, "peor": 0.0, "bajo_sla": 0}
                )
                acc_h["cuenta"] += h["cuenta"]
                acc_h["suma"] += h["suma"]
                acc_h["peor"] = max(acc_h["peor"], h["peor"])
                acc_h["bajo_sla"] += h["bajo_sla"]
            recolectadas.extend(list(_muestras.get(k, [])))
            for tramo, a in _acumulado.get(k, {}).items():
                dest = acumulados.setdefault(
                    tramo, {"suma": 0.0, "cuenta": 0, "peor": 0.0, "mejor": None}
                )
                dest["suma"] += a["suma"]
                dest["cuenta"] += a["cuenta"]
                dest["peor"] = max(dest["peor"], a["peor"])
                dest["mejor"] = (a["mejor"] if dest["mejor"] is None
                                 else min(dest["mejor"], a["mejor"]))

    if not acumulados:
        return {"muestras": 0, "muestras_percentiles": 0, "tramos": [],
                "distribucion": [], "bajo_sla_pct": 0.0, "sla_ms": SLA_MS,
                "alerta_sla": False, "umbral_alerta_pct": UMBRAL_ALERTA_PCT,
                "por_hora": []}

    salida = []
    for clave, etiqueta, explicacion in TRAMOS:
        a = acumulados.get(clave)
        if not a or not a["cuenta"]:
            continue
        # Promedio, peor y mejor salen del acumulado: son de TODA la corrida.
        # Mediana y p95 salen de la ventana reciente, que es lo que se guarda.
        recientes = [m[clave] for m in recolectadas if clave in m]
        salida.append({
            "tramo": clave,
            "etiqueta": etiqueta,
            "explicacion": explicacion,
            "promedio_ms": round(a["suma"] / a["cuenta"], 3),
            "mediana_ms": round(_percentil(recientes, 0.5), 3),
            "p95_ms": round(_percentil(recientes, 0.95), 3),
            "peor_ms": round(a["peor"], 3),
            "mejor_ms": round(a["mejor"], 3),
            "muestras": a["cuenta"],
        })

    total = max((a["cuenta"] for a in acumulados.values()), default=0)
    # La distribución se ordena por el límite inferior del tramo, no
    # alfabéticamente: "1000-2500" no puede quedar antes que "5-10".
    def _orden(etiqueta: str) -> float:
        return float("inf") if etiqueta.startswith(">") else float(etiqueta.split("-")[0])

    total_dist = sum(dist_total.values()) or 1
    distribucion = [
        {"tramo": e, "peticiones": n, "porcentaje": round(n * 100 / total_dist, 2)}
        for e, n in sorted(dist_total.items(), key=lambda x: _orden(x[0]))
    ]

    # Porcentaje bajo el umbral del SLA: es el número que pide una
    # certificación, más que el promedio.
    bajo_sla = sum(
        n for e, n in dist_total.items()
        if not e.startswith(">") and float(e.split("-")[1].split()[0]) <= SLA_MS
    )

    return {
        "muestras": total,
        "muestras_percentiles": len(recolectadas),
        "tramos": salida,
        "distribucion": distribucion,
        "bajo_sla_pct": round(bajo_sla * 100 / total_dist, 2),
        "sla_ms": SLA_MS,
        "alerta_sla": (
            total_dist >= MINIMO_PARA_ALERTAR
            and (bajo_sla * 100 / total_dist) < UMBRAL_ALERTA_PCT
        ),
        "umbral_alerta_pct": UMBRAL_ALERTA_PCT,
        "por_hora": [
            {
                "hora": hora,
                "peticiones": h["cuenta"],
                "promedio_ms": round(h["suma"] / h["cuenta"], 3) if h["cuenta"] else 0.0,
                "peor_ms": round(h["peor"], 3),
                "bajo_sla_pct": round(h["bajo_sla"] * 100 / h["cuenta"], 2) if h["cuenta"] else 0.0,
            }
            for hora, h in sorted(horas_total.items())
        ],
    }


def integraciones_medidas() -> list[str]:
    with _LOCK:
        return sorted(_muestras.keys())


def limpiar():
    """Para los tests, y para reiniciar la medición desde el panel."""
    with _BUCLE_LOCK:
        _bucle_muestras.clear()
        _bucle_acumulado.update({"suma": 0.0, "cuenta": 0, "peor": 0.0, "peor_ts": 0.0})
    with _LOCK:
        _muestras.clear()
        _totales.clear()
        _acumulado.clear()
        _distribucion.clear()
        _por_hora.clear()


# ─── Retraso del bucle de eventos ────────────────────────────────────────────
#
# El agujero que dejaba el cronómetro: arranca DENTRO del handler. Si el bucle
# de eventos está trabado, la petición espera antes de llegar ahí y ese tiempo
# es invisible desde adentro.
#
# Sin esta medición, "los 326 ms que no medimos son red" es una suposición.
# Con ella es un descarte: si el bucle responde en 2 ms el proceso está sano y
# la culpa es de la red; si responde en 200 ms el problema es nuestro.
#
# El método es el estándar: pedir dormir un tiempo fijo y medir cuánto se
# durmió de verdad. La diferencia es lo que el bucle tardó en volver a
# atenderte, o sea cuánto lo están ocupando otros.

INTERVALO_SONDEO_SEG = 0.1
MAX_MUESTRAS_BUCLE = 3_000

# Umbrales de interpretación, en milisegundos.
#
# Están calibrados para tolerar la granularidad del temporizador del sistema
# operativo, que NO es un problema del hub: Windows despierta las tareas con
# ~15 ms de resolución y Linux con ~1 ms. Un umbral de 5 ms daba "competencia
# interna" en cualquier máquina Windows aunque el proceso estuviera ocioso.
#
# 25 ms deja pasar esa granularidad y sigue detectando lo que importa: un
# bloqueo capaz de inflar la latencia de recepción está en el orden de las
# centenas de milisegundos, no de las decenas.
UMBRAL_HOLGADO_MS = 25
UMBRAL_BLOQUEADO_MS = 100

_bucle_muestras: deque = deque(maxlen=MAX_MUESTRAS_BUCLE)
# El peor caso se guarda CON su momento. Sin eso, un pico de hace tres días
# seguía apareciendo como alarma actual: el veredicto decía "hubo un bloqueo de
# 48 segundos" sobre algo ya resuelto. Un número acumulado sin contexto
# temporal deja de informar y empieza a hacer ruido.
_bucle_acumulado = {"suma": 0.0, "cuenta": 0, "peor": 0.0, "peor_ts": 0.0}

# Pasado este plazo, un pico deja de condicionar el veredicto: se sigue
# mostrando como dato histórico, pero el estado que se reporta es el actual.
SEGUNDOS_PICO_VIGENTE = 900
_BUCLE_LOCK = threading.Lock()


async def sondear_bucle_eventos():
    """
    Corre para siempre midiendo cuánto se atrasa el bucle de eventos.

    Es deliberadamente barato: dormir y restar. No compite con nada.
    """
    import asyncio

    while True:
        inicio = time.perf_counter()
        await asyncio.sleep(INTERVALO_SONDEO_SEG)
        real = time.perf_counter() - inicio
        atraso_ms = max(0.0, (real - INTERVALO_SONDEO_SEG) * 1000.0)

        with _BUCLE_LOCK:
            _bucle_muestras.append(atraso_ms)
            _bucle_acumulado["suma"] += atraso_ms
            _bucle_acumulado["cuenta"] += 1
            if atraso_ms > _bucle_acumulado["peor"]:
                _bucle_acumulado["peor"] = atraso_ms
                _bucle_acumulado["peor_ts"] = time.time()


def retraso_bucle() -> dict:
    """
    Cuánto se atrasa el bucle de eventos.

    Interpretación:
      · Bajo 5 ms  — el proceso está holgado; lo que mida el proveedor de más
                     es red, TLS o nginx
      · 5 a 50 ms  — hay competencia interna, vale la pena mirar qué
      · Sobre 50   — el bucle está bloqueado y ESO está inflando la latencia
    """
    with _BUCLE_LOCK:
        recientes = list(_bucle_muestras)
        acc = dict(_bucle_acumulado)

    promedio = round(acc["suma"] / acc["cuenta"], 3) if acc["cuenta"] else 0.0
    peor = round(acc["peor"], 3)

    # Se juzga por la MEDIANA, no por el peor caso. Un pico aislado —el sistema
    # operativo despertando tarde una vez— no describe el estado del proceso; la
    # mediana sí. El peor caso se muestra igual, pero no dispara el veredicto.
    mediana = _percentil(recientes, 0.5)

    # El veredicto mira las DOS cosas, porque describen problemas distintos:
    #
    #   · La mediana dice cómo está el proceso de manera sostenida.
    #   · El peor caso dice si hubo bloqueos puntuales.
    #
    # Mirar solo la mediana escondía un bloqueo real de 350 ms entre cuatro
    # sondeos rápidos. Mirar solo el peor caso daba alarma por la granularidad
    # del temporizador de Windows, que despierta con ~15 ms de retraso.
    # Un pico viejo no describe el estado actual. Se sigue mostrando, pero
    # solo condiciona el veredicto mientras esté vigente.
    edad_pico = time.time() - acc["peor_ts"] if acc["peor_ts"] else None
    pico_vigente = edad_pico is not None and edad_pico < SEGUNDOS_PICO_VIGENTE

    if mediana >= UMBRAL_BLOQUEADO_MS:
        veredicto = ("El bucle se está bloqueando de forma sostenida. "
                     "Esto SÍ infla la latencia de recepción.")
    elif mediana >= UMBRAL_HOLGADO_MS:
        veredicto = "Hay competencia interna moderada. Revisable, pero no es el cuello."
    elif peor >= UMBRAL_BLOQUEADO_MS and pico_vigente:
        veredicto = (f"El bucle está holgado en general, pero hubo un bloqueo puntual de "
                     f"{peor:.0f} ms hace {_fmt_edad(edad_pico)}. "
                     f"Vale mirar qué pasó en ese momento.")
    else:
        veredicto = "El bucle está holgado: la latencia que vea el proveedor no viene de acá."

    return {
        "promedio_ms": promedio,
        "mediana_ms": round(_percentil(recientes, 0.5), 3),
        "p95_ms": round(_percentil(recientes, 0.95), 3),
        "peor_ms": peor,
        "sondeos": acc["cuenta"],
        "peor_hace_seg": int(edad_pico) if edad_pico is not None else None,
        "pico_vigente": pico_vigente,
        "veredicto": veredicto,
    }
