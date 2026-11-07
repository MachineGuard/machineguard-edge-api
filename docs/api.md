# API de Edge

Todas las rutas empiezan por `/api/v1/edge`. Las rutas administrativas usan la
cabecera `X-Edge-Admin-Key`. Los nodos solo pueden publicar en `POST /readings`,
con `X-Node-Credential`. Ver [configuración](configuracion.md).

## Lecturas y calibración

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/readings` | Registra una muestra (`deviceCode`, `sampleId`, `temperature`, `humidity`, `capturedAt` opcional). 201 si se calibra, 202 si se descarta |
| GET | `/readings` | Últimas lecturas procesadas |
| GET | `/readings/discarded` | Lecturas descartadas por implausibilidad |
| GET | `/calibration/{deviceCode}` | Calibration Profile del nodo |
| PUT | `/calibration/{deviceCode}/offset` | Actualiza el Calibration Offset. Solo afecta a lecturas posteriores |
| GET | `/calibration/{deviceCode}/changes` | Quién cambió los offsets, cuándo y con qué valores |
| GET | `/calibration/{deviceCode}/plausibility-range` | Rango de plausibilidad configurado |

## Buffer y nodos

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/buffer` | Estado del buffer: pendientes y última sincronización |
| POST | `/buffer/sync` | Pide una sincronización del buffer |
| POST | `/sensor-nodes` | Registra un Sensor Node en el gateway |
| POST | `/sensor-nodes/{deviceCode}/credentials` | Emite o cambia la credencial de un nodo registrado |
| GET | `/sensor-nodes` | Estado de conexión de los nodos |
| GET | `/sensor-nodes/offline` | Nodos detectados como OFFLINE |

## Cuarentena y salud

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Estado del servicio, incluido ThingSpeak y la caducidad de la credencial |
| GET | `/quarantine?limit=50&offset=0` | Muestras en cuarentena |
| POST | `/quarantine/{id}/reprocess` | Reprocesa una muestra en cuarentena, después de corregir la causa en Core |

## ThingSpeak (opcional)

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/thingspeak/jobs?state=UNCERTAIN&limit=50&offset=0` | Envíos a ThingSpeak por estado |
| GET | `/thingspeak/losses` | Pérdidas de ventanas de ThingSpeak |
| POST | `/thingspeak/jobs/{id}/reconcile` | Resuelve un envío `UNCERTAIN`. Ver [ThingSpeak](ejecucion-y-thingspeak.md#revisar-un-envío-uncertain) |

## Respuestas de la muestra

- `persistence=LOCAL_COMMITTED`: la muestra ya está guardada en Edge.
- `delivery=CONFIRMED`: Core devolvió un recibo válido.
- `delivery=PENDING`: el envío a Core sigue pendiente.

Más reglas de entrega y cuarentena en [arquitectura](arquitectura.md#entrega-y-recuperación).
