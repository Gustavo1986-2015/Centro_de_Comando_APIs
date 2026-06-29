# Arquitectura y Seguridad: Envelope Encryption

Este documento detalla el mecanismo de seguridad implementado para la gestión de credenciales en el Centro de Comando APIs.

## ¿Qué es Envelope Encryption?
Es la práctica de cifrar los datos (contraseñas, tokens) utilizando una llave, y a su vez proteger esa llave utilizando otra llave maestra. En nuestra arquitectura, lo simplificamos de la siguiente manera:

1. **Datos Sensibles:** Las contraseñas de Recurso Confiable (`rc_password`), los secretos de los Webhooks PUSH (`webhook_auth_secret`), y la configuración JSON de PULL (`fetch_config`) **NUNCA** se guardan en texto claro en la base de datos SQLite.
2. **La Llave Maestra (Vault Key):** Existe una variable de entorno llamada `MASTER_ENC_KEY` en el archivo `.env`. Esta llave es la única capaz de cifrar y descifrar los datos sensibles. **Nunca** abandona el servidor, no se sube a repositorios, y no se expone por la API.

## Flujo de Trabajo (JIT Decryption)
- **Escritura:** Cuando el administrador guarda una configuración en el Dashboard, el Backend (FastAPI) toma la contraseña, la cifra usando `Fernet` (AES-128 con autenticación HMAC) impulsado por la `MASTER_ENC_KEY`, y guarda el galimatías resultante en SQLite (`system_config_global.db`).
- **Lectura:** Cuando el motor PULL o PUSH necesita conectarse, extrae el texto cifrado de la base de datos y realiza un "Just-In-Time Decryption" (Descifrado en tiempo real) en la memoria RAM. La contraseña cruda existe en memoria solo por los milisegundos necesarios para establecer la conexión, minimizando la ventana de exposición.

## Beneficios
- **Mitigación de LFI (Local File Inclusion):** Si un atacante logra leer archivos del disco (ej. robando `system_config_global.db`), solo obtendrá basura criptográfica. Sin el `.env`, los datos son inútiles.
- **Fail-Closed:** Si la `MASTER_ENC_KEY` no coincide o se corrompe, el descifrado falla inmediatamente y el sistema bloquea el tráfico, protegiendo las integraciones.

Para detalles sobre recuperación en caso de pérdida de la llave, consulta el archivo [faqs.md](faqs.md).
