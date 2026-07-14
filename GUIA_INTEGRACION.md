# 🚀 Guía Definitiva: Integración de Nuevas APIs al Hub Telemático

¡Bienvenido/a al equipo! Si estás leyendo esto, es porque tienes la misión de conectar un nuevo proveedor de GPS o telemática a nuestro **Hub Central**. 

Nuestro Hub es una pieza de software de calidad Enterprise (robusta, a prueba de fallos y asíncrona). Para mantener este estándar de calidad y asegurar que ninguna nueva integración rompa el sistema, hemos establecido una metodología estricta. Sigue estos pasos y tu integración será un éxito.

---

## 🧭 Conceptos Clave antes de empezar

1. **El Escudo:** El Hub asume que todos los proveedores externos envían basura (datos nulos, rotos, incompletos). Tu trabajo es programar de forma defensiva para que esos errores reboten en el escudo y no cuelguen nuestro servidor.
2. **El Modelo Canónico:** No importa si el proveedor habla en chino, ruso, XML o JSON. Dentro de nuestro Hub, TODOS los vehículos hablan un único idioma: el `RCCanonicalModel`. Tu misión principal es traducir los datos del proveedor a este formato.
3. **Deduplicación Automática:** No te preocupes por filtrar eventos repetidos o rebotes de puerta en el código. El Hub tiene un motor central de Deduplicación. Tú solo pasa el evento crudo y el motor hará el resto basado en lo que configure el operador en la Interfaz Gráfica.

---

## 🛠️ Paso 1: Investigación y Blueprint

Antes de tirar una sola línea de código, necesitas entender a tu "enemigo":
* Pídele al proveedor su documentación oficial en PDF o Postman.
* Exige credenciales de prueba.
* **Lo más importante:** Solicita un ejemplo real (un texto en crudo) del JSON o XML que te van a enviar o que vas a recibir.

> [!IMPORTANT]  
> Debes identificar claramente dónde vienen estos 5 datos críticos en el JSON del proveedor: **Patente/IMEI, Latitud, Longitud, Velocidad y Fecha/Hora**.

---

## 🧪 Paso 2: Desarrollo Guiado por Pruebas (La Regla de Oro)

En este proyecto usamos **TDD (Test-Driven Development)**. Está terminantemente prohibido programar la integración sin antes escribir cómo se va a probar.

1. Abre el archivo `tests/conftest.py`.
2. Pega el JSON crudo que conseguiste en el Paso 1 y conviértelo en un `@pytest.fixture`.
3. Crea un archivo llamado `tests/test_[nombre]_mapper.py`.
4. Escribe pruebas que garanticen que, al procesar ese JSON, sale un objeto `RCCanonicalModel` perfecto, con la patente en mayúsculas, sin guiones, y sin que el código explote si le borras un campo a propósito.

---

## 💻 Paso 3: Escribir el "Mapper" (Traductor)

Una vez que tienes el test fallando (porque aún no hay código), es hora de programar.

1. Crea la carpeta `app/providers/[nombre]/`.
2. Crea el archivo `mapper.py`.
3. Escribe la lógica para extraer los datos.

> [!CAUTION]  
> **Programación Defensiva:** Nunca uses `diccionario["latitud"]` directamente, porque si no viene, Python lanzará un `KeyError` y matará el proceso. Usa siempre `diccionario.get("latitud", 0.0)` o bloques `try/except`.

Corre tus tests en la consola (`pytest tests/`). No avances al Paso 4 hasta que todo esté en color **verde**.

---

## 🔌 Paso 4: Conectar la Manguera (Push vs Pull)

¿Cómo nos llegan los datos?
* **¿Es PULL?** (Nosotros vamos a buscarlos, ej: Protrack). 
  Abre `app/worker/pull_engine.py` y agrega la lógica HTTP para pedir los datos cada X segundos y pasárselos a tu nuevo mapper.
* **¿Es PUSH?** (Ellos nos mandan los datos a nosotros, ej: Schmitz).
  El Hub ya tiene un Webhook universal en `app/api/routers/dynamic_webhook.py`. Rara vez tendrás que tocar esto, a menos que el proveedor tenga un sistema de contraseñas súper raro en las cabeceras (Headers).

---

## ⚙️ Paso 5: Diccionario de Sensores en la UI

Tu mapper debe dejar el código del sensor (ej: `alarma = "101"`) totalmente intacto y guardarlo en el campo `event_code`. 
No programes en duro *"Si 101 entonces Pánico"*. 

Una vez que la aplicación esté corriendo, dile al Operador de Monitoreo que entre a la web del Dashboard, vaya al **Integration Studio**, seleccione tu nuevo proveedor y cargue allí las "Reglas de Disparo" (qué código es Pánico, qué código es Puerta, etc.). Esto permite cambiar reglas en vivo sin reiniciar el servidor.

---

## 🚀 Paso 6: Observabilidad y Despliegue

1. Arranca la app en local con Docker o Uvicorn (`uvicorn main:app`).
2. Manda un dato falso y mira la consola.
3. Verifica que la latencia diga algo como `soap_avg_ms=750` y `sent=1 failed=0`.
4. Si todo es exitoso, commitea tus cambios y envíalos a la rama `main` para que el equipo de Infraestructura actualice el servidor de Producción.

> [!TIP]  
> Si usas el agente de IA (Antigravity/Gemini) para programar, simplemente pásale el JSON de prueba del proveedor y recuérdale: *"Usa el estándar definido en el AGENTS.md y en el PLAYBOOK"*. El agente hará el 90% del trabajo aburrido por ti de forma impecable.
