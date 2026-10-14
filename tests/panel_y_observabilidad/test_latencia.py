"""
Regresión del desglose de latencia y del tope de muestras.

CONTEXTO: la certificación de Schmitz del 14/09 dio 327 ms contra un límite de
250, y no teníamos forma de saber en qué tramo se iba el tiempo. Además, la
latencia crecía a lo largo del test — causado por el store de métricas sin tope.
"""
import threading
import time

from app.api.routers import dashboard
from app.core import latencia


def test_los_tramos_suman_el_total():
    """
    Cada marca mide desde la marca anterior, no desde el inicio. Si no sumaran,
    el desglose no explicaría el total y sería inútil para decidir qué optimizar.
    """
    latencia.limpiar()
    c = latencia.Cronometro("schmitz", "prod")
    time.sleep(0.01)
    c.marca("auth")
    time.sleep(0.01)
    c.marca("parseo")
    time.sleep(0.01)
    c.marca("encolado")
    c.cerrar()

    d = latencia.desglose("schmitz", "prod")
    tramos = {t["tramo"]: t["promedio_ms"] for t in d["tramos"]}
    suma = tramos["auth"] + tramos["parseo"] + tramos["encolado"]
    assert abs(suma - tramos["total_handler"]) < 5, (
        f"Los tramos suman {suma:.1f} ms y el total dice {tramos['total_handler']:.1f} ms"
    )
    latencia.limpiar()


def test_distingue_promedio_de_picos():
    """
    Un promedio bajo con p95 alto es un problema distinto de una lentitud
    pareja. Durante la certificación el promedio era 327 ms y el máximo 6,8 s:
    solo con el promedio esa diferencia no se ve.
    """
    latencia.limpiar()
    for i in range(100):
        c = latencia.Cronometro("schmitz", "prod")
        time.sleep(0.05 if i == 99 else 0.001)   # un solo pico
        c.marca("parseo")
        c.cerrar()

    t = next(x for x in latencia.desglose("schmitz", "prod")["tramos"]
             if x["tramo"] == "parseo")
    assert t["mediana_ms"] < t["peor_ms"] / 5, "El pico no se distingue del caso típico"
    latencia.limpiar()


def test_el_promedio_es_exacto_pero_la_memoria_acotada():
    """
    Las dos propiedades a la vez, que era el punto:

    · El promedio cuenta TODOS los eventos, no los últimos 10.000. A 40 msg/s
      un tope de 10.000 serían cuatro minutos, y no habría cómo compararlo
      contra el número que el proveedor informa al cabo de 24 horas.
    · Las muestras guardadas para los percentiles sí están acotadas: sin tope,
      el store llegaba a millones de entradas y cada consulta copiaba la lista
      entera. Medido: 992 ms por consulta.
    """
    latencia.limpiar()
    total = latencia.MAX_MUESTRAS + 500
    for _ in range(total):
        c = latencia.Cronometro("schmitz", "prod")
        c.marca("parseo")
        c.cerrar()

    d = latencia.desglose("schmitz", "prod")
    assert d["muestras"] == total, "El promedio no cubre toda la corrida"
    assert d["muestras_percentiles"] == latencia.MAX_MUESTRAS, "La memoria no está acotada"
    latencia.limpiar()


def test_separa_por_proveedor_y_entorno():
    latencia.limpiar()
    for prov, env in (("schmitz", "prod"), ("schmitz", "test"), ("protrack", "prod")):
        c = latencia.Cronometro(prov, env)
        c.marca("parseo")
        c.cerrar()

    assert latencia.desglose("schmitz", "prod")["muestras"] == 1
    assert latencia.desglose("schmitz")["muestras"] == 2
    assert latencia.desglose()["muestras"] == 3
    latencia.limpiar()


def test_sin_muestras_no_rompe():
    latencia.limpiar()
    d = latencia.desglose("inexistente", "prod")
    assert d["muestras"] == 0
    assert d["tramos"] == []


