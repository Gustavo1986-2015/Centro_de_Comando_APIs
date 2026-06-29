# Preguntas Frecuentes (FAQs) y Disaster Recovery

## 1. ¿Qué pasa si pierdo la `MASTER_ENC_KEY` del archivo `.env`?
Si el archivo `.env` se borra por accidente o pierdes la llave maestra, **los datos cifrados en la base de datos se vuelven irrecuperables**. El sistema empezará a mostrar alertas en consola indicando `(clave rotada?)` porque no puede leer las contraseñas ni los secretos que están en la base de datos `system_config.db`.

**Procedimiento de Restauración (Disaster Recovery):**
No puedes recuperar el texto original. Debes forzar al sistema a generar una nueva llave y reingresar las credenciales:
1. Asegúrate de que no haya ninguna variable `MASTER_ENC_KEY` en tu `.env`.
2. Reinicia el servidor (`python main.py`). Al detectar que no hay llave, el sistema generará una nueva automáticamente y la guardará en el `.env`.
3. Ingresa al **Dashboard** -> pestaña **Configuración Global**.
4. Verás que los campos de contraseñas (RC Password, PUSH API Key) pueden aparecer vacíos o corruptos. 
5. Vuelve a teclear a mano la contraseña de Recurso Confiable, el secreto de Schmitz y vuelve a configurar el Studio iPaas.
6. Dale a **Guardar**. Las credenciales se cifrarán con la llave nueva y todo volverá a la normalidad en minutos. No pierdes históricos de vehículos, solo pierdes las credenciales de conexión.

## 2. ¿Cómo roto (cambio) la `MASTER_ENC_KEY` manualmente cada semana de forma segura?
Si deseas cambiar la llave maestra periódicamente (por políticas de seguridad corporativa) **SIN perder las contraseñas actuales**, ya existe un script preparado para eso.

El script descifrará la base de datos usando tu llave vieja, y volverá a cifrarla usando la llave nueva.

**Procedimiento:**
1. Genera una nueva llave (puedes usar un script de python: `from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())`).
2. Obtén la llave actual de tu archivo `.env`.
3. Ejecuta el script de rotación pasándole ambas llaves como variables de entorno:

```powershell
$env:OLD_KEY="tu_llave_vieja_del_env"
$env:NEW_KEY="la_nueva_llave_generada"
python scripts/rotate_master_key.py
```

4. El script actualizará los proveedores exitosamente.
5. Finalmente, abre tu archivo `.env`, reemplaza la `MASTER_ENC_KEY` vieja por la nueva, y reinicia el servidor.

## 3. ¿El cifrado afecta el rendimiento con millones de datos al día?
**No, el impacto es virtualmente cero (0).**
El cifrado se aplica sobre la configuración, no sobre los datos de GPS.
- **En modo PULL:** El Worker se despierta, lee la base de datos, descifra la contraseña **una sola vez**, y con esa contraseña trae 20,000 vehículos en una petición. No descifra 20,000 veces.
- **En modo PUSH:** El servidor lee el secreto esperado, lo descifra (tarda ~0.05 milisegundos) y compara. La red (internet) es 3,000 veces más lenta que la operación de descifrado, por lo que nunca será el cuello de botella.
