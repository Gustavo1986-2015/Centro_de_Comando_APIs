"""
Velocidad estimada a partir de posiciones sucesivas (v1.9.10, Bloque 1).

POR QUÉ EXISTE

SIMON hace las transiciones de estado de los viajes con la velocidad. Tive no
la mide: cada evento de Tive llegaba a RC con speed "0" y SIMON veía el equipo
siempre detenido. El hub la estima con la distancia y el tiempo entre dos
lecturas del mismo equipo.

DÓNDE ESTÁ Y CÓMO SE REUTILIZARÍA

Este archivo es solo cálculo: funciones puras que reciben lecturas (momento,
latitud, longitud, método de ubicación, precisión) y devuelven velocidad y
motivo. No sabe nada de Tive, de su payload ni de dónde se guarda el historial.

  · Lo específico de Tive (qué es un equipo, ShipmentStopped, el tramo
    marítimo, el interruptor) está en app/providers/tive/modulo.py.
  · El historial persistente está en app/providers/tive/estado.py.

Para usarlo con otro proveedor alcanzaría con armar `Lectura`s con sus
posiciones, guardar un historial por equipo y llamar a `estimar`. HOY NO SE
USA EN NINGÚN OTRO PROVEEDOR: Schmitz y Protrack miden su velocidad.

SEMÁNTICA DEL VALOR (lo que va a RC)

  0      detenido: distancia menor al umbral, o el proveedor lo afirma
  1      no se pudo calcular: es un MARCADOR, no una velocidad
  >= 2   velocidad estimada en km/h

Si la distancia supera el umbral pero el cálculo redondea a 0 o 1, se informa
2: hubo movimiento real, y no puede confundirse con "detenido" ni con el
marcador.

DECISIONES DE DIRECCIÓN (no cambiar ni "mejorar")

  · Coordenadas redondeadas a 5 decimales antes de comparar.
  · Distancia (haversine) menor a UMBRAL_METROS: velocidad 0.
  · Si no, distancia / tiempo en km/h, entera. Se informa SIEMPRE: sin tope
    máximo ni filtro de saltos.
  · Se compara contra la lectura ANTERIOR EN EL TIEMPO, no contra la última
    recibida: Tive entrega con demoras y en ráfagas desordenadas.

El método de ubicación y la precisión de las dos lecturas NO entran en el
cálculo: viajan en el detalle para saber qué tan fiable es cada valor.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime

UMBRAL_METROS = 5
DECIMALES = 5
RADIO_TIERRA_METROS = 6_371_000

VELOCIDAD_DETENIDO = 0
VELOCIDAD_NO_CALCULADA = 1
VELOCIDAD_MINIMA_EN_MOVIMIENTO = 2

# Motivos. El código es estable (va en el detalle); el texto es para el panel.
DETENIDO_UMBRAL = "detenido_umbral"
ESTIMADA = "estimada"
SIN_POSICION_PREVIA = "sin_posicion_previa"
MISMA_HORA_COORDENADAS_DISTINTAS = "misma_hora_coordenadas_distintas"
DETENIDO_INFORMADO = "detenido_informado"     # el proveedor lo afirma (ShipmentStopped en Tive)
MARITIMO = "maritimo"
MEDIDA = "medida"                             # preparado: el proveedor mandó una velocidad medida

MOTIVOS = {
    DETENIDO_UMBRAL: "detenido por umbral",
    ESTIMADA: "estimada",
    SIN_POSICION_PREVIA: "sin posición previa",
    MISMA_HORA_COORDENADAS_DISTINTAS: "misma hora con coordenadas distintas",
    DETENIDO_INFORMADO: "detenido informado por el proveedor",
    MARITIMO: "marítimo",
    MEDIDA: "medida por el proveedor",
}


def redondear(valor: float) -> float:
    return round(float(valor), DECIMALES)


@dataclass
class Lectura:
    """Una posición de un equipo. lat/lon se guardan ya redondeadas."""
    momento: datetime
    lat: float
    lon: float
    metodo: str | None = None
    precision_m: float | None = None
    # Resultado ya calculado para esta lectura (para la lectura repetida).
    valor: int | None = None
    detalle: dict | None = field(default=None, repr=False)

    def __post_init__(self):
        self.lat, self.lon = redondear(self.lat), redondear(self.lon)

    def misma_posicion(self, otra: "Lectura") -> bool:
        return self.lat == otra.lat and self.lon == otra.lon

    def resumen(self) -> dict:
        return {"momento": self.momento.isoformat(), "lat": self.lat, "lon": self.lon,
                "metodo": self.metodo, "precision_m": self.precision_m}


@dataclass
class Resultado:
    valor: int
    motivo: str
    distancia_m: float | None = None
    segundos: float | None = None
    actual: Lectura | None = None
    anterior: Lectura | None = None
    # True: lectura repetida, se devolvió el valor ya calculado y el
    # historial no se toca.
    repetida: bool = False
    # True: la lectura entra al historial.
    guardar: bool = True
    # Lectura repetida: el detalle que se calculó la primera vez.
    detalle_previo: dict | None = field(default=None, repr=False)

    def detalle(self) -> dict:
        """El detalle completo, para el visor, el JSON RC y el panel."""
        if self.detalle_previo is not None:
            return dict(self.detalle_previo, lectura_repetida=True)
        return {
            "valor": self.valor,
            "motivo": self.motivo,
            "texto": MOTIVOS.get(self.motivo, self.motivo),
            "distancia_m": None if self.distancia_m is None else round(self.distancia_m, 1),
            "segundos": None if self.segundos is None else round(self.segundos, 1),
            "umbral_m": UMBRAL_METROS,
            "actual": self.actual.resumen() if self.actual else None,
            "anterior": self.anterior.resumen() if self.anterior else None,
        }


def distancia_metros(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine, en metros."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * RADIO_TIERRA_METROS * math.asin(min(1.0, math.sqrt(a)))


def anterior_en_el_tiempo(actual: Lectura, historial: list[Lectura]) -> Lectura | None:
    """La lectura más reciente ANTERIOR a la actual por su momento, no por su llegada."""
    previas = [l for l in historial if l.momento < actual.momento]
    return max(previas, key=lambda l: l.momento) if previas else None


def estimar(actual: Lectura, historial: list[Lectura], velocidad_medida: float | None = None) -> Resultado:
    """
    Velocidad de `actual` contra el historial del mismo equipo.

    `velocidad_medida`: si el proveedor algún día manda una velocidad medida,
    tiene prioridad sobre la estimada. Hoy ningún proveedor que use este
    módulo la manda.
    """
    if velocidad_medida is not None:
        return Resultado(max(VELOCIDAD_MINIMA_EN_MOVIMIENTO, int(round(velocidad_medida)))
                         if velocidad_medida >= 0.5 else VELOCIDAD_DETENIDO,
                         MEDIDA, actual=actual)

    misma_hora = [l for l in historial if l.momento == actual.momento]
    for previa in misma_hora:
        if previa.misma_posicion(actual) and previa.detalle is not None:
            # Lectura repetida: el valor ya calculado, sin tocar el historial.
            return Resultado(previa.valor, previa.detalle.get("motivo"), previa.detalle.get("distancia_m"),
                             previa.detalle.get("segundos"), actual=actual, repetida=True, guardar=False,
                             detalle_previo=previa.detalle)
    if misma_hora:
        # Misma hora, otro lugar: no se puede dividir por cero. Queda la
        # primera lectura de esa hora en el historial.
        return Resultado(VELOCIDAD_NO_CALCULADA, MISMA_HORA_COORDENADAS_DISTINTAS,
                         actual=actual, anterior=misma_hora[0], guardar=False)

    anterior = anterior_en_el_tiempo(actual, historial)
    if anterior is None:
        return Resultado(VELOCIDAD_NO_CALCULADA, SIN_POSICION_PREVIA, actual=actual)

    distancia = distancia_metros(anterior.lat, anterior.lon, actual.lat, actual.lon)
    segundos = (actual.momento - anterior.momento).total_seconds()
    if distancia < UMBRAL_METROS:
        return Resultado(VELOCIDAD_DETENIDO, DETENIDO_UMBRAL, distancia, segundos, actual, anterior)

    kmh = int(round(distancia / segundos * 3.6))
    # Movimiento real que redondea a 0 o 1: nunca "detenido" ni el marcador.
    return Resultado(max(kmh, VELOCIDAD_MINIMA_EN_MOVIMIENTO), ESTIMADA, distancia, segundos, actual, anterior)


def detenido_informado(actual: Lectura) -> Resultado:
    """El proveedor afirma que el equipo está detenido (en Tive, ShipmentStopped)."""
    return Resultado(VELOCIDAD_DETENIDO, DETENIDO_INFORMADO, actual=actual)


def maritimo(actual: Lectura | None = None) -> Resultado:
    """Posición marítima del contenedor: siempre 0 y no entra al historial."""
    return Resultado(VELOCIDAD_DETENIDO, MARITIMO, actual=actual, guardar=False)
