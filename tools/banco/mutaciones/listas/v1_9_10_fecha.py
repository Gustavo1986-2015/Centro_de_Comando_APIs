# v1.9.11: _textoFechaDescarte delega en _fechaHora (formato compartido, 6.1).
MUTACIONES = [
    ("4 JS: la fecha vuelve a toLocaleString", "frontend/static/dashboard.js",
     "            return ts ? _fechaHora(ts) : '—';\n        }\n\n        function _categoriaDescarte(",
     "            return ts ? new Date(ts * 1000).toLocaleString('en-US') : '—';\n        }\n\n        function _categoriaDescarte(",
     "fecha_se_muestra_en_24_h"),
]
