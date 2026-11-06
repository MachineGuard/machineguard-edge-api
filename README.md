# MachineGuard Edge API

Bounded Context **Edge Processing** de MachineGuard. Se ejecuta en el Edge Gateway (Raspberry Pi o equivalente) dentro de la red del cliente: recibe las lecturas de los nodos ESP32, descarta las físicamente implausibles, aplica el Calibration Offset de cada nodo y las publica hacia la RESTful API central. Si no hay conectividad, las conserva en un Local Buffer y las sincroniza en orden cronológico al volver el enlace.

- Flask + Peewee ORM + SQLite
- Arquitectura DDD por capas: `domain`, `application`, `infrastructure`, `interfaces`

## Acceso y configuración

Define las variables en el entorno antes de iniciar; `.env.example` sirve de
guía y no se carga automáticamente. `EDGE_ADMIN_KEY` debe ser una clave privada
aleatoria de al menos 32 bytes, distinta de la credencial de Core.
`EDGE_ADMIN_ID` identifica al administrador en el registro de calibración.

Edge escucha en `127.0.0.1` por defecto. Para recibir nodos por la red del banco,
configura `EDGE_BIND_HOST` con la IP privada del gateway. La administración
solo acepta conexiones locales; para acceder por otra IP de la laptop, añádela
a `EDGE_ADMIN_ALLOWED_IPS` (IP completas separadas por comas, sin rangos).
En una red compartida se requiere HTTPS con certificado validado.

Usa `X-Edge-Admin-Key` para registrar nodos, calibrar, consultar y sincronizar.
Al registrar un nodo, la respuesta entrega `nodeCredential` una sola vez:
guárdala en ese nodo y envíala en `X-Node-Credential` al publicar `/readings`
con su `deviceCode`. No combines ambas claves ni entregues la de Core al nodo.
Los nodos ya registrados necesitan emitir su credencial; la base conserva sus
datos. La ruta de emisión también permite cambiar la clave e invalida la anterior.

Sin credencial válida se responde 401; un origen o deviceCode no autorizado,
403. JSON inválido devuelve 400 y un cuerpo mayor que `EDGE_MAX_CONTENT_BYTES`
(65536 por defecto), 413. `batchSize` debe ser un entero de 1 a 500. Por defecto,
los offsets admiten ±10 °C y ±20 % HR; una lectura calibrada fuera de −40..80 °C
o 0..100 % HR se descarta (202) y no se envía a Core. Los límites se configuran
con las variables indicadas en `.env.example`.

### Muestras y hora del nodo

Cada lectura requiere `sampleId` con formato `bootId-secuencia`: un bootId nuevo
al reiniciar (1–64 caracteres; empieza por letra o dígito y admite guiones o
guiones bajos) y una secuencia creciente de hasta 20 dígitos, sin ceros iniciales.
Ejemplo de cuerpo:

```json
{"deviceCode":"ESP32-01","sampleId":"bootA-1","temperature":5,"humidity":50}
```

Sin hora NTP válida, omite `capturedAt`; Edge asigna la hora UTC una sola vez.
Si lo envías, debe incluir zona (`Z` u offset). Por defecto se aceptan hasta
60 s de adelanto, 30 días de antigüedad y fechas desde el 01/01/2026 UTC.
Configura estos límites con `EDGE_MAX_FUTURE_SECONDS`,
`EDGE_MAX_READING_AGE_DAYS` y `EDGE_MIN_CAPTURED_AT`; una fecha inválida devuelve 400 con motivo.

Para reintentar, conserva el mismo sampleId y cuerpo, incluso si después obtienes
hora NTP. Edge devuelve la fila guardada y su estado actual, sin recalibrar ni
reenviar; cambiar sus valores o fecha devuelve 409. Tras actualizar SQLite,
las lecturas anteriores se conservan con `sampleId=null`; no se les inventa una identidad.

### Entrega y recuperación

La respuesta confirma `persistence=LOCAL_COMMITTED`: la muestra ya está guardada.
`delivery=CONFIRMED` indica además un recibo válido de Core; `PENDING` indica
envío pendiente. Edge reintenta automáticamente con espera creciente, también
tras reiniciar. Conserva el mismo archivo SQLite. Para detenerlo desde Python,
usa `app.config['services'].runtime.stop()`; `run.py` lo hace al salir.

