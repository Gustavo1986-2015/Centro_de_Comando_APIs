K = "test_fallidos_v1910"
D = "app/api/routers/dashboard.py"
JS = "frontend/static/dashboard.js"
MUTACIONES = [
    ("2: fallidos ahora solo los de hoy", D,
     'func.sum(case((NormalizedRCEvent.status == "failed", 1), else_=0)).label("failed_now"),',
     'func.sum(case(((NormalizedRCEvent.status == "failed") & (NormalizedRCEvent.created_at >= today_start), 1), else_=0)).label("failed_now"),',
     "numero_grande or tarjeta_con_los_datos_reales"),
    ("2: el número grande vuelve a ser el acumulado", D,
     '        "fallidos_ahora": total_failed_now,\n', '        "fallidos_ahora": total_failed,\n',
     "numero_grande or al_recuperarlos"),
    ("2: sin recuperados", D,
     '        "recuperados_hoy": total_recovered,\n', '        "recuperados_hoy": 0,\n', "al_recuperarlos"),
    ("2: el reintento no suma recuperados", "app/api/routers/reintentos.py",
     "            sumar_recuperados(p, e, cambiados)\n", "            pass\n", "al_recuperarlos"),
    ("2: recuperados suma 1 por pedido", "app/worker/processor.py",
     # v1.9.11: la suma pasó a estadisticas_diarias.sumar (UPSERT).
     "        estadisticas_diarias.sumar(provider, env, recuperados=cantidad)",
     "        estadisticas_diarias.sumar(provider, env, recuperados=1)", "al_recuperarlos"),
    # v1.9.11: la migración de daily_stats (estadisticas_diarias.fusionar_duplicados)
    # también agrega la columna, porque la necesita para fusionar. Se rompen las dos.
    ("2: sin migración de recovered_count", [
        ("app/database.py",
         '                cursor.execute("ALTER TABLE daily_stats ADD COLUMN recovered_count INTEGER DEFAULT 0")\n',
         "                pass\n"),
        ("app/core/estadisticas_diarias.py",
         '            conexion.execute("ALTER TABLE daily_stats ADD COLUMN recovered_count INTEGER DEFAULT 0")\n',
         "            pass\n"),
     ], None, None, "migracion_agrega_recovered_count"),
    ("2 JS: el número grande con el acumulado", JS,
     "updateValue('val-failed', data.fallidos_ahora ?? data.failed);",
     "updateValue('val-failed', data.failed);", "tarjeta_con_los_datos_reales or boton_solo_con_fallidos"),
    ("2 JS: sin hoy y recuperados", JS,
     "if (el) el.textContent = `hoy: ${Number(data.fallidos_hoy ?? data.failed ?? 0).toLocaleString()} · recuperados: ${Number(data.recuperados_hoy ?? 0).toLocaleString()}`;",
     "if (el) el.textContent = '';", "tarjeta_con_los_datos_reales"),
    ("2 JS: el botón aunque no haya fallidos", JS,
     "if (b) b.style.display = currentStatusFilter === 'failed' && _fallidosAhora > 0 ? '' : 'none';",
     "if (b) b.style.display = currentStatusFilter === 'failed' ? '' : 'none';", "boton_solo_con_fallidos"),
    ("2 JS: el botón sin el filtro", JS,
     "if (b) b.style.display = currentStatusFilter === 'failed' && _fallidosAhora > 0 ? '' : 'none';",
     "if (b) b.style.display = _fallidosAhora > 0 ? '' : 'none';", "boton_solo_con_fallidos"),
]
