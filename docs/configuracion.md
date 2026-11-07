# Configuración

Define las variables en el entorno antes de iniciar Edge. `.env.example` tiene
todos los nombres y valores de ejemplo, pero el programa no lo carga solo.
Las claves reales no van a Git, a capturas ni a logs.

## Acceso administrativo

- `EDGE_ADMIN_KEY` es obligatoria. Es una clave privada aleatoria de al menos
  32 bytes, distinta de la credencial de Core. Va en la cabecera
  `X-Edge-Admin-Key` para registrar nodos, calibrar, consultar y sincronizar.
- `EDGE_ADMIN_ID` identifica al administrador en el registro de calibración.
- Edge escucha en `127.0.0.1` por defecto. Para recibir nodos desde la red del
  banco, pon en `EDGE_BIND_HOST` la IP privada del gateway.
- La administración solo acepta conexiones locales. Para entrar desde otra IP
  de la laptop, añádela en `EDGE_ADMIN_ALLOWED_IPS`: IPs completas separadas por
  comas, sin rangos ni comodines.
- En una red compartida se requiere HTTPS con certificado validado.

## Credenciales de los nodos

Al registrar un nodo, la respuesta trae `nodeCredential` **una sola vez**.
Guárdala en ese nodo. El nodo la envía en la cabecera `X-Node-Credential`
al publicar en `/readings`, junto con su `deviceCode`.

- No combines la clave administrativa con la credencial de un nodo.
- No entregues a un nodo la credencial de Core.
- Los nodos que ya estaban registrados necesitan emitir su credencial. La base
  conserva sus datos.
- Emitir una credencial nueva cambia la clave: la anterior deja de funcionar.

## Respuestas de error

| Código | Cuándo |
|---|---|
| 400 | JSON inválido, o una fecha `capturedAt` no válida (la respuesta indica el motivo) |
| 401 | Falta una credencial válida |
| 403 | El origen o el `deviceCode` no están autorizados |
| 409 | Mismo `sampleId` con valores o fecha distintos |
| 413 | El cuerpo supera `EDGE_MAX_CONTENT_BYTES` (65536 por defecto) |

`batchSize` debe ser un entero de 1 a 500.

## Límites de valores

- Los offsets de calibración admiten por defecto ±10 °C y ±20 % HR
  (`EDGE_MAX_TEMPERATURE_OFFSET`, `EDGE_MAX_HUMIDITY_OFFSET`).
- Una lectura ya calibrada fuera de −40..80 °C o de 0..100 % HR se descarta
  (respuesta 202) y **no se envía a Core**. Los rangos están en
  `EDGE_TEMPERATURE_MIN/MAX` y `EDGE_HUMIDITY_MIN/MAX`.

## Muestras y hora del nodo

Cada lectura necesita un `sampleId` con formato `bootId-secuencia`:

- `bootId`: cambia en cada reinicio del nodo. Tiene de 1 a 64 caracteres, empieza
  por letra o número y admite guiones y guiones bajos.
- `secuencia`: número creciente de hasta 20 dígitos, sin ceros al inicio.

Ejemplo de cuerpo:

```json
{"deviceCode":"ESP32-01","sampleId":"bootA-1","temperature":5,"humidity":50}
```

- Sin hora NTP válida, omite `capturedAt`. Edge asigna la hora UTC una sola vez.
- Si envías `capturedAt`, debe incluir zona (`Z` u offset).
- Por defecto se aceptan hasta 60 s de adelanto, 30 días de antigüedad y fechas
  desde el 01/01/2026 UTC. Cambia estos límites con `EDGE_MAX_FUTURE_SECONDS`,
  `EDGE_MAX_READING_AGE_DAYS` y `EDGE_MIN_CAPTURED_AT`.

Para reintentar, usa el mismo `sampleId` y el mismo cuerpo, aunque después
obtengas hora NTP. Edge devuelve la fila guardada y su estado actual, sin
recalibrar ni reenviar. Si cambias valores o fecha, responde 409.

Al actualizar la base SQLite, las lecturas anteriores se conservan con
`sampleId=null`. No se les inventa una identidad.

## Variables de entorno

| Variable | Descripción | Por defecto |
|---|---|---|
| `EDGE_DATABASE_PATH` | Archivo SQLite del gateway | `edge.db` |
| `EDGE_GATEWAY_ID` | Identificador del gateway | `gateway-001` |
| `MACHINEGUARD_API_URL` | URL base de la API central (vacía = sin conectividad) | _vacía_ |
| `EDGE_DEVICE_CREDENTIAL` | Credencial de gateway entregada en la instalación | _vacía_ |
| `EDGE_BUFFER_CAPACITY` | Capacidad máxima del buffer local | `10000` |
| `EDGE_SYNC_BATCH_SIZE` | Lecturas por lote al sincronizar | `100` |
| `EDGE_SAMPLING_INTERVAL` | Intervalo de muestreo por defecto, en segundos | `60` |
| `EDGE_AVAILABILITY_CHECK_SECONDS` | Cada cuánto se revisan los nodos sin conexión | `30` |

El archivo `.env.example` incluye además estos grupos:

| Grupo | Variables |
|---|---|
| Servidor | `EDGE_PORT`, `EDGE_WSGI_THREADS`, `EDGE_MAX_CONTENT_BYTES` |
| Administración | `EDGE_ADMIN_KEY`, `EDGE_ADMIN_ID`, `EDGE_ADMIN_ALLOWED_IPS`, `EDGE_BIND_HOST` |
| Calibración y rangos | `EDGE_MAX_TEMPERATURE_OFFSET`, `EDGE_MAX_HUMIDITY_OFFSET`, `EDGE_TEMPERATURE_MIN`, `EDGE_TEMPERATURE_MAX`, `EDGE_HUMIDITY_MIN`, `EDGE_HUMIDITY_MAX` |
| Hora de las muestras | `EDGE_MAX_FUTURE_SECONDS`, `EDGE_MAX_READING_AGE_DAYS`, `EDGE_MIN_CAPTURED_AT` |
| Entrega automática | `EDGE_DELIVERY_POLL_SECONDS`, `EDGE_DELIVERY_BACKOFF_SECONDS`, `EDGE_DELIVERY_MAX_BACKOFF_SECONDS`, `EDGE_DELIVERY_MAX_REQUESTS`, `EDGE_DELIVERY_MAX_DEPTH`, `EDGE_DELIVERY_HTTP_TIMEOUT_SECONDS`, `EDGE_DELIVERY_LEASE_SECONDS`, `EDGE_NODE_PAUSE_SECONDS`, `EDGE_OFFLINE_GRACE_RATIO` |
| Credencial del gateway | `EDGE_DEVICE_CREDENTIAL_EXPIRES_AT`, `EDGE_CREDENTIAL_WARNING_SECONDS` |
| ThingSpeak (opcional) | `THINGSPEAK_ENABLED` y las demás `THINGSPEAK_*` de `.env.example` |

Para ThingSpeak y la rotación de credenciales, ver
[ejecución y ThingSpeak](ejecucion-y-thingspeak.md).

## Dependencias

El archivo `.in` lista las dependencias directas. Los `.txt` fijan también las
transitivas y sus hashes. Para regenerarlos, usa pip-tools con Python 3.12 y los
comandos indicados al comienzo de cada lock. Ejecutar Edge no necesita pytest
ni pip-tools.