def test_medir_bajo_concurrencia_no_falla():
    """El handler corre en varios hilos: el store tiene que aguantarlo."""
    latencia.limpiar()
    errores = []

    def medir():
        try:
            for _ in range(200):
                c = latencia.Cronometro("schmitz", "prod")
                c.marca("parseo")
                c.cerrar()
        except Exception as e:
            errores.append(str(e))

    def leer():
        try:
            for _ in range(200):
                latencia.desglose()
        except Exception as e:
            errores.append(str(e))

    hilos = [threading.Thread(target=medir) for _ in range(4)]
    hilos += [threading.Thread(target=leer) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert not errores, f"Fallas de concurrencia: {errores[:3]}"
    latencia.limpiar()


# ─── El tope del store de latencias push ─────────────────────────────────────

def test_la_memoria_no_crece_con_el_volumen():
    """
    El freno progresivo de la certificación: 24 horas de muestras sin límite
    son 3,4 millones de tuplas a 40 msg/s, y cada consulta copiaba esa lista
    entera. Medido entonces: 992 ms por consulta.

    Ahora se guardan acumuladores, así que la memoria es constante: un evento o
    un millón ocupan lo mismo.
    """
    dashboard.reset_push_stats()
    for _ in range(50_000):
        dashboard.record_push_latency("schmitz:prod", 0.1)

    acc = dashboard.push_latency_store["schmitz:prod"]
    # Un contador por rango, más los escalares. Nada proporcional al volumen.
    assert len(acc["buckets"]) == len(dashboard.BUCKETS_MS) + 1
    assert acc["count"] == 50_000
    assert not any(isinstance(v, (list, tuple)) and len(v) > 20 for v in acc.values())
    dashboard.reset_push_stats()


def test_la_consulta_no_se_degrada_con_el_volumen():
    """Es la propiedad que importa: el costo no puede depender del caudal."""
    import time as _t

    dashboard.reset_push_stats()
    for _ in range(1_000):
        dashboard.record_push_latency("schmitz:prod", 0.1)
    t0 = _t.perf_counter()
    dashboard.get_push_stats("all")
    con_mil = _t.perf_counter() - t0

    for _ in range(200_000):
        dashboard.record_push_latency("schmitz:prod", 0.1)
    t0 = _t.perf_counter()
    dashboard.get_push_stats("all")
    con_doscientos_mil = _t.perf_counter() - t0

    assert con_doscientos_mil < con_mil * 5 + 0.001, (
        f"La consulta se degradó: {con_mil*1000:.3f} ms con mil muestras, "
        f"{con_doscientos_mil*1000:.3f} ms con doscientas mil"
    )
    dashboard.reset_push_stats()


def test_el_promedio_es_exacto_sobre_todos_los_eventos():
    """
    Acotar el deque habría dado el promedio de los últimos N, no de todos. La
    ventana habría cambiado sola según el caudal: cuatro minutos a 40 msg/s,
    cien horas a 100 por minuto, con la tarjeta diciendo "24h" en los dos casos.
    """
    dashboard.reset_push_stats()
    for _ in range(100_000):
        dashboard.record_push_latency("schmitz:prod", 0.1)      # 100 ms
    for _ in range(100_000):
        dashboard.record_push_latency("schmitz:prod", 0.3)      # 300 ms

    stats = dashboard.get_push_stats("schmitz:prod")
    assert stats["count"] == 200_000
    assert stats["avg_ms"] == 200.0, "El promedio no cubre todos los eventos"
    # El SLA son 250 ms: cumplen exactamente la mitad.
    assert stats["compliance_pct"] == 50.0
    dashboard.reset_push_stats()


def test_los_percentiles_distinguen_los_picos():
    """Un promedio bajo con p99 alto es un problema distinto de lentitud pareja."""
    dashboard.reset_push_stats()
    for _ in range(950):
        dashboard.record_push_latency("schmitz:prod", 0.02)     # 20 ms
    for _ in range(50):
        dashboard.record_push_latency("schmitz:prod", 3.0)      # 3 s

    stats = dashboard.get_push_stats("schmitz:prod")
    # El percentil se informa como el TECHO del rango donde cae: es aproximado
    # por diseño, y para decidir si hay picos alcanza.
    assert stats["p50_ms"] <= 25, "La mediana debería reflejar el caso típico"
    assert stats["p99_ms"] >= 1000, "El p99 no refleja los picos"
    assert stats["max_ms"] >= 3000, "El peor caso tiene que ser exacto"
    dashboard.reset_push_stats()


def test_separa_los_entornos_y_los_suma_por_proveedor():
    dashboard.reset_push_stats()
    dashboard.record_push_latency("schmitz:prod", 0.1)
    dashboard.record_push_latency("schmitz:test", 0.3)
    dashboard.record_push_latency("protrack:prod", 0.9)

    assert dashboard.get_push_stats("schmitz:prod")["count"] == 1
    assert dashboard.get_push_stats("schmitz")["count"] == 2
    assert dashboard.get_push_stats("all")["count"] == 3
    dashboard.reset_push_stats()


# ─── Acumulado de toda la corrida ────────────────────────────────────────────

def test_el_promedio_cubre_mas_alla_de_la_ventana():
    """
    El deque acotado son MAX_MUESTRAS: a 40 msg/s, pocos minutos. El promedio
    tiene que salir del acumulado y cubrir TODA la corrida, que es lo comparable
    contra lo que reporta el proveedor al final de un test de 24 horas.
    """
    latencia.limpiar()
    total = latencia.MAX_MUESTRAS + 3_000
    for _ in range(total):
        c = latencia.Cronometro("schmitz", "prod")
        c.cerrar()

    d = latencia.desglose("schmitz", "prod")
    t = next(x for x in d["tramos"] if x["tramo"] == "total_handler")

    assert d["muestras_percentiles"] == latencia.MAX_MUESTRAS, "La ventana no está acotada"
    assert t["muestras"] == total, (
        f"El promedio se calculó sobre {t['muestras']} y no sobre las {total} "
        f"peticiones: no sirve para una corrida larga"
    )
    latencia.limpiar()


def test_el_peor_caso_no_se_pierde_al_rotar_la_ventana():
    """
    Un pico al principio de una corrida de 24 horas no puede desaparecer del
    informe solo porque la ventana ya lo descartó.
    """
    latencia.limpiar()
    c = latencia.Cronometro("schmitz", "prod")
    time.sleep(0.05)
    c.cerrar()
    pico = next(x for x in latencia.desglose("schmitz", "prod")["tramos"]
                if x["tramo"] == "total_handler")["peor_ms"]

    for _ in range(latencia.MAX_MUESTRAS + 100):
        c = latencia.Cronometro("schmitz", "prod")
        c.cerrar()

    ahora = next(x for x in latencia.desglose("schmitz", "prod")["tramos"]
                 if x["tramo"] == "total_handler")["peor_ms"]
    assert ahora == pico, "Se perdió el peor caso al rotar la ventana"
    latencia.limpiar()


def test_reiniciar_borra_tambien_el_acumulado():
    latencia.limpiar()
    c = latencia.Cronometro("schmitz", "prod")
    c.cerrar()
    latencia.limpiar()
    assert latencia.desglose("schmitz", "prod")["muestras"] == 0


# ─── Detalle por proveedor y evidencia exportable ────────────────────────────

def test_el_desglose_separa_por_integracion():
    """
    Sin esto, un proveedor lento quedaría promediado con uno rápido y no se
    vería cuál es cuál. Para discutir una certificación hace falta el detalle.
    """
    latencia.limpiar()
    for _ in range(10):
        latencia.Cronometro("schmitz", "prod").cerrar()
    for _ in range(5):
        latencia.Cronometro("schmitz", "test").cerrar()
    for _ in range(3):
        latencia.Cronometro("protrack", "prod").cerrar()

    assert latencia.desglose("schmitz", "prod")["muestras"] == 10
    assert latencia.desglose("schmitz", "test")["muestras"] == 5
    assert latencia.desglose("schmitz")["muestras"] == 15
    assert latencia.desglose()["muestras"] == 18
    latencia.limpiar()


def test_la_distribucion_muestra_la_forma_no_solo_el_promedio():
    """
    Un promedio esconde la forma: 327 ms de promedio puede ser todo parejo o
    90% rápido con 10% muy lento. Son problemas distintos.
    """
    latencia.limpiar()
    for i in range(100):
        c = latencia.Cronometro("schmitz", "prod")
        if i < 5:
            time.sleep(0.03)      # unos pocos lentos
        c.cerrar()

    d = latencia.desglose("schmitz", "prod")
    assert d["distribucion"], "No se calculó la distribución"
    assert sum(b["peticiones"] for b in d["distribucion"]) == 100
    assert abs(sum(b["porcentaje"] for b in d["distribucion"]) - 100) < 1
    # Hay al menos dos tramos distintos: rápidos y lentos.
    assert len(d["distribucion"]) >= 2
    latencia.limpiar()


def test_la_distribucion_viene_ordenada_por_tramo():
    """Ordenar alfabéticamente pondría '1000-2500' antes que '5-10'."""
    latencia.limpiar()
    for i in range(60):
        c = latencia.Cronometro("schmitz", "prod")
        if i % 20 == 0:
            time.sleep(0.03)
        c.cerrar()

    tramos = [b["tramo"] for b in latencia.desglose("schmitz", "prod")["distribucion"]]
    limites = [float("inf") if t.startswith(">") else float(t.split("-")[0]) for t in tramos]
    assert limites == sorted(limites), f"Distribución desordenada: {tramos}"
    latencia.limpiar()


def test_informa_el_porcentaje_bajo_el_umbral():
    """
    Es el número que pide una certificación: no "el promedio dio X" sino "el
    N% cumplió".
    """
    latencia.limpiar()
    for _ in range(50):
        latencia.Cronometro("schmitz", "prod").cerrar()

    d = latencia.desglose("schmitz", "prod")
    assert d["sla_ms"] == latencia.SLA_MS
    assert d["bajo_sla_pct"] == 100.0, "Peticiones instantáneas deberían cumplir"
    latencia.limpiar()


def test_la_distribucion_cubre_toda_la_corrida():
    """Igual que los acumuladores: no puede perder lo viejo al rotar la ventana."""
    latencia.limpiar()
    total = latencia.MAX_MUESTRAS + 2_000
    for _ in range(total):
        latencia.Cronometro("schmitz", "prod").cerrar()

    d = latencia.desglose("schmitz", "prod")
    assert sum(b["peticiones"] for b in d["distribucion"]) == total
    latencia.limpiar()


# ─── Retraso del bucle de eventos ────────────────────────────────────────────

def test_el_bucle_holgado_da_retraso_bajo():
    """
    Es el descarte que faltaba: si el bucle responde al instante, la latencia
    que mida el proveedor no viene del proceso.
    """
    import asyncio

    latencia.limpiar()

    async def correr():
        t = asyncio.create_task(latencia.sondear_bucle_eventos())
        await asyncio.sleep(1.0)
        t.cancel()

    asyncio.run(correr())
    rb = latencia.retraso_bucle()
    assert rb["sondeos"] >= 5, "La sonda no llegó a medir"
    # Se mira la mediana, no el peor caso: en Windows el temporizador despierta
    # con ~15 ms de granularidad y un pico aislado no describe el proceso.
    assert rb["mediana_ms"] < latencia.UMBRAL_HOLGADO_MS, (
        f"Bucle holgado reportando atraso alto: {rb}"
    )
    assert "no viene de acá" in rb["veredicto"]
    latencia.limpiar()


def test_el_bucle_bloqueado_se_detecta():
    """
    Lo que importa de verdad: que un bloqueo real aparezca. Se simula con una
    tarea que ocupa el bucle sin cederlo.
    """
    import asyncio
    import time as _t

    latencia.limpiar()

    async def correr():
        t = asyncio.create_task(latencia.sondear_bucle_eventos())
        await asyncio.sleep(0.15)
        _t.sleep(0.4)          # bloqueo sincrónico: nadie más puede correr
        await asyncio.sleep(0.3)
        t.cancel()

    asyncio.run(correr())
    rb = latencia.retraso_bucle()
    assert rb["peor_ms"] > 100, f"No detectó un bloqueo de 400 ms: {rb}"
    # Aunque la mediana esté baja, un bloqueo puntual no puede quedar escondido.
    assert "bloqueo puntual" in rb["veredicto"] or "sostenida" in rb["veredicto"], (
        f"El bloqueo no aparece en el veredicto: {rb['veredicto']}"
    )
    latencia.limpiar()


def test_sin_sondeos_no_rompe():
    latencia.limpiar()
    rb = latencia.retraso_bucle()
    assert rb["sondeos"] == 0
    assert rb["promedio_ms"] == 0.0


def test_reiniciar_borra_tambien_el_retraso_del_bucle():
    import asyncio

    async def correr():
        t = asyncio.create_task(latencia.sondear_bucle_eventos())
        await asyncio.sleep(0.4)
        t.cancel()

    asyncio.run(correr())
    assert latencia.retraso_bucle()["sondeos"] > 0
    latencia.limpiar()
    assert latencia.retraso_bucle()["sondeos"] == 0


# ─── Cortes por hora y aviso de incumplimiento ───────────────────────────────

def test_hay_corte_por_hora():
    """
    El acumulado desde el arranque es plano. Para evidencia hace falta poder
    decir "entre las 10 y las 11 el promedio fue X": durante la certificación
    la latencia creció a lo largo del día y eso solo se ve así.
    """
    latencia.limpiar()
    for _ in range(50):
        latencia.Cronometro("schmitz", "prod").cerrar()

    horas = latencia.desglose("schmitz", "prod")["por_hora"]
    assert len(horas) == 1
    assert horas[0]["peticiones"] == 50
    assert horas[0]["bajo_sla_pct"] == 100.0
    assert "UTC" not in horas[0]["hora"]     # la etiqueta la agrega el panel
    latencia.limpiar()


def test_las_horas_vienen_ordenadas():
    latencia.limpiar()
    for _ in range(10):
        latencia.Cronometro("schmitz", "prod").cerrar()
    horas = [h["hora"] for h in latencia.desglose("schmitz", "prod")["por_hora"]]
    assert horas == sorted(horas)
    latencia.limpiar()


def test_no_acumula_horas_sin_limite():
    """Memoria fija: 48 horas cubren una certificación y su día previo."""
    latencia.limpiar()
    assert latencia.HORAS_RETENIDAS <= 72, (
        "Retener demasiadas horas vuelve a ser el problema de la estructura "
        "que crece sin límite"
    )


def test_no_avisa_con_pocas_muestras():
    """Gritar por una muestra de tres entrena a ignorar el aviso."""
    latencia.limpiar()
    for _ in range(10):
        latencia.Cronometro("schmitz", "prod").cerrar()
    assert latencia.desglose("schmitz", "prod")["alerta_sla"] is False
    latencia.limpiar()


def test_avisa_cuando_el_cumplimiento_cae(monkeypatch):
    """
    De la certificación fallida te enteraste cuando lo dijo el proveedor. Este
    aviso da horas en vez de días.
    """
    latencia.limpiar()
    monkeypatch.setattr(latencia, "MINIMO_PARA_ALERTAR", 10)
    monkeypatch.setattr(latencia, "SLA_MS", 1)     # casi todo lo incumple

    for _ in range(20):
        c = latencia.Cronometro("schmitz", "prod")
        time.sleep(0.003)
        c.cerrar()

    d = latencia.desglose("schmitz", "prod")
    assert d["alerta_sla"] is True, f"No avisó con {d['bajo_sla_pct']}% de cumplimiento"
    latencia.limpiar()


def test_no_avisa_cuando_todo_cumple():
    latencia.limpiar()
    for _ in range(latencia.MINIMO_PARA_ALERTAR + 50):
        latencia.Cronometro("schmitz", "prod").cerrar()
    assert latencia.desglose("schmitz", "prod")["alerta_sla"] is False
    latencia.limpiar()


# ─── Legibilidad: cada tramo se explica solo ─────────────────────────────────

def test_cada_tramo_tiene_nombre_y_explicacion():
    """
    Si un proveedor pide datos, hay que poder decir qué mide cada fila sin
    abrir el código.
    """
    latencia.limpiar()
    c = latencia.Cronometro("schmitz", "prod")
    for tramo, _, _ in latencia.TRAMOS:
        if tramo != "total_handler":
            c.marca(tramo)
    c.cerrar()

    for t in latencia.desglose("schmitz", "prod")["tramos"]:
        assert t["etiqueta"], f"Tramo sin nombre: {t['tramo']}"
        assert len(t.get("explicacion", "")) > 40, (
            f"El tramo '{t['tramo']}' no se explica solo: {t.get('explicacion')}"
        )
    latencia.limpiar()


def test_los_tramos_estan_numerados_en_orden_de_ejecucion():
    """El nombre tiene que dejar claro qué pasa antes y qué después."""
    etiquetas = [e for _, e, _ in latencia.TRAMOS if not e.startswith("TOTAL")]
    numeros = [int(e.split(".")[0]) for e in etiquetas]
    assert numeros == sorted(numeros), f"Tramos desordenados: {etiquetas}"


# ─── El peor caso envejece ───────────────────────────────────────────────────

def test_un_pico_viejo_deja_de_condicionar_el_veredicto():
    """
    Sin esto, un bloqueo de hace tres días seguía apareciendo como alarma
    actual. Un número acumulado sin contexto temporal deja de informar y
    empieza a hacer ruido.
    """
    import asyncio
    import time as _t

    latencia.limpiar()

    async def correr():
        t = asyncio.create_task(latencia.sondear_bucle_eventos())
        await asyncio.sleep(0.15)
        _t.sleep(0.35)
        await asyncio.sleep(0.3)
        t.cancel()

    asyncio.run(correr())

    reciente = latencia.retraso_bucle()
    assert reciente["pico_vigente"] is True
    assert "bloqueo puntual" in reciente["veredicto"]

    # Se envejece el pico más allá del plazo de vigencia.
    latencia._bucle_acumulado["peor_ts"] = _t.time() - (latencia.SEGUNDOS_PICO_VIGENTE + 60)

    viejo = latencia.retraso_bucle()
    assert viejo["pico_vigente"] is False
    assert "bloqueo puntual" not in viejo["veredicto"], (
        "Un pico viejo sigue gritando como si fuera actual"
    )
    # Pero el dato no se pierde: sigue visible como historia.
    assert viejo["peor_ms"] == reciente["peor_ms"]
    assert viejo["peor_hace_seg"] > latencia.SEGUNDOS_PICO_VIGENTE
    latencia.limpiar()


def test_sin_picos_no_reporta_edad():
    latencia.limpiar()
    rb = latencia.retraso_bucle()
    assert rb["peor_hace_seg"] is None
    assert rb["pico_vigente"] is False
