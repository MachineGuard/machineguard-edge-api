# MachineGuard Edge API

Bounded Context **Edge Processing** de MachineGuard. Se ejecuta en el Edge Gateway (Raspberry Pi o equivalente) dentro de la red del cliente: recibe las lecturas de los nodos ESP32, descarta las físicamente implausibles, aplica el Calibration Offset de cada nodo y las publica hacia la RESTful API central. Si no hay conectividad, las conserva en un Local Buffer y las sincroniza en orden cronológico al volver el enlace.

- Flask + Peewee ORM + SQLite
- Arquitectura DDD por capas: `domain`, `application`, `infrastructure`, `interfaces`

## Ejecución

```bash
python -m venv .venv
.venv\Scripts\activate        # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python run.py                 # http://localhost:5000
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
| GET | `/calibration/{deviceCode}/plausibility-range` | Rango de plausibilidad configurado |
| GET | `/buffer` | Estado del Local Buffer (pendientes, última sincronización) |
| POST | `/buffer/sync` | Fuerza la sincronización del buffer |
| POST | `/sensor-nodes` | Registra un Sensor Node en el gateway |
| GET | `/sensor-nodes` | Estado de conectividad de los nodos |
| GET | `/sensor-nodes/offline` | Nodos detectados como Offline |
| GET | `/health` | Estado del servicio |

## Reglas de negocio implementadas

- La lectura conserva la marca temporal de captura del nodo, no la del envío.
- Una lectura fuera del rango físico del sensor se descarta antes de calibrarse y nunca se publica.
- Solo se publican lecturas en estado `CALIBRATED`; sin conectividad se encolan en el Local Buffer.
- Mientras haya lecturas pendientes, las nuevas también se encolan para no romper el orden cronológico.
- La sincronización envía lotes en orden cronológico ascendente; una lectura `SYNCED` no se reenvía.
- Si el buffer se llena, se descartan las pendientes más antiguas.
- Un nodo que supera su Sampling Interval se registra como `OfflineNode` (`SensorNodeWentOffline`) y se restaura al recibir una nueva lectura.

## Contrato con la API central

El gateway se autentica con la cabecera `X-Device-Credential` y publica:

- `POST {MACHINEGUARD_API_URL}/api/v1/measurements` con `{deviceCode, temperature, humidity, capturedAt}`
- `POST {MACHINEGUARD_API_URL}/api/v1/measurements/batch` con `{measurements: [...]}` (sincronización del buffer)

Cualquier respuesta 2xx confirma la recepción; un error de red o un código distinto se trata como falta de conectividad.
