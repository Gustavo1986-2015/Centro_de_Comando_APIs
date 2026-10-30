"""Protrack simulado (el de prueba_funcional) con /api/track respondiendo 502:
el token se obtiene bien y el sondeo falla con la URL completa (B-12)."""
import sys
from http.server import ThreadingHTTPServer

sys.argv = [sys.argv[0], "-", "sim502"]
import prueba_funcional as pf  # noqa: E402


class Sim502(pf.Simulador):
    def do_GET(self):
        if self.path.startswith("/api/track"):
            pf.pedidos.append({"metodo": "GET", "ruta": "/api/track"})
            return self._json(502, {"message": "Bad Gateway"})
        return super().do_GET()


ThreadingHTTPServer(("127.0.0.1", pf.PUERTO_SIM), Sim502).serve_forever()
