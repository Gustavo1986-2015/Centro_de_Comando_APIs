# Hub de Integración Telemática y Centro de Comando

Microservicio on-premise desarrollado con FastAPI, SQLite y Vanilla JS que actúa como un hub centralizador de eventos telemáticos.

## Características Principales
*   **Recepción de Webhooks**: Endpoint asíncrono para recibir eventos (inicialmente Schmitz Cargobull v1.35).
*   **Mapeo y Normalización**: Transformación de payloads crudos a un modelo canónico centralizado (RC Canonical Model).
*   **Auditoría Dinámica**: Almacenamiento rotativo en archivos `.jsonl` separados por proveedor.
*   **Persistencia y Encolamiento**: Base de datos SQLite para mantener estado de los eventos recibidos y su estado de procesamiento.
*   **Cliente SOAP (Próximamente)**: Worker en background para la comunicación con Recurso Confiable.
*   **Dashboard Reactivo (Próximamente)**: Interfaz de Centro de Comando en tiempo real.

## Tecnologías Utilizadas
*   Python 3
*   FastAPI
*   SQLAlchemy
*   SQLite
*   HTTPX
*   Pydantic
