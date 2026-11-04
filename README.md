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

## Ejecución

```bash
python -m venv .venv
.venv\Scripts\activate        # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
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
| POST | `/readings` | Registra una lectura del nodo (`deviceCode`, `temperature`, `humidity`, `capturedAt` opcional). 201 si se calibra, 202 si se descarta |
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
- Si el buffer se llena, se descartan las pendientes más antiguas.
- Un nodo que supera su Sampling Interval se registra como `OfflineNode` (`SensorNodeWentOffline`) y se restaura al recibir una nueva lectura.

## Contrato con la API central

El gateway se autentica con la cabecera `X-Device-Credential` y publica:

- `POST {MACHINEGUARD_API_URL}/api/v1/measurements` con `{deviceCode, temperature, humidity, capturedAt}`
- `POST {MACHINEGUARD_API_URL}/api/v1/measurements/batch` con `{measurements: [...]}` (sincronización del buffer)

La credencial se emite en Core mediante un ADMIN activo, ligada a su organización y a los Sensor Nodes autorizados. Se envía sin Bearer y se mantiene en `EDGE_DEVICE_CREDENTIAL`, fuera del repositorio. La rotación o revocación se administra en Core; al cambiarla, reiniciar Edge con el nuevo valor conserva el buffer SQLite.

La confirmación requiere **2xx y un recibo válido**: `readingsAccepted` coincide con las lecturas enviadas y `measurementsCreated + measurementsReused` equivale a dos mediciones por lectura. Una página HTML, redirección, respuesta vacía o recibo incompleto no elimina pendientes. No se siguen redirecciones ni se registran secretos en logs.

Core resuelve `deviceCode` a los UUID de sus sensores autorizados y registra temperatura/humedad en una transacción. Un reintento idéntico responde con los registros existentes; datos distintos en el mismo instante son un conflicto 409. Así, si Core guardó y la respuesta se perdió, Edge puede confirmar después sin duplicar datos. Los errores de autenticación, autorización o conflicto conservan las lecturas pendientes para corregir su causa.

Se preservan microsegundos y se normaliza UTC. Los lotes se envían en bloques de hasta 500 lecturas, sin alterar el tamaño de selección del buffer. Si falla un bloque posterior, Edge conserva todo el lote seleccionado y reintenta; Core confirma idempotentemente los bloques ya recibidos. La sincronización se solicita mediante `POST /api/v1/edge/buffer/sync`; no se introduce un scheduler nuevo en esta entrega.

Configuración de ejemplo: `.env.example` (el proceso recibe las variables del entorno; no carga automáticamente el archivo). Las pruebas del transporte están en `tests/test_cloud_client.py`; ejecutar `python -m pytest` mantiene además la suite unitaria y los escenarios BDD integrados.

### Verificación contra Core real

`tests/test_live_core.py` usa la API real de IAM, configuración, gateways, mediciones y atención de incidentes. Preparar un Core y PostgreSQL **aislados de producción** y definir `MACHINEGUARD_CORE_URL`, `MACHINEGUARD_CORE_ADMIN_EMAIL` y `MACHINEGUARD_CORE_ADMIN_PASSWORD` solo en el entorno del proceso de pruebas. Ejecutar `python -m pytest tests/test_live_core.py`.

Las pruebas crean zonas y sensores con nombres E2E, aprovisionan credenciales limitadas, ejercitan calibración y lectura real, reconocimiento/acción/resolución, pérdida de confirmación y revocación con recuperación del buffer. Revocan las credenciales de pruebas al finalizar. Sin esas variables se omiten explícitamente; el resto de la suite sigue siendo autónomo. No ejecutarlas sobre una organización con datos reales.
