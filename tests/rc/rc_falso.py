"""
v1.9.11 — Cliente de RC falso para los tests de punta a punta.

Reemplaza SOLO la llamada de red: el worker, el cliente SOAP del hub
(rc_soap.RCSOAPClient._send_batch_sync) y construir_evento_rc corren de
verdad. Lo que zeep recibe se serializa contra el WSDL REAL de RC
(tests/fixtures/rc_wsdl/, bajado el 02/10/2026): si un valor no entra en el
esquema, el test falla igual que fallaría el envío.

Uso:
    falso = instalar(monkeypatch)
    ...procesar la cola...
    falso.eventos()   # los diccionarios que recibió zeep, en orden
    falso.nodos()     # los <Event> del sobre SOAP, como {campo: texto}
"""
import copy
import os

WSDL = os.path.join(os.path.dirname(__file__), "..", "fixtures", "rc_wsdl", "wsdl.xml")
NS = {"rc": "http://schemas.datacontract.org/2004/07/IronTracking"}


class ZeepFalso:
    def __init__(self):
        from zeep import Client
        self._real = Client(WSDL)
        self.service = self
        self.lotes = []
        self.sobres = []
        self._job = 900

    def GPSAssetTracking(self, token, argumento):
        # Copia: si el hub tocara el diccionario después de entregarlo, el
        # test vería lo que zeep recibió, no lo que quedó en memoria.
        self.lotes.append(copy.deepcopy(argumento))
        self.sobres.append(self._real.create_message(self._real.service, "GPSAssetTracking", token, argumento))
        respuesta = []
        for _ in argumento["Event"]:
            self._job += 1
            respuesta.append({"exception": None, "idJob": self._job})
        return respuesta

    def eventos(self) -> list[dict]:
        return [e for lote in self.lotes for e in lote["Event"]]

    def nodos(self) -> list[dict]:
        salida = []
        for sobre in self.sobres:
            for nodo in sobre.findall(".//rc:Event", NS):
                salida.append({hijo.tag.split("}")[1]: hijo.text for hijo in nodo
                               if len(hijo) == 0})
        return salida


def instalar(monkeypatch) -> ZeepFalso:
    """Envío real (RC_USE_MOCK apagado) contra el cliente falso."""
    from app.services import rc_soap
    falso = ZeepFalso()
    monkeypatch.setattr(rc_soap, "RC_USE_MOCK", False)
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_get_zeep_client", classmethod(lambda cls, endpoint: falso))
    monkeypatch.setattr(rc_soap.RCSOAPClient, "_get_token_sync", lambda self: "token-de-prueba")
    return falso


def procesar_cola(monkeypatch, proveedor: str, env: str) -> None:
    """Un ciclo del worker real sobre la cola de la integración."""
    import asyncio
    from app.worker import processor
    monkeypatch.setattr(processor, "trigger_worker", lambda *a, **k: None)
    asyncio.run(processor.process_provider_events(proveedor, env))
