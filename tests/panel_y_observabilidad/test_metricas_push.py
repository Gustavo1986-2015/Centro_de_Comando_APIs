"""
Regresión del `deque mutated during iteration` en las métricas push.

El bug saltó 123 veces en 4 segundos bajo carga durante la certificación. La
excepción aborta el cálculo ENTERO —no devuelve un parcial— así que la tarjeta
conservaba el valor de un momento más tranquilo. Sesgo optimista justo en los
picos, que es cuando el número importa.
"""
import threading
import time

from app.api.routers import dashboard


def test_leer_metricas_mientras_se_escriben_no_falla():
    """
    Cuatro hilos escribiendo y tres leyendo, que es el patrón que lo disparaba.
    Se fuerza la ventana a casi cero para que cada `append` provoque un
    `popleft` y la mutación sea constante.
    """
    dashboard.push_latency_store.clear()
    ventana_original = dashboard.PUSH_WIN_SECS
    dashboard.PUSH_WIN_SECS = 0.0005

    errores = []
    parar = threading.Event()

    def escribir(nombre):
        while not parar.is_set():
            dashboard.record_push_latency(f"{nombre}:prod", 0.1)

    def leer(clave):
        while not parar.is_set():
            try:
                dashboard.get_push_stats(clave)
            except Exception as e:
                errores.append(f"{clave}: {type(e).__name__}: {e}")

    hilos = [threading.Thread(target=escribir, args=(n,), daemon=True)
             for n in ("schmitz", "protrack", "tercero", "cuarto")]
    hilos += [threading.Thread(target=leer, args=(c,), daemon=True)
              for c in ("all", "schmitz", "schmitz:prod")]

    try:
        for h in hilos:
            h.start()
        time.sleep(3)
    finally:
        parar.set()
        for h in hilos:
            h.join(timeout=2)
        dashboard.PUSH_WIN_SECS = ventana_original
        dashboard.push_latency_store.clear()

    assert not errores, f"{len(errores)} fallas de concurrencia: {errores[:3]}"


def test_crear_claves_nuevas_mientras_se_recorre_el_diccionario():
    """
    El tercer sitio, que no estaba protegido: recorrer el diccionario mientras
    se le insertan claves da "dictionary changed size during iteration".
    """
    dashboard.push_latency_store.clear()
    errores = []
    parar = threading.Event()

    def crear_claves():
        i = 0
        while not parar.is_set():
            dashboard.record_push_latency(f"proveedor{i}:prod", 0.05)
            i += 1

    def recorrer():
        while not parar.is_set():
            try:
                {k: dashboard.get_push_stats(k)
                 for k in list(dashboard.push_latency_store.keys())}
            except Exception as e:
                errores.append(f"{type(e).__name__}: {e}")

    hilos = [threading.Thread(target=crear_claves, daemon=True),
             threading.Thread(target=recorrer, daemon=True)]
    try:
        for h in hilos:
            h.start()
        time.sleep(2)
    finally:
        parar.set()
        for h in hilos:
            h.join(timeout=2)
        dashboard.push_latency_store.clear()

    assert not errores, f"{len(errores)} fallas: {errores[:3]}"


def test_las_metricas_siguen_calculando_bien():
    """Proteger con lock no puede cambiar el resultado."""
    dashboard.push_latency_store.clear()
    for _ in range(10):
        dashboard.record_push_latency("schmitz:prod", 0.1)      # 100 ms
    for _ in range(10):
        dashboard.record_push_latency("schmitz:prod", 0.4)      # 400 ms

    stats = dashboard.get_push_stats("schmitz:prod")
    assert stats["count"] == 20
    assert stats["avg_ms"] == 250.0
    # El SLA son 250 ms: los de 100 cumplen, los de 400 no.
    assert stats["compliance_pct"] == 50.0
    dashboard.push_latency_store.clear()


def test_consultar_por_proveedor_suma_sus_entornos():
    dashboard.push_latency_store.clear()
    dashboard.record_push_latency("schmitz:prod", 0.1)
    dashboard.record_push_latency("schmitz:test", 0.3)
    dashboard.record_push_latency("protrack:prod", 0.9)

    assert dashboard.get_push_stats("schmitz")["count"] == 2
    assert dashboard.get_push_stats("schmitz:prod")["count"] == 1
    assert dashboard.get_push_stats("all")["count"] == 3
    dashboard.push_latency_store.clear()