Un conflicto o valor rechazado queda en cuarentena; un problema de configuración
pausa solo el nodo afectado. Consulta `/health` y `/quarantine?limit=50&offset=0`
con la clave administrativa. Tras corregir la causa en Core, puedes solicitar
`POST /quarantine/{id}/reprocess`: conserva la muestra, calibración e historial.
Los nodos pausados se reintentan solos. `/buffer/sync` solicita un ciclo acotado;
si ya hay otro envío, devuelve `busy=true`.

Si se supera la capacidad, las pendientes más antiguas que no estén en envío
quedan como `DROPPED_OVERFLOW`, con un registro de pérdida. La evidencia sigue
en SQLite: la capacidad limita pendientes, no el tamaño total de la base.

## Ejecución

### ThingSpeak opcional

Está apagado por defecto. Configura `THINGSPEAK_ENABLED=true`, el `DEVICE_CODE`,
`CHANNEL_ID` y `WRITE_KEY` del ejemplo de entorno. Usa un canal dedicado a ese
nodo y a esta instancia Edge, sin otros escritores. No cambies el nodo ni la
ventana de un canal ya registrado; usa otro canal para otra selección.

El worker secundario lee muestras válidas ya guardadas desde su habilitación.
Agrupa por recepción UTC en ventanas de 20 s como mínimo: `field1` es temperatura
media calibrada en °C, `field2` humedad media en % HR y `field3` cantidad.
No rellena muestras ausentes ni sustituye los originales enviados a Core.
Congela valores e IDs de origen al cerrar la ventana. La clave solo viaja en
cabecera HTTPS; no llega al firmware. La licencia gratuita es no comercial y
exige al menos 15 s por canal; Edge espera una ventana entre intentos, también
al recuperar pendientes. [Límites oficiales](https://thingspeak.mathworks.com/pages/license_faq).

Consulta `thingspeak` en `/health`, `/thingspeak/jobs?state=UNCERTAIN&limit=50&offset=0`
y `/thingspeak/losses`, con la clave administrativa. La capacidad limita las
ventanas pendientes; un desborde conserva motivo y fuentes, sin borrar evidencia
primaria. La base crece con el historial; no hay purga automática.

`thingspeak.lastObservation` en `/health` muestra el último intento y sus fases
observadas, con tiempos relativos e IDs locales de intento y trabajo. Se conserva
solo en memoria; `UNKNOWN` significa que la fase no se observó. HTTP iniciado no
demuestra que DNS o TLS hayan terminado. Los logs acotados permiten correlacionar
el intento sin incluir claves ni payloads; el progreso no confirma un envío.

Un ID positivo completo confirma ThingSpeak; `0` conserva pendiente. Una respuesta
perdida puede haber guardado datos remotos: queda `UNCERTAIN`, sin retry automático.
Busca el identificador `mg:<id>` del payload en el canal antes de usar
`POST /thingspeak/jobs/{id}/reconcile` con `{"action":"confirmed","entryId":123}`
o `{"action":"abandon"}`. Para reintentar explícitamente usa
`{"action":"retry","acceptDuplicateRisk":true}`: puede duplicar el registro remoto.
Un envío secundario nunca confirma Core. Conserva SQLite al reiniciar.

Un canal real y la aceptación docente requieren comprobación posterior; las
pruebas locales usan un doble HTTP. ThingSpeak no resuelve los pendientes de
NTP/NVS, Wi-Fi virtual, IO ESP32, Docker ni hardware.

Para operación en Windows usa Python 3.12 y Waitress. Crea un venv nuevo e instala
el lock de ejecución; el archivo de entorno de ejemplo no se carga automáticamente:

```powershell
python -m venv .venv
& ./.venv/Scripts/python.exe -m pip install --require-hashes -r requirements.txt
& ./.venv/Scripts/python.exe -m edge_api.server
```

Escucha en `EDGE_BIND_HOST` y `EDGE_PORT` (127.0.0.1:5000 por defecto), con cuatro
hilos. Ctrl+C detiene servidor y runtime; para reiniciar conserva el mismo archivo
SQLite. Usa un solo proceso por archivo de base. `run.py` queda para desarrollo.
Para pruebas instala `requirements-test.txt` con `--require-hashes` en otro venv.

### Vencimiento y rotación del gateway

El health administrativo muestra `credentialExpiry`. Copia `expiresAt` de los
metadatos Core a `EDGE_DEVICE_CREDENTIAL_EXPIRES_AT`; si falta, se muestra UNKNOWN.
El aviso comienza siete días antes, configurable con `EDGE_CREDENTIAL_WARNING_SECONDS`.
La fecha es información configurada del operador; no se deduce del secreto ni
sustituye la validación de Core.

1. Un ADMIN con sesión vigente rota mediante `POST /api/v1/gateways/{id}/credentials`.
2. Detén Edge, sustituye en su entorno `EDGE_DEVICE_CREDENTIAL` y la fecha `expiresAt`
   de esa respuesta, y conserva SQLite. No copies secretos a logs, nodos ni Git.
3. Reinicia y consulta health/buffer: el worker reconcilia pendientes automáticamente.
   Una credencial vencida conserva la cola; si el gateway fue revocado, registra
   uno nuevo con los sensores permitidos y actualiza también `EDGE_GATEWAY_ID`.

El archivo `.in` lista dependencias directas; los `.txt` fijan también transitivas
y hashes. Para regenerarlos usa pip-tools con Python 3.12 y los comandos indicados
al comienzo de cada lock. La ejecución no necesita pytest ni pip-tools.

```bash
python -m venv .venv
.venv\Scripts\activate        # Linux/macOS: source .venv/bin/activate
pip install --require-hashes -r requirements-test.txt
# Set private environment variables first (see .env.example).
python run.py                 # http://127.0.0.1:5000
```

Pruebas:

```bash
python -m pytest
```

Los criterios de aceptación de TS02, TS03 y US03 están escritos en Gherkin, en español, en `tests/features` y se ejecutan con pytest-bdd (`tests/test_bdd_acceptance.py`). Para generar el reporte en formato Cucumber, crea la carpeta `reports` y ejecuta `python -m pytest --cucumberjson=reports/cucumber.json`. GitHub Actions (`.github/workflows/ci.yml`) ejecuta todas las pruebas en cada push y pull request a `develop` y `main`.

## Configuración (variables de entorno)

| Variable | Descripción | Por defecto |
|---|---|---|
| `EDGE_DATABASE_PATH` | Archivo SQLite del gateway | `edge.db` |
| `EDGE_GATEWAY_ID` | Identificador del gateway | `gateway-001` |
| `MACHINEGUARD_API_URL` | URL base de la API central (vacía = sin conectividad) | _vacía_ |
| `EDGE_DEVICE_CREDENTIAL` | Credencial de dispositivo entregada en la instalación | _vacía_ |
| `EDGE_BUFFER_CAPACITY` | Capacidad máxima del Local Buffer | `10000` |
| `EDGE_SYNC_BATCH_SIZE` | Lecturas por lote al sincronizar | `100` |
| `EDGE_SAMPLING_INTERVAL` | Sampling Interval por defecto (segundos) | `60` |
| `EDGE_AVAILABILITY_CHECK_SECONDS` | Cada cuánto se revisan los nodos fuera de línea | `30` |

## Endpoints (`/api/v1/edge`)

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/readings` | Registra una muestra (`deviceCode`, `sampleId`, `temperature`, `humidity`, `capturedAt` opcional). 201 si se calibra, 202 si se descarta |
| GET | `/readings` | Últimas lecturas procesadas |
| GET | `/readings/discarded` | Lecturas descartadas por implausibilidad |
| GET | `/calibration/{deviceCode}` | Calibration Profile del nodo |
| PUT | `/calibration/{deviceCode}/offset` | Actualiza el Calibration Offset (solo afecta lecturas posteriores) |
| GET | `/calibration/{deviceCode}/changes` | Consulta quién cambió los offsets, cuándo y sus valores |
| GET | `/calibration/{deviceCode}/plausibility-range` | Rango de plausibilidad configurado |
| GET | `/buffer` | Estado del Local Buffer (pendientes, última sincronización) |
| POST | `/buffer/sync` | Fuerza la sincronización del buffer |
| POST | `/sensor-nodes` | Registra un Sensor Node en el gateway |
| POST | `/sensor-nodes/{deviceCode}/credentials` | Emite o cambia la credencial de un nodo registrado |
| GET | `/sensor-nodes` | Estado de conectividad de los nodos |
| GET | `/sensor-nodes/offline` | Nodos detectados como Offline |
| GET | `/health` | Estado del servicio |

## Reglas de negocio implementadas

- La lectura conserva la marca temporal de captura del nodo, no la del envío.
- Una lectura fuera del rango físico del sensor se descarta antes de calibrarse y nunca se publica.
- La lectura calibrada también debe respetar el rango físico; un descarte no restaura ONLINE.
- Solo se publican lecturas en estado `CALIBRATED`; sin conectividad se encolan en el Local Buffer.
- Mientras haya lecturas pendientes, las nuevas también se encolan para no romper el orden cronológico.
- La sincronización envía lotes en orden cronológico ascendente; una lectura `SYNCED` no se reenvía.
- El desborde conserva la lectura y un registro de pérdida separado de la implausibilidad.
- OFFLINE usa la recepción válida y un margen configurable del 25 % del intervalo; capturas futuras, reintentos e implausibles no alargan esa ventana.

## Contrato con la API central

El gateway se autentica con la cabecera `X-Device-Credential` y publica:

- `POST {MACHINEGUARD_API_URL}/api/v1/measurements` con `{deviceCode, temperature, humidity, capturedAt}`
- `POST {MACHINEGUARD_API_URL}/api/v1/measurements/batch` con `{measurements: [...]}` (sincronización del buffer)

La credencial se emite en Core mediante un ADMIN activo, ligada a su organización y a los Sensor Nodes autorizados. Se envía sin Bearer y se mantiene en `EDGE_DEVICE_CREDENTIAL`, fuera del repositorio. La rotación o revocación se administra en Core; al cambiarla, reiniciar Edge con el nuevo valor conserva el buffer SQLite.

La confirmación requiere **2xx y un recibo válido**: `readingsAccepted` coincide con las lecturas enviadas y `measurementsCreated + measurementsReused` equivale a dos mediciones por lectura. Una página HTML, redirección, respuesta vacía o recibo incompleto no elimina pendientes. No se siguen redirecciones ni se registran secretos en logs.

Core resuelve `deviceCode` a los UUID de sus sensores autorizados y registra temperatura/humedad en una transacción. Un reintento idéntico responde con los registros existentes; datos distintos en el mismo instante son un conflicto 409. Así, si Core guardó y la respuesta se perdió, Edge puede confirmar después sin duplicar datos. Edge interpreta el campo `code`: errores de muestra van a cuarentena; errores de configuración pausan el nodo. Red, 401/403, 5xx y respuestas sin código conocido conservan pendientes.

Se preservan microsegundos y se normaliza UTC. Los lotes tienen hasta 500 lecturas; ante rechazos identificables o 5xx repetidos, Edge los divide con un límite de peticiones por ciclo. Core conserva la atomicidad de cada petición. El worker y el sync manual comparten exclusión; un recibo inválido nunca libera pendientes.

Configuración de ejemplo: `.env.example` (el proceso recibe las variables del entorno; no carga automáticamente el archivo). Las pruebas del transporte están en `tests/test_cloud_client.py`; ejecutar `python -m pytest` mantiene además la suite unitaria y los escenarios BDD integrados.

### Verificación contra Core real

`tests/test_live_core.py` usa la API real de IAM, configuración, gateways, mediciones y atención de incidentes. Preparar un Core y PostgreSQL **aislados de producción** y definir `MACHINEGUARD_CORE_URL`, `MACHINEGUARD_CORE_ADMIN_EMAIL` y `MACHINEGUARD_CORE_ADMIN_PASSWORD` solo en el entorno del proceso de pruebas. Ejecutar `python -m pytest tests/test_live_core.py`.

Las pruebas crean zonas y sensores con nombres E2E, aprovisionan credenciales limitadas, ejercitan calibración y lectura real, reconocimiento/acción/resolución, pérdida de confirmación y revocación con recuperación del buffer. Revocan las credenciales de pruebas al finalizar. Sin esas variables se omiten explícitamente; el resto de la suite sigue siendo autónomo. No ejecutarlas sobre una organización con datos reales.

La CI añade integración con PostgreSQL real y ambos SHA registrados mediante
`scripts/edge-core-integration.py`; requiere los dos checkouts y bases desechables
`mgci`/`mgci_test` en loopback. Sus comandos se describen en `scripts/ci.md`.
